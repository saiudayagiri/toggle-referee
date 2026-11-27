"""Tests for changes that came out of a fresh coding agent running an experiment with Referee."""

import copy
import tempfile
import unittest
from pathlib import Path

from referee import cli, codescan, decisions, preflight, report, scorecard, spec
from tests import helpers as h
from tests.test_core import winning_card
from tests.test_preflight import Fake, status_of


def scan(files, flag="signup_form"):
    with tempfile.TemporaryDirectory() as folder:
        for name, source in files.items():
            Path(folder, name).write_text(source)
        return codescan.scan(Path(folder), flag, ["three_fields"])


def run_preflight(source):
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "app.py").write_text(source)
        return preflight.run(Fake(), 7, plan=spec.parse(h.PLAN), code_root=Path(folder))


class ConstantsTests(unittest.TestCase):
    APP = (
        'FLAG = "signup_form"\nDONE = "signup_completed"\n'
        "flag = fs.get_experiment_flag(FLAG, identifier=u)\n"
        'if flag.value == "three_fields":\n    short()\n'
        'fs.track_event(DONE, identifier=u)\nfs.track_event("signup_error", identifier=u)\n'
    )

    def test_flag_name_in_a_constant_is_followed(self):
        found = scan({"app.py": self.APP})
        self.assertEqual([(r.line, r.kind) for r in found.references], [(1, "constant"), (3, "exposing")])
        self.assertEqual(sorted(found.tracked_events), ["signup_completed", "signup_error"])
        self.assertEqual(found.unnamed_tracks, [])

    def test_preflight_passes_code_that_uses_constants(self):
        result = run_preflight(self.APP)
        self.assertTrue(result["ready"], [c for c in result["checks"] if c["status"] == "fail"])
        self.assertEqual(status_of(result, "code_exposure"), "pass")

    def test_constant_defined_in_another_file(self):
        found = scan({"names.ts": "export const FLAG = 'signup_form';\n", "page.tsx": "const f = useExperiment(FLAG);\n"})
        self.assertEqual([(r.file, r.kind) for r in found.exposing], [("page.tsx", "exposing")])

    def test_event_constant_as_second_argument(self):
        found = scan({"m.go": 'const Done = "signup_completed"\nclient.TrackEvent(ctx, Done, nil)\n'})
        self.assertIn("signup_completed", found.tracked_events)

    def test_name_built_at_run_time_is_a_warning_with_honest_wording(self):
        result = run_preflight(
            "name = pick()\nflag = fs.get_experiment_flag(name, identifier=u)\n"
            'fs.track_event("signup_completed", identifier=u)\nfs.track_event("signup_error", identifier=u)\n'
        )
        self.assertEqual(status_of(result, "code_exposure"), "warn")
        self.assertTrue(result["ready"])

    def test_wrapper_definition_is_not_counted_as_an_unnamed_call(self):
        found = scan({"app.py": 'def track_event(event, who):\n    pass\n\ntrack_event("signup_completed", u)\n'})
        self.assertEqual(found.unnamed_tracks, [])
        self.assertIn("signup_completed", found.tracked_events)

    def test_guardrail_fix_text_does_not_talk_about_success(self):
        result = run_preflight('flag = fs.get_experiment_flag("signup_form", identifier=u)\nfs.track_event("signup_completed", identifier=u)\n')
        check = [c for c in result["checks"] if c["id"] == "code_event:signup_error"][0]
        self.assertNotIn("success", check["fix"])

    def test_events_seen_before_do_not_count_as_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            result = preflight.run(Fake(seen=["signup_completed", "signup_error"]), 7)
        self.assertEqual(status_of(result, "event_seen:signup_completed"), "info")


class ScorecardWordingTests(unittest.TestCase):
    N = spec.parse(h.PLAN).sizing()["per_arm"]

    def card(self, errors, spread_days=0):
        arms = {"control": (self.N, int(self.N * 0.2)), "three_fields": (self.N, int(self.N * 0.27))}
        return scorecard.judge(
            h.experiment(), h.results(arms, errors, spread_days=spread_days), h.exposures(), spec.parse(h.PLAN), now=h.NOW,
        )

    def test_guardrail_states_are_named_for_what_they_mean(self):
        held = self.card({"control": (self.N, 30), "three_fields": (self.N, 28)})
        self.assertEqual(held["metrics"][1]["arms"]["three_fields"]["state"], "held")
        leaning = self.card({"control": (self.N, 30), "three_fields": (self.N, 37)})
        chance = leaning["metrics"][1]["arms"]["three_fields"]["chance_good"]
        self.assertTrue(0.05 < chance < 0.25, chance)
        self.assertEqual(leaning["metrics"][1]["arms"]["three_fields"]["state"], "leans worse")
        self.assertIn("leans worse", " ".join(leaning["verdict"]["reasons"]))
        self.assertIn("leans worse", report.scorecard_text(leaning))

    def test_data_from_a_single_day_is_pointed_out_on_an_actionable_call(self):
        one_day = self.card({"control": (self.N, 30), "three_fields": (self.N, 28)})
        self.assertIn("single day", " ".join(one_day["verdict"]["reasons"]))
        week = self.card({"control": (self.N, 30), "three_fields": (self.N, 28)}, spread_days=7)
        self.assertNotIn("single day", " ".join(week["verdict"]["reasons"]))

    def test_progress_line_reads_sensibly_past_the_target(self):
        text = report.scorecard_text(self.card({"control": (self.N, 30), "three_fields": (self.N, 28)}))
        self.assertIn("planned sample reached", text)
        self.assertNotIn("(100%)", text)

    def test_plan_text_states_exposure(self):
        self.assertIn("puts 50% of all users on a new version", report.plan_text(spec.parse(h.PLAN)))
        wide = copy.deepcopy(h.PLAN)
        wide["variants"] = [{"key": "b", "value": "b", "share": 45}, {"key": "c", "value": "c", "share": 45}]
        self.assertIn("a person has to start it", report.plan_text(spec.parse(wide)))


class ProposalTests(unittest.TestCase):
    def test_expiry_can_be_lengthened_within_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = decisions.Ledger(Path(folder))
            default = ledger.propose(winning_card(), "env")
            longer = ledger.propose(winning_card(), "env", valid_hours=72)
            span = lambda d: (spec.parse_time(d["expires_at"]) - spec.parse_time(d["proposed_at"])).total_seconds() / 3600
            self.assertEqual((round(span(default)), round(span(longer))), (24, 72))
            for bad in (0, 0.5, 169, 10000):
                with self.assertRaises(decisions.DecisionError):
                    ledger.propose(winning_card(), "env", valid_hours=bad)

    def test_note_is_shown_back(self):
        with tempfile.TemporaryDirectory() as folder:
            decision = decisions.Ledger(Path(folder)).propose(winning_card(), "env", note="guardrail leans worse")
            self.assertIn("guardrail leans worse", report.decision_text(decision))

    def test_mcp_tool_names_work_as_commands(self):
        parser = cli.build_parser()
        for alias, command in (("check_plan", "plan"), ("apply_plan", "apply"), ("start_experiment", "start"), ("propose_rollout", "propose"), ("cleanup_report", "cleanup")):
            args = parser.parse_args([alias] + (["x"] if alias != "check_plan" else ["x.json"]))
            self.assertEqual(args.func.__name__, "cmd_" + command)


if __name__ == "__main__":
    unittest.main()


class PreviewTests(unittest.TestCase):
    """`referee preview` lets one test identity see a chosen version."""

    def setUp(self):
        from tests.test_core import FakeFlagsmith
        from tests.test_review import FakeSettings
        from referee import ops

        self.ops = ops
        self.folder = tempfile.TemporaryDirectory()
        self.client = FakeFlagsmith()
        self.settings = FakeSettings(self.folder.name, self.client)
        ops.apply_plan(self.settings, h.PLAN)
        self.overrides = {}
        client = self.client
        client.identity_id = lambda identifier, create=False: 900 if (create or identifier in self.overrides) else None
        client.identity_overrides = lambda identity_id, feature_id: list(self.overrides.values())

        def set_override(identity_id, feature_id, value, shares):
            self.overrides["referee-preview"] = {"id": 1, "value": value, "shares": shares}
            return {"id": 1}

        client.set_identity_override = set_override
        client.delete_identity_override = lambda identity_id, override_id: self.overrides.clear()
        client.identity_flags = lambda identifier: {"flags": [{
            "feature": {"name": "signup_form"},
            "feature_state_value": "three_fields" if any(v == 100.0 for v in self.overrides.get("referee-preview", {}).get("shares", {}).values()) else "six_fields",
        }]}

    def tearDown(self):
        self.folder.cleanup()

    def test_variant_and_control_can_be_previewed_and_cleared(self):
        result = self.ops.preview(self.settings, 7, "three_fields")
        self.assertEqual(result["serves"], "three_fields")
        self.assertEqual(list(self.overrides["referee-preview"]["shares"].values()), [100.0])
        result = self.ops.preview(self.settings, 7, "control")
        self.assertEqual(result["serves"], "six_fields")
        self.assertEqual(self.overrides["referee-preview"]["value"], "six_fields")
        cleared = self.ops.preview(self.settings, 7, clear=True)
        self.assertTrue(cleared["cleared"])
        self.assertEqual(self.overrides, {})

    def test_only_preview_identities_can_be_overridden(self):
        for name in ("visitor-123", "alice@example.com", "x-referee-preview", "referee-preview/../x", "referee-preview" + "a" * 200):
            with self.assertRaises(self.ops.OpError):
                self.ops.preview(self.settings, 7, "three_fields", identity=name)
        self.assertEqual(self.overrides, {})

    def test_unknown_version_is_refused_with_the_choices(self):
        with self.assertRaises(self.ops.OpError) as caught:
            self.ops.preview(self.settings, 7, "bogus")
        self.assertIn("control, three_fields", str(caught.exception))

    def test_preview_changes_nothing_about_the_experiment(self):
        before = dict(self.client.experiment(7)["experiment_rollout"])
        calls = list(self.client.calls)
        self.ops.preview(self.settings, 7, "three_fields")
        self.assertEqual(self.client.experiment(7)["experiment_rollout"], before)
        self.assertEqual(self.client.calls, calls)


class VoidTests(unittest.TestCase):
    """A spoiled run can be ended without a result, but only by a person."""

    def setUp(self):
        from tests.test_core import FakeFlagsmith

        self.folder = tempfile.TemporaryDirectory()
        self.ledger = decisions.Ledger(Path(self.folder.name))
        self.human = FakeFlagsmith()

    def tearDown(self):
        self.folder.cleanup()

    def broken(self):
        # The variant cannot be completed: no signups at all, many errors.
        return scorecard.judge(
            h.experiment(), h.results({"control": (1200, 260), "three_fields": (1200, 0)},
                                      errors={"control": (1200, 12), "three_fields": (1200, 350)}),
            h.exposures(), spec.parse(h.PLAN), now=h.NOW,
        )

    def test_broken_variant_is_investigate_with_the_next_steps(self):
        card = self.broken()
        self.assertEqual(card["verdict"]["call"], "investigate")
        text = " ".join(card["verdict"]["reasons"])
        self.assertIn("points at the version itself failing", text)
        self.assertIn("referee preview 7 three_fields", text)
        self.assertEqual(card["metrics"][0]["arms"]["three_fields"]["state"], "not computed")
        shown = report.scorecard_text(card)
        self.assertTrue(shown.splitlines()[1].strip().startswith("Call: INVESTIGATE"))
        self.assertNotIn("0.0%*", shown)

    def test_a_rollout_cannot_be_proposed_but_a_void_can(self):
        with self.assertRaises(decisions.DecisionError) as caught:
            self.ledger.propose(self.broken(), "env")
        self.assertIn("void", str(caught.exception))
        decision = self.ledger.propose(self.broken(), "env", void_reason="three-field form could not be submitted")
        self.assertEqual((decision["action"], decision["status"], decision["variant_id"]), ("void", "pending", None))
        self.assertIn("could not be submitted", decision["reasons"][0])
        self.assertIsNone(self.human.completed)

    def test_void_needs_a_reason_and_an_untrustworthy_run(self):
        with self.assertRaises(decisions.DecisionError):
            self.ledger.propose(self.broken(), "env", void_reason="  ")
        with self.assertRaises(decisions.DecisionError) as caught:
            self.ledger.propose(winning_card(), "env", void_reason="I would rather not wait")
        self.assertIn("only be ended without a result when the call is 'investigate'", str(caught.exception))

    def test_approved_void_completes_with_control_and_names_the_person(self):
        decision = self.ledger.propose(self.broken(), "env", void_reason="variant broken")
        applied = self.ledger.approve(decision["id"], self.human, lambda _id: self.broken())
        self.assertEqual(self.human.completed, (7, "control", None))
        self.assertEqual(applied["approval"]["by"], "human@example.com")

    def test_void_is_refused_once_the_run_is_healthy_again(self):
        decision = self.ledger.propose(self.broken(), "env", void_reason="variant broken")
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        self.assertIsNone(self.human.completed)

    def test_agent_key_cannot_approve_a_void(self):
        decision = self.ledger.propose(self.broken(), "env", void_reason="variant broken")
        self.human.user = None
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(decision["id"], self.human, lambda _id: self.broken())
        self.assertIsNone(self.human.completed)
