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
