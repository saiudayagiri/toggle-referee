import unittest

from referee import scorecard, spec
from tests import helpers as h


def judge(arms, plan=True, errors=None, experiment=None, excluded=0, warehouse=None, **kwargs):
    return scorecard.judge(
        experiment or h.experiment(**kwargs),
        h.results(arms, errors),
        h.exposures(excluded),
        spec.parse(h.PLAN) if plan else None,
        now=h.NOW,
        warehouse=warehouse,
    )


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.needed = spec.parse(h.PLAN).sizing()["per_arm"]

    def test_not_started(self):
        card = scorecard.judge(h.experiment(status="created"), {}, {}, spec.parse(h.PLAN), now=h.NOW)
        self.assertEqual(card["verdict"]["call"], "not_started")
        self.assertFalse(card["verdict"]["actionable"])

    def test_collecting_below_flagsmith_floor(self):
        card = judge({"control": (30, 6), "three_fields": (28, 9)})
        self.assertEqual(card["verdict"]["call"], "collecting")

    def test_clear_winner_with_planned_sample_ships(self):
        n = self.needed
        card = judge({"control": (n, int(n * 0.20)), "three_fields": (n, int(n * 0.27))})
        self.assertEqual(card["verdict"]["call"], "ship_variant")
        self.assertEqual(card["verdict"]["variant_key"], "three_fields")
        self.assertEqual(card["verdict"]["variant_id"], 10)
        self.assertTrue(card["verdict"]["actionable"])

    def test_early_lead_is_not_called(self):
        # 97% chance to win on a third of the planned sample: ahead, not done.
        card = judge({"control": (300, 60), "three_fields": (300, 80)})
        chance = card["metrics"][0]["arms"]["three_fields"]["inference"]["chance_to_win"]
        self.assertGreater(chance, 0.95)
        self.assertLess(chance, 0.999)
        self.assertEqual(card["verdict"]["call"], "keep_running")
        self.assertFalse(card["verdict"]["actionable"])

    def test_overwhelming_early_result_ships(self):
        card = judge({"control": (400, 60), "three_fields": (400, 140)})
        self.assertEqual(card["verdict"]["call"], "ship_variant")

    def test_planned_sample_without_winner_keeps_control(self):
        n = self.needed
        card = judge({"control": (n, int(n * 0.20)), "three_fields": (n, int(n * 0.205))})
        self.assertEqual(card["verdict"]["call"], "keep_control")
        self.assertTrue(card["verdict"]["actionable"])

    def test_losing_variant_keeps_control_even_early(self):
        card = judge({"control": (400, 120), "three_fields": (400, 70)})
        self.assertEqual(card["verdict"]["call"], "keep_control")

    def test_guardrail_blocks_a_winner(self):
        n = self.needed
        card = judge(
            {"control": (n, int(n * 0.20)), "three_fields": (n, int(n * 0.27))},
            errors={"control": (n, 10), "three_fields": (n, 60)},
        )
        self.assertEqual(card["verdict"]["call"], "keep_control")
        self.assertIn("guardrail", " ".join(card["verdict"]["reasons"]))

    def test_no_plan_winner_ships_with_caveat(self):
        card = judge({"control": (800, 160), "three_fields": (800, 230)}, plan=False)
        self.assertEqual(card["verdict"]["call"], "ship_variant")
        self.assertIn("No planned sample size", " ".join(card["verdict"]["reasons"]))

    def test_no_plan_no_winner_keeps_running(self):
        card = judge({"control": (800, 160), "three_fields": (800, 165)}, plan=False)
        self.assertEqual(card["verdict"]["call"], "keep_running")

    def test_completed_reports_what_shipped(self):
        experiment = h.experiment(status="completed", rollout_decision="variant", rollout_variant=10)
        card = judge({"control": (900, 180), "three_fields": (900, 250)}, experiment=experiment)
        self.assertEqual(card["verdict"]["call"], "completed")
        self.assertEqual(card["verdict"]["variant_key"], "three_fields")
        self.assertFalse(card["verdict"]["actionable"])


class DataQualityTests(unittest.TestCase):
    def test_sample_ratio_mismatch_blocks_the_call(self):
        # A 50/50 plan that delivered 1000 vs 800 exposures.
        card = judge({"control": (1000, 200), "three_fields": (800, 240)})
        self.assertTrue(card["sample_ratio"]["alarm"])
        self.assertEqual(card["verdict"]["call"], "investigate")

    def test_fair_split_passes(self):
        card = judge({"control": (1000, 200), "three_fields": (1020, 240)})
        self.assertFalse(card["sample_ratio"]["alarm"])
        self.assertTrue(card["sample_ratio"]["checked"])

    def test_split_is_checked_against_the_experiment_not_the_flag_default(self):
        # Flagsmith's own srm_p_value is null for these experiments; ours is not.
        card = judge({"control": (500, 100), "three_fields": (500, 110)})
        self.assertIsNone(card["sample_ratio"]["flagsmith_p_value"])
        self.assertIsNotNone(card["sample_ratio"]["p_value"])

    def test_uneven_planned_split_is_respected(self):
        experiment = h.experiment(variants=(("three_fields", 20.0),))
        card = judge({"control": (800, 160), "three_fields": (200, 44)}, experiment=experiment)
        self.assertFalse(card["sample_ratio"]["alarm"])

    def test_unkeyed_variant_fails(self):
        card = judge({"control": (500, 100)}, experiment=h.experiment(keys=False))
        self.assertEqual(card["verdict"]["call"], "investigate")
        self.assertIn("variant_keys", [c["id"] for c in card["checks"]])

    def test_exposures_for_a_version_outside_the_experiment(self):
        card = judge({"control": (500, 100), "three_fields": (500, 120), "3": (40, 0)})
        self.assertEqual(card["verdict"]["call"], "investigate")
        self.assertIn("unexpected_variant", [c["id"] for c in card["checks"]])

    def test_many_excluded_identities_block(self):
        card = judge({"control": (500, 100), "three_fields": (500, 120)}, excluded=200)
        self.assertEqual(card["verdict"]["call"], "investigate")

    def test_silent_experiment_points_at_the_events_url(self):
        card = scorecard.judge(
            h.experiment(started_hours_ago=1.0),
            h.results({"control": (0, 0)}),
            h.exposures(),
            spec.parse(h.PLAN),
            now=h.NOW,
            warehouse={"total_events_received": 0},
        )
        self.assertEqual(card["verdict"]["call"], "investigate")
        self.assertIn("/api/", " ".join(card["verdict"]["reasons"]))

    def test_just_started_experiment_is_not_called_silent(self):
        card = scorecard.judge(
            h.experiment(started_hours_ago=0.02),
            h.results({"control": (0, 0)}, as_of_minutes_ago=0.5),
            h.exposures(),
            spec.parse(h.PLAN),
            now=h.NOW,
            warehouse={"total_events_received": 0},
        )
        self.assertEqual(card["verdict"]["call"], "collecting")


class ThreeArmTests(unittest.TestCase):
    def test_threshold_tightens_with_more_variants(self):
        experiment = h.experiment(variants=(("b", 33.0), ("c", 33.0)))
        card = judge({"control": (700, 140), "b": (700, 175), "c": (700, 150)}, plan=False, experiment=experiment)
        self.assertAlmostEqual(card["thresholds"]["win"], 0.975)

    def test_best_of_two_winners_is_chosen(self):
        experiment = h.experiment(variants=(("b", 33.0), ("c", 33.0)))
        card = judge({"control": (900, 150), "b": (900, 215), "c": (900, 260)}, plan=False, experiment=experiment)
        self.assertEqual(card["verdict"]["variant_key"], "c")


class EvidenceTests(unittest.TestCase):
    def test_hash_ignores_time_but_not_data(self):
        arms = {"control": (500, 100), "three_fields": (500, 130)}
        first = judge(arms)
        second = scorecard.judge(h.experiment(), h.results(arms), h.exposures(), spec.parse(h.PLAN), now=h.NOW.replace(hour=13))
        changed = judge({"control": (500, 100), "three_fields": (500, 131)})
        self.assertEqual(first["evidence_hash"], second["evidence_hash"])
        self.assertNotEqual(first["evidence_hash"], changed["evidence_hash"])


if __name__ == "__main__":
    unittest.main()
