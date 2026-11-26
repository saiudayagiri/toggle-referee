"""Regression tests for the third verification round."""

import copy
import tempfile
import unittest
from pathlib import Path

from referee import codescan, ops, preflight, provision, scorecard, spec
from tests import helpers as h
from tests.test_core import FakeFlagsmith
from tests.test_preflight import Fake
from tests.test_review import FakeSettings


class NothingIsServedBeforeStartTests(unittest.TestCase):
    """Flagsmith serves an experiment's rollout from the moment it exists."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.client = FakeFlagsmith()
        self.settings = FakeSettings(self.folder.name, self.client)

    def tearDown(self):
        self.folder.cleanup()

    def rollout(self):
        return self.client.experiment(7)["experiment_rollout"]["rollout_percentage"]

    def test_apply_creates_the_experiment_at_zero_percent(self):
        ops.apply_plan(self.settings, h.PLAN)
        self.assertEqual(self.rollout(), 0)
        hypothesis = self.client.experiment(7)["hypothesis"]
        self.assertEqual(spec.planned_rollout_from(hypothesis), 100.0)
        self.assertEqual(spec.planned_sample_from(hypothesis), spec.parse(h.PLAN).sizing()["per_arm"])

    def test_start_raises_the_rollout_and_pause_drops_it(self):
        plan = copy.deepcopy(h.PLAN)
        plan["rollout_percentage"] = 40
        ops.apply_plan(self.settings, plan)
        started = ops.start(self.settings, 7)
        self.assertTrue(started["started"])
        self.assertEqual(self.rollout(), 40)
        self.assertLess(self.client.calls.index("start"), self.client.calls.index("rollout 40"))
        paused = ops.pause(self.settings, 7)
        self.assertFalse(paused["serving"])
        self.assertEqual(self.rollout(), 0)
        self.assertTrue(ops.start(self.settings, 7)["started"])  # resume
        self.assertEqual(self.rollout(), 40)

    def test_exposure_cap_uses_the_planned_rollout_not_the_live_zero(self):
        plan = copy.deepcopy(h.PLAN)
        plan["variants"] = [{"key": "b", "value": "b", "share": 45}, {"key": "c", "value": "c", "share": 45}]
        ops.apply_plan(self.settings, plan)
        self.assertEqual(self.rollout(), 0)
        self.assertEqual(ops.exposure(self.client.experiment(7)), 90.0)
        refused = ops.start(self.settings, 7, max_exposure=ops.AGENT_MAX_EXPOSURE)
        self.assertFalse(refused["started"])
        self.assertEqual(self.rollout(), 0)

    def test_missing_rollout_is_treated_as_everyone(self):
        self.assertEqual(ops.planned_rollout({"experiment_rollout": {}}), 100.0)

    def test_pausing_an_outside_experiment_notes_its_rollout_first(self):
        ops.apply_plan(self.settings, h.PLAN)
        row = self.client.experiment(7)
        row.update(status="running", hypothesis="Made in the Flagsmith UI.")
        row["experiment_rollout"]["rollout_percentage"] = 30
        ops.pause(self.settings, 7)
        self.assertEqual(self.rollout(), 0)
        self.assertEqual(spec.planned_rollout_from(row["hypothesis"]), 30.0)
        ops.start(self.settings, 7)
        self.assertEqual(self.rollout(), 30)

    def test_apply_zeroes_an_unstarted_experiment_that_is_already_serving(self):
        ops.apply_plan(self.settings, h.PLAN)
        row = self.client.experiment(7)
        row["hypothesis"] = spec.parse(h.PLAN).hypothesis
        row["experiment_rollout"]["rollout_percentage"] = 100
        result = ops.apply_plan(self.settings, h.PLAN)
        self.assertEqual(self.rollout(), 0)
        self.assertIn("already serving", " ".join(result["actions"]))

    def test_preflight_warns_when_an_unstarted_experiment_is_serving(self):
        experiment = h.experiment(status="created")
        result = preflight.run(Fake(experiment), 7)
        self.assertIn("serving_before_start", [c["id"] for c in result["checks"] if c["status"] == "warn"])
        experiment["experiment_rollout"]["rollout_percentage"] = 0
        experiment["hypothesis"] = "x" + spec.marker(100, None)
        quiet = preflight.run(Fake(experiment), 7)
        self.assertNotIn("serving_before_start", [c["id"] for c in quiet["checks"]])
        self.assertNotIn("rollout", [c["id"] for c in quiet["checks"] if c["status"] == "fail"])


class StatusQuoStrictTests(unittest.TestCase):
    def refusal(self, state, plan=None):
        client = FakeFlagsmith()
        client.features_by_name["signup_form"] = {"id": 3, "name": "signup_form", "type": "MULTIVARIATE", "initial_value": "six_fields"}
        client.environment_default = state if callable(state) else (lambda _id: state)
        try:
            provision.apply(client, spec.parse(plan or h.PLAN))
        except provision.ProvisionError as error:
            self.assertEqual(client.calls, [], "refused, so nothing may have been created")
            return str(error)
        return ""

    def test_good_state_is_accepted(self):
        self.assertEqual(self.refusal({"feature_state_value": "six_fields", "enabled": True}), "")

    def test_refusals(self):
        self.assertIn("switched off", self.refusal({"feature_state_value": "six_fields", "enabled": False}))
        self.assertIn("no value", self.refusal({"feature_state_value": None, "enabled": True}))
        self.assertIn("text-valued", self.refusal({"feature_state_value": 5, "enabled": True}))
        self.assertIn("text-valued", self.refusal({"feature_state_value": True, "enabled": True}))
        self.assertIn("reports no value", self.refusal(None))

        def unreachable(_id):
            raise provision.FlagsmithError(403, "GET", "/featurestates/", "forbidden")

        self.assertIn("Could not read", self.refusal(unreachable))

    def test_variant_value_conflict_is_caught_before_anything_is_created(self):
        client = FakeFlagsmith()
        client.features_by_name["signup_form"] = {"id": 3, "name": "signup_form", "type": "MULTIVARIATE", "initial_value": "six_fields"}
        client.variant_rows[3] = [{"id": 10, "key": "three_fields", "string_value": "something_else", "type": "unicode"}]
        plan = copy.deepcopy(h.PLAN)
        plan["variants"].append({"key": "extra", "value": "extra", "share": 10})
        with self.assertRaises(provision.ProvisionError) as caught:
            provision.apply(client, spec.parse(plan))
        self.assertIn("serves 'something_else' in Flagsmith but the plan says 'three_fields'", str(caught.exception))
        self.assertEqual(client.calls, [])

    def test_a_refused_step_reports_what_already_exists(self):
        client = FakeFlagsmith()

        def refuse(body):
            raise provision.FlagsmithError(400, "POST", "/experiments/", {"name": ["nope"]})

        client.create_experiment = refuse
        with self.assertRaises(provision.ProvisionError) as caught:
            provision.apply(client, spec.parse(h.PLAN))
        self.assertIn("stays in place and is reused", str(caught.exception))


class StoppingRuleTests(unittest.TestCase):
    EARLY = {"control": (300, 60), "three_fields": (300, 84)}

    def days(self, points, now=h.NOW):
        payload = {"exposures_timeseries": {"granularity": "hour", "points": points}}
        return scorecard._days_of_data(payload, {}, now)

    def point(self, days_ago, count):
        return {"bucket": (h.NOW - h.timedelta(days=days_ago)).isoformat(), "new_identities": {"control": count, "b": count}}

    def test_days_of_data_cannot_be_stretched(self):
        self.assertEqual(self.days([self.point(0, 300)]), 1)
        self.assertEqual(self.days([self.point(7, 1), self.point(0, 300)]), 1)       # one stray early visitor
        self.assertEqual(self.days([self.point(30, 150), self.point(0, 150)]), 2)    # a long pause in between
        self.assertEqual(self.days([self.point(-8, 300), self.point(0, 300)]), 1)    # a bucket dated in the future
        self.assertEqual(self.days([self.point(d, 100) for d in range(7)]), 7)
        self.assertEqual(self.days([{"bucket": h.NOW.isoformat(), "new_identities": {"a": None, "b": "x"}}]), 0)
        self.assertEqual(self.days([]), 0)

    def test_stray_early_visitor_does_not_ship_an_unplanned_lead(self):
        data = h.results(self.EARLY)
        data["payload"]["exposures_timeseries"]["points"].insert(
            0, {"bucket": (h.NOW - h.timedelta(days=7)).isoformat(), "new_identities": {"control": 1}}
        )
        card = scorecard.judge(h.experiment(started_hours_ago=200), data, h.exposures(), None, now=h.NOW)
        self.assertEqual(card["verdict"]["call"], "keep_running")

    def test_trailing_variant_on_a_tiny_sample_is_not_ended(self):
        data = dict(h.PLAN, assumptions={"baseline_rate": 0.2, "minimum_lift": 0.05, "daily_identities": 1000})
        behind = {"control": (300, 60), "three_fields": (300, 42)}
        card = scorecard.judge(h.experiment(), h.results(behind), h.exposures(), spec.parse(data), now=h.NOW)
        chance = card["metrics"][0]["arms"]["three_fields"]["chance_good"]
        self.assertTrue(0.001 < chance <= 0.05, chance)
        self.assertEqual(card["verdict"]["call"], "keep_running")
        self.assertFalse(card["verdict"]["actionable"])
        unplanned = scorecard.judge(h.experiment(), h.results(behind), h.exposures(), None, now=h.NOW)
        self.assertEqual(unplanned["verdict"]["call"], "keep_running")

    def test_overwhelming_loss_can_still_be_ended_early(self):
        data = dict(h.PLAN, assumptions={"baseline_rate": 0.2, "minimum_lift": 0.05, "daily_identities": 1000})
        card = scorecard.judge(
            h.experiment(), h.results({"control": (400, 120), "three_fields": (400, 60)}), h.exposures(), spec.parse(data), now=h.NOW,
        )
        self.assertEqual(card["verdict"]["call"], "keep_control")

    def test_guardrail_block_on_a_tiny_sample_is_not_ended(self):
        data = dict(h.PLAN, assumptions={"baseline_rate": 0.2, "minimum_lift": 0.05, "daily_identities": 1000})
        card = scorecard.judge(
            h.experiment(), h.results(self.EARLY, errors={"control": (300, 6), "three_fields": (300, 30)}),
            h.exposures(), spec.parse(data), now=h.NOW,
        )
        self.assertEqual(card["verdict"]["call"], "keep_running")
        self.assertIn("cannot be shipped", " ".join(card["verdict"]["reasons"]))


class ResolveTests(unittest.TestCase):
    def test_two_live_experiments_with_one_name_need_an_id(self):
        class Client:
            def experiments(self):
                return [
                    {"id": 1, "name": "same", "status": "created", "feature": {"name": "a"}},
                    {"id": 2, "name": "same", "status": "running", "feature": {"name": "b"}},
                    {"id": 3, "name": "done", "status": "completed", "feature": {"name": "c"}},
                    {"id": 4, "name": "done", "status": "created", "feature": {"name": "c"}},
                ]

        with self.assertRaises(ops.OpError) as caught:
            ops.resolve_experiment(Client(), "same")
        self.assertIn("ids 1, 2", str(caught.exception))
        self.assertEqual(ops.resolve_experiment(Client(), "done"), 4)
        self.assertEqual(ops.resolve_experiment(Client(), "2"), 2)


class ScannerRound3Tests(unittest.TestCase):
    def scan(self, name, source, flag="f"):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, name).write_text(source)
            return codescan.scan(Path(folder), flag, [])

    def test_urls_are_not_comments(self):
        for name, source in (
            ("a.html", "<p>See https://example.com</p><script>fs.trackEvent('real'); fs.getExperimentFlag('f')</script>\n"),
            ("b.jsx", "return <p>Docs: https://x.io <button onClick={() => fs.trackEvent('real')}>Go</button></p>;\n"),
            ("c.html", "<script src=//cdn.x/y.js></script><script>fs.trackEvent('real')</script>\n"),
            ("d.php", "<style>a { color: #fff; }</style><?php $fs->trackEvent('real'); ?>\n"),
            ("e.js", "var r=/'/;var u='http://x';fs.trackEvent('real');\n"),
        ):
            self.assertIn("real", self.scan(name, source).tracked_events, name)

    def test_call_shapes(self):
        for name, source in (
            ("f.go", 'c.TrackEvent(context.WithValue(r.Context(), k, v), "real", nil)\n'),
            ("g.py", 'fs.track_event(identifier=u, event="real")\n'),
            ("h.rb", "fs.track_event 'real', identifier: u\n"),
            ("i.ts", "fs.trackEvent<Props>('real')\n"),
        ):
            self.assertIn("real", self.scan(name, source).tracked_events, name)

    def test_event_named_by_a_constant_is_a_warning_not_a_failure(self):
        found = self.scan("a.py", "fs.track_event(EVENT_NAME, identifier=u)\n")
        self.assertEqual(found.tracked_events, {})
        self.assertEqual(len(found.unnamed_tracks), 1)

    def test_text_blocks_and_escaped_quotes_in_docstrings(self):
        found = self.scan("a.java", 'String help = """\n  fs.trackEvent("fake")\n""";\nfs.trackEvent("real");\n')
        self.assertEqual(sorted(found.tracked_events), ["real"])
        found = self.scan("b.py", '"""a \\""" fs.track_event("fake")\n"""\nfs.track_event("real")\n')
        self.assertEqual(sorted(found.tracked_events), ["real"])

    def test_read_is_classified_by_the_nearest_call(self):
        refs = self.scan("a.py", "name = 'get_experiment_flag'\nv = fs.get_feature_value('f')\n").references
        self.assertEqual([r.kind for r in refs], ["plain_read"])
        refs = self.scan("b.py", 'flag = fs.get_experiment_flag(\n    identifier=u,\n    traits=None,\n    feature_name="f",\n)\n').references
        self.assertEqual([r.kind for r in refs], ["exposing"])


if __name__ == "__main__":
    unittest.main()
