"""Builders for the Flagsmith payloads the tests feed to Referee."""

from datetime import datetime, timedelta, timezone  # noqa: F401 (timedelta is used by tests)
from statistics import NormalDist
import math

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)

PLAN = {
    "name": "Signup form length",
    "hypothesis": "Fewer fields, more signups.",
    "flag": "signup_form",
    "control": {"value": "six_fields"},
    "variants": [{"key": "three_fields", "value": "three_fields", "share": 50}],
    "metrics": [
        {"name": "Signup completed", "event": "signup_completed", "goal": "increase"},
        {"name": "Signup error", "event": "signup_error", "goal": "not_increase"},
    ],
    "assumptions": {"baseline_rate": 0.2, "minimum_lift": 0.25, "daily_identities": 2000},
}


def inference(control_n, control_sum, n, total, lower_is_better=False):
    """What Flagsmith's engine reports for a conversion metric."""
    if min(control_n, n) < 50 or min(control_sum, total) < 5:
        return None
    mc, mt = control_sum / control_n, total / n
    vc = (control_sum - control_sum**2 / control_n) / (control_n - 1)
    vt = (total - total**2 / n) / (n - 1)
    lift = (mt - mc) / mc
    se = math.sqrt(vt / (n * mc**2) + mt**2 * vc / (control_n * mc**4))
    chance = NormalDist().cdf(lift / se)
    return {
        "lift": lift,
        "ci_low": lift - 1.96 * se,
        "ci_high": lift + 1.96 * se,
        "chance_to_win": 1 - chance if lower_is_better else chance,
    }


def experiment(status="running", variants=(("three_fields", 50.0),), started_hours_ago=10.0, keys=True, **extra):
    options = [
        {"id": 10 + i, "key": key if keys else None, "string_value": key, "type": "unicode"}
        for i, (key, _) in enumerate(variants)
    ]
    data = {
        "id": 7,
        "name": "Signup form length",
        "hypothesis": "Fewer fields, more signups.",
        "status": status,
        "feature": {"id": 3, "name": "signup_form", "initial_value": "six_fields", "multivariate_options": options},
        "metrics": [
            {"id": 1, "metric": 101, "metric_name": "Signup completed", "aggregation": "occurrence", "direction": "up", "expected_direction": "increase"},
            {"id": 2, "metric": 102, "metric_name": "Signup error", "aggregation": "occurrence", "direction": "down", "expected_direction": "not_increase"},
        ],
        "experiment_rollout": {
            "enabled": True,
            "rollout_percentage": 100.0,
            "feature_state_value": {"type": "string", "value": "six_fields"},
            "multivariate_feature_state_values": [
                {"multivariate_feature_option": 10 + i, "percentage_allocation": share}
                for i, (_, share) in enumerate(variants)
            ],
        },
        "started_at": None if status == "created" else (NOW - timedelta(hours=started_hours_ago)).isoformat(),
        "ended_at": None,
        "rollout_decision": None,
        "rollout_variant": None,
    }
    data.update(extra)
    return data


def results(arms, errors=None, as_of_minutes_ago=1.0, spread_days=0.0):
    """``arms`` maps a variant key to (identities, conversions)."""
    errors = errors or {key: (n, max(5, n // 50)) for key, (n, _) in arms.items()}

    def metric(metric_id, numbers, lower_is_better):
        control_n, control_sum = numbers["control"]
        return {
            "metric_id": metric_id,
            "variants": {k: {"n": n, "sum": float(s), "sum_squares": float(s)} for k, (n, s) in numbers.items()},
            "inference": {
                k: inference(control_n, control_sum, n, s, lower_is_better)
                for k, (n, s) in numbers.items()
                if k != "control"
            },
        }

    return {
        "as_of": (NOW - timedelta(minutes=as_of_minutes_ago)).isoformat(),
        "last_error_at": None,
        "is_final": False,
        "payload": {
            "srm_p_value": None,
            "metrics": [metric(101, arms, False), metric(102, errors, True)],
            "exposures_timeseries": {
                "granularity": "hour",
                # With spread_days the identities arrive evenly on that many separate days.
                "points": (
                    [{"bucket": NOW.isoformat(), "new_identities": {k: n for k, (n, _) in arms.items()}}]
                    if not spread_days
                    else [
                        {
                            "bucket": (NOW - timedelta(days=day)).isoformat(),
                            "new_identities": {
                                k: n // int(spread_days) + (n % int(spread_days) if day == 0 else 0)
                                for k, (n, _) in arms.items()
                            },
                        }
                        for day in range(int(spread_days))
                    ]
                ),
            },
        },
    }


def exposures(excluded=0):
    return {"payload": {"excluded_identities": excluded, "timeseries": {"points": []}}}
