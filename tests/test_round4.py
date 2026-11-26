"""Regression tests for the fourth verification round (rollout lifecycle edge cases)."""

import copy
import tempfile
import unittest

from referee import ops, provision, spec
from referee.api import FlagsmithError
from tests import helpers as h
from tests.test_core import FakeFlagsmith
from tests.test_review import FakeSettings

WIDE = copy.deepcopy(h.PLAN)
WIDE["variants"] = [{"key": "b", "value": "b", "share": 45}, {"key": "c", "value": "c", "share": 45}]


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.client = FakeFlagsmith()
        self.settings = FakeSettings(self.folder.name, self.client)

    def tearDown(self):
        self.folder.cleanup()

    def row(self):
        return self.client.experiment(7)

    def live(self):
        return self.row()["experiment_rollout"]["rollout_percentage"]

    def test_cap_applies_when_restoring_a_running_experiment_at_zero(self):
        ops.apply_plan(self.settings, WIDE)
        ops.start(self.settings, 7)                      # a person, no cap: 90% exposed
        self.row()["experiment_rollout"]["rollout_percentage"] = 0   # someone zeroes it in Flagsmith, still "running"
        refused = ops.start(self.settings, 7, max_exposure=ops.AGENT_MAX_EXPOSURE)
        self.assertFalse(refused["started"])
        self.assertIn("90%", refused["reason"])
        self.assertEqual(self.live(), 0)
        restored = ops.start(self.settings, 7)           # a person may
        self.assertTrue(restored["rollout_restored"])
        self.assertEqual(self.live(), 100)

    def test_small_experiment_at_zero_is_restored_and_says_so(self):
        ops.apply_plan(self.settings, h.PLAN)
        ops.start(self.settings, 7)
        self.row()["experiment_rollout"]["rollout_percentage"] = 0
        result = ops.start(self.settings, 7, max_exposure=ops.AGENT_MAX_EXPOSURE)
        self.assertTrue(result["rollout_restored"])
        self.assertEqual(result["rollout_percentage"], 100.0)

    def test_failed_raise_and_failed_pause_back_is_reported_truthfully(self):
        ops.apply_plan(self.settings, h.PLAN)

        def refuse(*_):
            raise FlagsmithError(400, "X", "/", "no")

        self.client.set_rollout_percentage = refuse
        self.client.pause_experiment = refuse
        with self.assertRaises(ops.OpError) as caught:
            ops.start(self.settings, 7)
        self.assertNotIn("paused again", str(caught.exception))
        self.assertIn("still marked running", str(caught.exception))

    def test_pause_zeroes_an_experiment_that_is_paused_but_serving(self):
        ops.apply_plan(self.settings, h.PLAN)
        self.row().update(status="paused", hypothesis="Made elsewhere.")
        self.row()["experiment_rollout"]["rollout_percentage"] = 40
        result = ops.pause(self.settings, 7)
        self.assertFalse(result["serving"])
        self.assertEqual(self.live(), 0)
        self.assertNotIn("pause", self.client.calls)     # Flagsmith's pause would be refused; it is not called
        self.assertEqual(spec.planned_rollout_from(self.row()["hypothesis"]), 40.0)

    def test_pause_keeps_the_rollout_if_it_cannot_be_recorded_first(self):
        ops.apply_plan(self.settings, h.PLAN)
        self.row().update(status="running", hypothesis="Made elsewhere.")
        self.row()["experiment_rollout"]["rollout_percentage"] = 40

        def refuse(*_):
            raise FlagsmithError(400, "PATCH", "/", "no")

        self.client.update_hypothesis = refuse
        result = ops.pause(self.settings, 7)
        self.assertTrue(result["serving"])
        self.assertEqual(self.live(), 40)
        self.assertIn("could not be recorded", result["warning"])

    def test_completed_experiment_cannot_be_paused(self):
        ops.apply_plan(self.settings, h.PLAN)
        self.row()["status"] = "completed"
        with self.assertRaises(ops.OpError):
            ops.pause(self.settings, 7)


class NoteAndSizingTests(unittest.TestCase):
    def test_rollout_note_round_trips_exactly(self):
        for value in (100, 50, 12.5, 33.33, 0.01, 99.99):
            self.assertEqual(spec.planned_rollout_from("h" + spec.marker(value, 500)), float(value))
            self.assertEqual(spec.planned_sample_from("h" + spec.marker(value, 500)), 500)

    def test_impossible_notes_are_ignored(self):
        for text in ("250", "0", "1e-05", "9" * 300):
            note = "h\n\n[Referee: rollout %s%%; no planned sample]" % text
            self.assertIsNone(spec.planned_rollout_from(note), text)

    def problems(self, **changes):
        data = copy.deepcopy(h.PLAN)
        data.update(changes)
        try:
            spec.parse(data)
        except spec.PlanError as error:
            return " ".join(error.problems)
        return ""

    def test_rollout_precision_is_limited_so_reapplying_matches(self):
        self.assertIn("two decimal places", self.problems(rollout_percentage=33.333333))
        self.assertIn("at least 0.01", self.problems(rollout_percentage=0.00001))
        self.assertEqual(self.problems(rollout_percentage=33.33), "")
        client = FakeFlagsmith()
        plan = spec.parse(dict(copy.deepcopy(h.PLAN), rollout_percentage=33.33))
        provision.apply(client, plan)
        provision.apply(client, plan)  # the identical plan must match, not be refused

    def test_planned_sample_cannot_be_made_trivially_small(self):
        easy = dict(copy.deepcopy(h.PLAN), assumptions={"baseline_rate": 0.4, "minimum_lift": 1.4, "daily_identities": 1000})
        sizing = spec.parse(easy).sizing()
        self.assertEqual(sizing["per_arm"], spec.MIN_PLANNED_SAMPLE)
        self.assertTrue(any("held at" in note for note in sizing["notes"]))
        self.assertIn("assumptions.power", self.problems(assumptions={"baseline_rate": 0.2, "minimum_lift": 0.2, "power": 0.5}))


if __name__ == "__main__":
    unittest.main()
