"""Regression tests for defects found by outside-in testing."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from referee import codescan, config, decisions, mcp_server, ops, preflight, scorecard, spec
from tests import helpers as h
from tests.test_core import FakeFlagsmith, winning_card
from tests.test_preflight import Fake
from tests.test_review import FakeSettings


def problems(**changes):
    data = copy.deepcopy(h.PLAN)
    data.update(changes)
    try:
        spec.parse(data)
    except spec.PlanError as error:
        return " ".join(error.problems)
    return ""


class LopsidedSplitTests(unittest.TestCase):
    def test_plan_that_gives_nearly_everyone_the_variant_is_refused(self):
        text = problems(variants=[{"key": "new", "value": "new", "share": 99.99}])
        self.assertIn("rollout, not an experiment", text)
        self.assertEqual(problems(variants=[{"key": "new", "value": "new", "share": 90}]), "")

    def test_preflight_fails_an_existing_lopsided_experiment(self):
        experiment = h.experiment(status="created", variants=(("three_fields", 97.0),))
        result = preflight.run(Fake(experiment), 7)
        self.assertFalse(result["ready"])
        self.assertIn("control_share", [c["id"] for c in result["checks"] if c["status"] == "fail"])

    def test_agent_cannot_start_an_experiment_exposing_most_users(self):
        self.assertEqual(ops.exposure(h.experiment(variants=(("b", 50.0),))), 50.0)
        self.assertEqual(ops.exposure(h.experiment(variants=(("b", 45.0), ("c", 45.0)))), 90.0)
        with tempfile.TemporaryDirectory() as folder:
            client = FakeFlagsmith()
            settings = FakeSettings(folder, client)
            plan = copy.deepcopy(h.PLAN)
            plan["variants"] = [{"key": "b", "value": "b", "share": 45}, {"key": "c", "value": "c", "share": 45}]
            ops.apply_plan(settings, plan)
            refused = ops.start(settings, 7, max_exposure=ops.AGENT_MAX_EXPOSURE)
            self.assertFalse(refused["started"])
            self.assertIn("90%", refused["reason"])
            self.assertNotIn("start", client.calls)
            self.assertTrue(ops.start(settings, 7)["started"])  # a person at the CLI may


class PlannedSampleTravelsTests(unittest.TestCase):
    def test_marker_round_trip(self):
        plan = spec.parse(h.PLAN)
        stored = plan.hypothesis_with_marker()
        self.assertEqual(spec.planned_sample_from(stored), plan.sizing()["per_arm"])
        self.assertEqual(spec.without_marker(stored), plan.hypothesis)
        self.assertIsNone(spec.planned_sample_from("Just a hypothesis."))
        self.assertIn("Referee writes that itself", problems(hypothesis=stored))

    def test_target_comes_from_flagsmith_when_no_local_plan_exists(self):
        early = {"control": (400, 60), "three_fields": (400, 82)}
        experiment = h.experiment(hypothesis=spec.parse(h.PLAN).hypothesis_with_marker())
        card = scorecard.judge(experiment, h.results(early), h.exposures(), None, now=h.NOW)
        self.assertEqual(card["progress"]["needed_per_arm"], spec.parse(h.PLAN).sizing()["per_arm"])
        self.assertEqual(card["verdict"]["call"], "keep_running")
        self.assertNotIn("Referee:", card["experiment"]["hypothesis"])

    def test_easier_plan_from_a_fresh_checkout_does_not_move_the_target(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as fresh:
            client = FakeFlagsmith()
            ops.apply_plan(FakeSettings(first, client), h.PLAN)
            client.experiment_rows[0]["status"] = "running"
            easier = copy.deepcopy(h.PLAN)
            easier["assumptions"]["minimum_lift"] = 3.0
            again = ops.apply_plan(FakeSettings(fresh, client), easier)
            self.assertIn("not stored", " ".join(again["actions"]))
            self.assertFalse((Path(fresh) / "plans").exists())
            self.assertEqual(
                spec.planned_sample_from(client.experiment(7)["hypothesis"]), spec.parse(h.PLAN).sizing()["per_arm"]
            )
            self.assertIn("did not change", again["next"])


class StuckCollectingTests(unittest.TestCase):
    def test_arm_with_no_conversions_is_investigated_not_awaited(self):
        card = scorecard.judge(
            h.experiment(), h.results({"control": (600, 0), "three_fields": (600, 197)}), h.exposures(),
            spec.parse(h.PLAN), now=h.NOW,
        )
        self.assertEqual(card["verdict"]["call"], "investigate")
        text = " ".join(card["verdict"]["reasons"])
        self.assertIn("cannot be completed at all", text)
        self.assertIn("referee preview 7 control", text)
        self.assertIn("--void", text)

    def test_few_conversions_everywhere_says_which_floor_is_missing(self):
        card = scorecard.judge(
            h.experiment(), h.results({"control": (200, 2), "three_fields": (200, 4)}), h.exposures(),
            spec.parse(h.PLAN), now=h.NOW,
        )
        self.assertEqual(card["verdict"]["call"], "collecting")
        self.assertIn("control 2, three_fields 4", " ".join(card["verdict"]["reasons"]))


class ScannerTests(unittest.TestCase):
    def scan(self, name, source):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, name).write_text(source)
            return codescan.scan(Path(folder), "signup_form", ["three_fields"])

    def test_commented_out_calls_are_not_instrumentation(self):
        found = self.scan("a.py", '# TODO: fs.get_experiment_flag("signup_form") and fs.track_event("signup_completed")\n')
        self.assertEqual(found.exposing, [])
        self.assertEqual(found.tracked_events, {})
        self.assertEqual([r.kind for r in found.references], ["mention"])

    def test_go_event_call_with_a_context_argument(self):
        found = self.scan("main.go", 'flag, _ := client.GetExperimentFlag(ctx, "signup_form", ec)\nclient.TrackEvent(ctx, "signup_completed", id)\n')
        self.assertIn("signup_completed", found.tracked_events)
        self.assertEqual(len(found.exposing), 1)

    def test_skipped_files_are_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "big.py").write_text("x = 1\n" * 250000)
            Path(folder, "ok.py").write_text("x = 1\n")
            found = codescan.scan(Path(folder), "signup_form", [])
            self.assertEqual((found.files_scanned, found.files_skipped), (1, 1))


class EventNameTests(unittest.TestCase):
    def test_another_experiments_event_does_not_block(self):
        client = Fake(seen=["signup_error", "signup_complete_v2"])
        original = client.metrics
        client.metrics = lambda: original() + [{"id": 300, "name": "Other", "definition": {"event": "signup_complete_v2"}}]
        result = preflight.run(client, 7)
        self.assertTrue(result["ready"], result["checks"])

    def test_similar_unclaimed_name_is_a_warning_and_exact_respelling_a_failure(self):
        near = preflight.run(Fake(seen=["signup_complete"]), 7)
        self.assertTrue(near["ready"])
        self.assertIn("warn", [c["status"] for c in near["checks"] if c["id"] == "event_seen:signup_completed"])
        same = preflight.run(Fake(seen=["Signup-Completed"]), 7)
        self.assertFalse(same["ready"])


class PlanInputTests(unittest.TestCase):
    def test_rejections(self):
        self.assertIn("Unknown key 'variantz' (did you mean 'variants'?)", problems(variantz=[]))
        self.assertIn("Unknown key 'assumptions.baseline'", problems(assumptions={"baseline": 0.2}))
        self.assertIn("control characters", problems(name="bad\x00name"))
        self.assertIn("flag must be at most", problems(flag="a" * 3000))
        self.assertIn("served by another variant", problems(variants=[
            {"key": "a", "value": "same", "share": 20}, {"key": "b", "value": "same", "share": 20}]))
        self.assertIn("used by another metric", problems(metrics=[
            {"name": "M", "event": "one"}, {"name": "M", "event": "two", "goal": "not_increase"}]))
        self.assertIn("at least 0.001", problems(assumptions={"baseline_rate": 0.2, "minimum_lift": 1e-12}))
        self.assertIn("must be between", problems(assumptions={"daily_identities": 10 ** 400}))
        self.assertIn("quote numbers", problems(variants=[{"key": "a", "value": 2, "share": 20}]))


class McpTests(unittest.TestCase):
    def test_deeply_nested_input_gets_a_parse_error_and_the_session_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            out = io.StringIO()
            lines = "[" * 200000 + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
            mcp_server.serve(config.Settings(state_dir=Path(folder)), io.StringIO(lines), out)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(answers[0]["error"]["code"], -32700)
        self.assertEqual(answers[1]["result"], {})


class StateTests(unittest.TestCase):
    def test_stray_files_in_the_decisions_folder_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = decisions.Ledger(Path(folder))
            ledger.propose(winning_card(), "env")
            Path(folder, "decisions", "D-junk.json").write_text("[1, 2]")
            Path(folder, "decisions", "D-bad.json").write_text("{nope")
            self.assertEqual(len(ledger.all()), 1)

    def test_long_note_is_capped(self):
        with tempfile.TemporaryDirectory() as folder:
            decision = decisions.Ledger(Path(folder)).propose(winning_card(), "env", note="x" * 100000)
            self.assertEqual(len(decision["note"]), decisions.MAX_NOTE)

    def test_stale_results_block_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = decisions.Ledger(Path(folder))
            human = FakeFlagsmith()
            decision = ledger.propose(winning_card(), "env")
            old = winning_card()
            old["freshness"]["age_minutes"] = 45.0
            with self.assertRaises(decisions.DecisionError) as caught:
                ledger.approve(decision["id"], human, lambda _id: old)
            self.assertIn("45 minutes old", str(caught.exception))
            self.assertIsNone(human.completed)

    def test_settings_problems_never_echo_the_key(self):
        with tempfile.TemporaryDirectory() as folder:
            for key in ("abc.SECRETSECRET\rx", "abc.SECRET SECRET"):
                with self.assertRaises(ValueError) as caught:
                    config.load(Path(folder), environ={"FLAGSMITH_API_KEY": key})
                self.assertNotIn("SECRET", str(caught.exception))
            with self.assertRaises(ValueError):
                config.load(Path(folder), environ={"FLAGSMITH_ENVIRONMENT_KEY": "a b/../c?x=1"})
            Path(folder, ".env").write_text("FLAGSMITH_ENVIRONMENT_KEY = key123  # the prod one\n")
            self.assertEqual(config.load(Path(folder), environ={}).environment_key, "key123")
            Path(folder, "referee.json").write_text("[1]")
            with self.assertRaises(ValueError):
                config.load(Path(folder), environ={})


if __name__ == "__main__":
    unittest.main()
