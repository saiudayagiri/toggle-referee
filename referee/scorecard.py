"""Read an experiment from Flagsmith and call it.

The scorecard is the one object everything else is built on: the CLI prints
it, the MCP server returns it, the dashboard draws it, and a rollout decision
stores a copy of it as the evidence a person approved.

Flagsmith's numbers (lift, credible interval, chance to beat control) are used
as they are. Referee adds the checks around them and one plain call:

* ``not_started``  the experiment has not been started
* ``collecting``   not enough data for Flagsmith to compute anything yet
* ``investigate``  the data cannot be trusted (for example a broken split)
* ``keep_running`` nothing is decided yet; includes how long it should take
* ``ship_variant`` a variant won on the primary metric and broke no guardrail
* ``keep_control`` the variants lost, or the planned sample found no difference
* ``completed``    the experiment is already finished in Flagsmith
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import stats
from .api import CONTROL_KEY, Flagsmith, FlagsmithError
from .provision import variant_value
from .spec import Plan, parse_time, planned_rollout_from, planned_sample_from, without_marker

# Ask Flagsmith for a recompute when its numbers are older than this.
STALE_AFTER_SECONDS = 60
SILENT_AFTER_MINUTES = 5
EXCLUDED_WARN_SHARE = 0.01
EXCLUDED_BLOCK_SHARE = 0.05
OVERWHELMING = 0.999
# A guardrail below this chance of having held is mentioned to the approver.
LEANING_WORSE = 0.25
# Without a planned sample size there is no stopping point fixed in advance, so
# a win is only called after a full weekly cycle (or when it is overwhelming).
UNPLANNED_MIN_DAYS = 7.0
# Fewest events (both arms together) for Referee's own fallback comparison.
FALLBACK_MIN_EVENTS = 10
# One arm at zero conversions while another has this many means broken tracking.
SILENT_ARM_MIN_EVENTS = 20


def build(
    client: Flagsmith,
    experiment_id: int,
    plan: Optional[Plan] = None,
    refresh: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fetch everything about one experiment and judge it."""
    experiment = client.experiment(experiment_id)
    results: Dict[str, Any] = {}
    exposures: Dict[str, Any] = {}
    refresh_note = ""
    if experiment.get("status") != "created":
        results = client.results(experiment_id)
        # A paused experiment keeps its data and can still be decided, so its
        # results are refreshed too.
        if refresh and experiment.get("status") in ("running", "paused") and _is_stale(results, now):
            try:
                results = client.fresh_results(experiment_id)
                retry_after = (results.get("refresh") or {}).get("retry_after")
                if retry_after:
                    refresh_note = (
                        "Flagsmith recomputes at most every five minutes; the next "
                        "refresh is possible in %d seconds." % retry_after
                    )
            except FlagsmithError as error:
                refresh_note = "Could not refresh results: %s" % error
        try:
            exposures = client.exposures(experiment_id)
        except FlagsmithError:
            exposures = {}
    warehouse = None
    if experiment.get("status") == "running":
        try:
            connections = client.warehouse_connections()
            warehouse = connections[0] if connections else {}
        except FlagsmithError:
            warehouse = None
    return judge(experiment, results, exposures, plan, now=now, note=refresh_note, warehouse=warehouse)


def _is_stale(results: Dict[str, Any], now: Optional[datetime]) -> bool:
    as_of = parse_time(results.get("as_of"))
    if as_of is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - as_of).total_seconds() > STALE_AFTER_SECONDS


def judge(
    experiment: Dict[str, Any],
    results: Dict[str, Any],
    exposures: Dict[str, Any],
    plan: Optional[Plan] = None,
    now: Optional[datetime] = None,
    note: str = "",
    warehouse: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Pure function from Flagsmith's data to a scorecard. No network."""
    now = now or datetime.now(timezone.utc)
    results = results or {}
    exposures = exposures or {}
    payload = results.get("payload") or {}
    feature = experiment.get("feature") or {}
    options = feature.get("multivariate_options") or []
    key_by_id = {o["id"]: o.get("key") for o in options}
    rollout = experiment.get("experiment_rollout") or {}
    status = experiment.get("status", "created")

    checks: List[Dict[str, str]] = []

    def check(check_id: str, state: str, title: str, detail: str) -> None:
        checks.append({"id": check_id, "status": state, "title": title, "detail": detail})

    # ---------------------------------------------------------------- arms
    # Flagsmith drops the rollout object once an experiment is completed, so
    # the configured split is then only known if the plan is on record.
    if rollout:
        shares = stats.expected_shares(rollout, {k: v for k, v in key_by_id.items() if v})
    elif plan is not None:
        shares = {CONTROL_KEY: plan.control_share / 100.0}
        shares.update({v.key: v.share / 100.0 for v in plan.variants})
    else:
        shares = {}
    shares_known = bool(shares)
    unkeyed = [o for o in options if not o.get("key") and _allocation(rollout, o["id"]) > 0]

    identities = _identities(payload, exposures)
    arm_keys = [CONTROL_KEY] + [k for k in shares if k != CONTROL_KEY]
    for key in identities:
        if key not in arm_keys:
            arm_keys.append(key)
    total = sum(identities.values())
    control_value = (rollout.get("feature_state_value") or {}).get("value")
    if control_value is None:
        control_value = plan.control_value if plan is not None else feature.get("initial_value", "")
    arms = []
    for key in arm_keys:
        option = next((o for o in options if o.get("key") == key), None)
        arms.append(
            {
                "key": key,
                "value": str(control_value) if key == CONTROL_KEY else (variant_value(option) if option else ""),
                "variant_id": option["id"] if option else None,
                "identities": identities.get(key, 0),
                "expected_share": shares.get(key),
                "observed_share": (identities.get(key, 0) / total) if total else None,
                # Without a known split, every arm that has data is taken to be part of the experiment.
                "in_experiment": bool(shares.get(key)) if shares_known else True,
            }
        )

    # ---------------------------------------------------------- data checks
    if unkeyed:
        check(
            "variant_keys",
            "fail",
            "A variant has no key",
            "%d variant(s) receive traffic but have no key, so their exposures are "
            "not recorded. Run `referee apply` again or set the key in Flagsmith." % len(unkeyed),
        )

    srm_arms = [a for a in arms if a["expected_share"]]
    srm_p = None
    if total >= stats.SRM_MIN_TOTAL and len(srm_arms) >= 2:
        srm_p = stats.srm_p_value(
            [a["identities"] for a in srm_arms], [a["expected_share"] for a in srm_arms]
        )
    unexpected = [a for a in arms if shares_known and not a["expected_share"] and a["identities"] > 0]
    srm = {
        "p_value": srm_p,
        "flagsmith_p_value": payload.get("srm_p_value"),
        "alarm": bool(srm_p is not None and srm_p < stats.SRM_ALARM),
        "checked": srm_p is not None,
    }
    if srm["alarm"]:
        check(
            "sample_ratio",
            "fail",
            "Traffic is not split as configured",
            "Expected %s but saw %s (p = %.2g). A split this far off almost never "
            "happens by chance, which usually means one version fails to record "
            "exposures (an error, a redirect, a slow page). The comparison is not "
            "trustworthy until this is explained."
            % (_split_text(srm_arms, "expected_share"), _split_text(srm_arms, "identities"), srm_p),
        )
    elif srm["checked"]:
        check(
            "sample_ratio",
            "pass",
            "Traffic split matches the plan",
            "Expected %s, saw %s (p = %.2f)."
            % (_split_text(srm_arms, "expected_share"), _split_text(srm_arms, "identities"), srm_p),
        )
    elif not shares_known and status != "created":
        check(
            "sample_ratio",
            "info",
            "The configured split is no longer on record",
            "Flagsmith does not keep an experiment's split after it is completed and no plan "
            "is stored here, so the split cannot be checked.",
        )
    elif status != "created":
        check(
            "sample_ratio",
            "info",
            "Too little traffic to check the split",
            "The split is checked once %d identities have been exposed (now %d)."
            % (stats.SRM_MIN_TOTAL, total),
        )
    if unexpected:
        check(
            "unexpected_variant",
            "fail",
            "Exposures recorded for a version outside the experiment",
            "Saw exposures for %s, which the experiment gives no traffic. The code "
            "is probably reporting a value instead of the variant key."
            % ", ".join("'%s' (%d)" % (a["key"], a["identities"]) for a in unexpected),
        )

    # A running experiment that has been live a while and still shows nobody
    # is not "waiting for traffic": its events are being lost somewhere
    # between the SDK and the warehouse. Time is measured to the last results
    # computation, or to now when Flagsmith has never computed any.
    started_at = parse_time(experiment.get("started_at"))
    computed_at = parse_time(results.get("as_of"))
    minutes_live = (((computed_at or now) - started_at).total_seconds() / 60.0) if started_at else 0.0
    if status == "running" and total == 0 and minutes_live >= SILENT_AFTER_MINUTES:
        received = (warehouse or {}).get("total_events_received")
        if warehouse is not None and not warehouse:
            check(
                "no_events", "fail", "No warehouse is connected",
                "The experiment is running but this environment has no warehouse, so events are discarded.",
            )
        elif warehouse is not None and not received:
            check(
                "no_events", "fail", "Flagsmith has received no events at all",
                "The experiment has been live for %s and the warehouse has not received a single event. "
                "The SDK is probably not sending: check that events are enabled (enable_events / enableEvents) "
                "and that the events URL is right. On self-hosted Flagsmith it is the API URL ending in /api/ "
                "(the SDK adds v1/events itself); a URL ending in /api/v1/ makes every event fail with a 405."
                % duration(minutes_live / 60.0),
            )
        else:
            check(
                "no_events", "fail", "Events arrive, but no exposures for this flag",
                "The experiment has been live for %s and the warehouse has events, yet nobody has been "
                "recorded as seeing '%s'. The code is probably reading the flag with a plain call instead of "
                "get_experiment_flag / getExperimentFlag, or under a different flag name%s."
                % (
                    duration(minutes_live / 60.0), feature.get("name"),
                    "" if computed_at else "; or Flagsmith's task processor is not running, since no results have ever been computed",
                ),
            )

    excluded = int((exposures.get("payload") or {}).get("excluded_identities") or 0)
    excluded_share = excluded / (total + excluded) if (total + excluded) else 0.0
    if excluded_share >= EXCLUDED_BLOCK_SHARE:
        check(
            "excluded",
            "fail",
            "Many identities saw more than one version",
            "%d identities (%.1f%%) were exposed to more than one version and are "
            "excluded. Assignment is not stable; check that the same identifier is "
            "used on every request." % (excluded, excluded_share * 100),
        )
    elif excluded_share >= EXCLUDED_WARN_SHARE:
        check(
            "excluded",
            "warn",
            "Some identities saw more than one version",
            "%d identities (%.1f%%) are excluded for seeing more than one version."
            % (excluded, excluded_share * 100),
        )

    # ------------------------------------------------------------- metrics
    treatment_keys = [a["key"] for a in arms if a["key"] != CONTROL_KEY and a["in_experiment"]]
    comparisons = max(1, len(treatment_keys))
    # More variants raise the bar for a win. The bar for harm stays fixed: a
    # safety check must not get weaker because more versions are being tried.
    threshold = stats.threshold_for(comparisons)
    harm_threshold = stats.LOSE_THRESHOLD
    result_by_metric = {m.get("metric_id"): m for m in payload.get("metrics") or []}
    metrics = []
    for attached in experiment.get("metrics") or []:
        goal = attached.get("expected_direction") or "increase"
        wants_lower = goal in ("decrease", "not_increase")
        # Flagsmith reports the chance that the metric moved in the *metric's*
        # good direction, which is down only for "lower is better" metrics.
        # The experiment's own expectation may differ, so everything below
        # works with the chance the metric moved the way the experiment wants.
        flagsmith_reports_down = attached.get("direction") == "down"
        result = result_by_metric.get(attached.get("metric")) or {}
        variants = result.get("variants") or {}
        inference = result.get("inference") or {}
        aggregation = attached.get("aggregation", "occurrence")
        control_numbers = variants.get(CONTROL_KEY) or {}
        per_arm = {}
        for arm in arms:
            key = arm["key"]
            numbers = variants.get(key) or {}
            n = int(numbers.get("n") or 0)
            total_value = float(numbers.get("sum") or 0.0)
            entry: Dict[str, Any] = {
                "identities": n,
                "sum": total_value,
                "value": (total_value / n) if n else None,
            }
            if key != CONTROL_KEY:
                found = inference.get(key) or None
                chance = None
                source = None
                if found and found.get("chance_to_win") is not None:
                    raw = float(found["chance_to_win"])
                    chance_up = (1.0 - raw) if flagsmith_reports_down else raw
                    chance = (1.0 - chance_up) if wants_lower else chance_up
                    source = "flagsmith"
                else:
                    # Flagsmith gives no inference below its floors (for
                    # example a rare event with under 5 conversions in an arm)
                    # or when control's mean is zero. The raw totals still say
                    # something, so judge the plain difference instead.
                    control_moments, moments = _moments(control_numbers), _moments(numbers)
                    enough = (
                        control_moments is not None
                        and moments is not None
                        and min(control_moments.n, moments.n) >= stats.MIN_IDENTITIES_PER_VARIANT
                        # With only a handful of events in total the normal
                        # approximation says nothing reliable either way.
                        and (aggregation != "occurrence" or control_moments.total + moments.total >= FALLBACK_MIN_EVENTS)
                    )
                    chance_up = stats.chance_higher(control_moments, moments) if enough else None
                    if chance_up is not None:
                        chance = (1.0 - chance_up) if wants_lower else chance_up
                        source = "difference"
                entry["inference"] = found
                entry["chance_good"] = chance
                entry["chance_source"] = source
                # Only Flagsmith's own inference can make a metric "winning"
                # or "losing"; the fallback is used for guardrails alone.
                entry["state"] = _state(chance if source == "flagsmith" else None, threshold, harm_threshold)
                if goal in ("not_increase", "not_decrease") and chance is not None:
                    # A guardrail is never "winning"; what matters is whether it held.
                    entry["state"] = (
                        "worse" if chance <= harm_threshold
                        else "leans worse" if chance < LEANING_WORSE
                        else "held"
                    )
            per_arm[key] = entry
        metrics.append(
            {
                "metric_id": attached.get("metric"),
                "name": attached.get("metric_name"),
                "aggregation": aggregation,
                "goal": goal,
                "role": "guardrail" if goal in ("not_increase", "not_decrease") else "goal",
                "lower_is_better": wants_lower,
                "arms": per_arm,
            }
        )
        if flagsmith_reports_down != wants_lower and attached.get("direction") in ("up", "down"):
            check(
                "direction:%s" % attached.get("metric"),
                "info",
                "Metric '%s' is used against its own direction" % attached.get("metric_name"),
                "In Flagsmith this metric is marked '%s is better', but this experiment expects it to %s. "
                "Flagsmith's dashboard labels it by the metric's direction; the chances shown here follow "
                "the experiment's expectation." % (
                    "lower" if flagsmith_reports_down else "higher", goal.replace("_", " "),
                ),
            )
    goal_metrics = [m for m in metrics if m["role"] == "goal"]
    primary = goal_metrics[0] if goal_metrics else None
    if primary is not None:
        primary["role"] = "primary"
    guardrails = [m for m in metrics if m["role"] == "guardrail"]

    # ------------------------------------------------------------ progress
    # The planned sample recorded in Flagsmith when the experiment was set up
    # is the target; a plan held locally only fills in when there is none.
    needed = planned_sample_from(experiment.get("hypothesis")) or _sizing(plan).get("per_arm")
    in_experiment = [a for a in arms if a["in_experiment"]]
    smallest = min((a["identities"] for a in in_experiment), default=0)
    ended = parse_time(experiment.get("ended_at"))
    hours_running = None
    if started_at is not None:
        hours_running = max(0.0, ((ended or now) - started_at).total_seconds() / 3600.0)
    eta_hours = None
    if needed and smallest < needed and hours_running and smallest > 0 and status == "running":
        eta_hours = (needed - smallest) / (smallest / hours_running)
    deadline = parse_time(plan.assumptions.deadline) if plan is not None else None
    hours_to_deadline = None
    if deadline is not None:
        hours_to_deadline = (deadline - now).total_seconds() / 3600.0
    progress = {
        "needed_per_arm": needed,
        "smallest_arm": smallest,
        "fraction": (min(1.0, smallest / needed) if needed else None),
        "hours_running": hours_running,
        "eta_hours": eta_hours,
        "deadline": plan.assumptions.deadline if plan is not None else None,
        "hours_to_deadline": hours_to_deadline,
        "will_miss_deadline": bool(
            eta_hours is not None and hours_to_deadline is not None and eta_hours > hours_to_deadline
        ),
        "total_identities": total,
        "excluded_identities": excluded,
        # Number of separate days that brought in a real share of the traffic.
        "days_of_data": _days_of_data(payload, exposures, now),
    }

    freshness = {
        "as_of": results.get("as_of"),
        "age_minutes": ((now - computed_at).total_seconds() / 60.0) if computed_at else None,
        "is_final": bool(results.get("is_final")),
        "last_error_at": results.get("last_error_at"),
        "note": note,
    }
    failed_at = parse_time(results.get("last_error_at"))
    if failed_at is not None and (computed_at is None or failed_at > computed_at):
        check(
            "results_error",
            "warn",
            "Flagsmith's last results computation failed",
            "The last attempt at %s failed, so these numbers may be older than they look."
            % results["last_error_at"],
        )

    verdict = _verdict(
        status=status,
        experiment=experiment,
        arms=arms,
        primary=primary,
        guardrails=guardrails,
        checks=checks,
        progress=progress,
        threshold=threshold,
        harm_threshold=harm_threshold,
        has_plan=plan is not None,
    )

    card: Dict[str, Any] = {
        "experiment": {
            "id": experiment.get("id"),
            "name": experiment.get("name"),
            "hypothesis": without_marker(experiment.get("hypothesis")),
            "status": status,
            "flag": feature.get("name"),
            "feature_id": feature.get("id"),
            "started_at": experiment.get("started_at"),
            "ended_at": experiment.get("ended_at"),
            # The percentage the experiment is meant to run at, and what is live now
            # (Referee holds the live value at 0% while it is not running).
            "rollout_percentage": planned_rollout_from(experiment.get("hypothesis")) or rollout.get("rollout_percentage"),
            "live_rollout_percentage": rollout.get("rollout_percentage"),
            "rollout_decision": experiment.get("rollout_decision"),
            "rollout_variant": experiment.get("rollout_variant"),
        },
        "arms": arms,
        "metrics": metrics,
        "sample_ratio": srm,
        "progress": progress,
        "freshness": freshness,
        "checks": checks,
        "thresholds": {
            "win": threshold,
            "harm": harm_threshold,
            "comparisons": comparisons,
        },
        "has_plan": plan is not None,
        "verdict": verdict,
        "generated_at": now.isoformat(timespec="seconds"),
    }
    card["evidence_hash"] = evidence_hash(card)
    return card


def _sizing(plan: Optional[Plan]) -> Dict[str, Any]:
    if plan is None:
        return {}
    try:
        return plan.sizing()
    except ValueError:
        return {}


def _moments(numbers: Dict[str, Any]) -> Optional[stats.Moments]:
    n = int(numbers.get("n") or 0)
    if n < 2:
        return None
    total = float(numbers.get("sum") or 0.0)
    squares = numbers.get("sum_squares")
    return stats.Moments(n=n, total=total, squares=float(squares) if squares is not None else total)


def _allocation(rollout: Dict[str, Any], option_id: int) -> float:
    for value in rollout.get("multivariate_feature_state_values") or []:
        if value.get("multivariate_feature_option") == option_id:
            return float(value.get("percentage_allocation") or 0)
    return 0.0


def _identities(payload: Dict[str, Any], exposures: Dict[str, Any]) -> Dict[str, int]:
    """Identities exposed per arm, preferring the results payload."""
    points = (payload.get("exposures_timeseries") or {}).get("points")
    if not points:
        points = ((exposures.get("payload") or {}).get("timeseries") or {}).get("points")
    return stats.totals_by_variant(points or [], "new_identities")


def _days_of_data(payload: Dict[str, Any], exposures: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """How many separate days brought the experiment a real share of its traffic.

    Counting days with data, instead of time between the first and last
    exposure, means neither a pause nor a single stray early visitor can
    stretch one day of data into "a week".
    """
    series = payload.get("exposures_timeseries") or {}
    if not series.get("points"):
        series = (exposures.get("payload") or {}).get("timeseries") or {}
    per_day: Dict[Any, int] = {}
    for point in series.get("points") or []:
        when = parse_time(point.get("bucket")) if isinstance(point, dict) else None
        if when is None or (now is not None and when > now):
            continue
        count = 0
        for value in (point.get("new_identities") or {}).values():
            try:
                count += int(value)
            except (TypeError, ValueError):
                continue
        if count > 0:
            per_day[when.date()] = per_day.get(when.date(), 0) + count
    total = sum(per_day.values())
    if not total:
        return 0.0
    # A day counts when it carried at least a fifth of an average day's traffic.
    floor = max(1.0, 0.2 * total / len(per_day))
    return float(sum(1 for count in per_day.values() if count >= floor))


def _split_text(arms: List[Dict[str, Any]], field: str) -> str:
    if field == "expected_share":
        return " / ".join("%s %.0f%%" % (a["key"], a[field] * 100) for a in arms)
    total = sum(a[field] for a in arms) or 1
    return " / ".join("%s %d (%.1f%%)" % (a["key"], a[field], a[field] * 100 / total) for a in arms)


def _state(chance: Optional[float], threshold: float, harm_threshold: float) -> str:
    if chance is None:
        return "collecting"
    if chance >= threshold:
        return "winning"
    if chance <= harm_threshold:
        return "losing"
    return "inconclusive"


def _verdict(
    status: str,
    experiment: Dict[str, Any],
    arms: List[Dict[str, Any]],
    primary: Optional[Dict[str, Any]],
    guardrails: List[Dict[str, Any]],
    checks: List[Dict[str, str]],
    progress: Dict[str, Any],
    threshold: float,
    harm_threshold: float,
    has_plan: bool,
) -> Dict[str, Any]:
    reasons: List[str] = []
    treatments = [a for a in arms if a["key"] != CONTROL_KEY and a["in_experiment"]]

    def result(call: str, headline: str, variant: Optional[Dict[str, Any]] = None, actionable: bool = False) -> Dict[str, Any]:
        if actionable and 0 < (progress.get("days_of_data") or 0) <= 1:
            reasons.append(
                "All of this data arrived within a single day, so it has not seen a full daily or weekly cycle. "
                "If behaviour differs by time of day or day of week, that is not in these numbers."
            )
        return {
            "call": call,
            "headline": headline,
            "variant_key": variant["key"] if variant else None,
            "variant_id": variant["variant_id"] if variant else None,
            "reasons": reasons,
            "actionable": actionable,
        }

    if status == "created":
        reasons.append("The experiment exists in Flagsmith but has not been started.")
        return result("not_started", "Not started yet.")

    if status == "completed":
        decision = experiment.get("rollout_decision")
        shipped_id = experiment.get("rollout_variant")
        if decision == "variant":
            shipped = next((a for a in arms if shipped_id is not None and a["variant_id"] == shipped_id), None)
            if shipped:
                reasons.append("Flagsmith now serves '%s' to everyone from the same flag." % shipped["key"])
                return result("completed", "Completed: '%s' was rolled out." % shipped["key"], shipped)
            reasons.append("Flagsmith recorded that a variant (id %s) was rolled out; it is no longer listed on the flag." % shipped_id)
            return result("completed", "Completed: a variant was rolled out.")
        if decision == "control":
            reasons.append("Flagsmith serves the control version to everyone.")
            return result("completed", "Completed: control was kept.")
        if decision == "disable":
            reasons.append("Flagsmith recorded that the flag was disabled.")
            return result("completed", "Completed: the flag was disabled.")
        reasons.append("Flagsmith did not record a rollout decision for this experiment.")
        return result("completed", "Completed.")

    failures = [c for c in checks if c["status"] == "fail"]
    if failures:
        for failure in failures:
            reasons.append("%s. %s" % (failure["title"], failure["detail"]))
        return result("investigate", "Do not call this yet: the data cannot be trusted.")

    if primary is None:
        reasons.append(
            "The experiment has no goal metric (one expected to increase or decrease), only guardrails, "
            "so there is nothing a variant could win on."
            if guardrails
            else "The experiment has no metric attached, so there is nothing to judge."
        )
        return result("investigate", "No goal metric to judge.")

    def entry_of(metric: Dict[str, Any], treatment: Dict[str, Any]) -> Dict[str, Any]:
        return metric["arms"].get(treatment["key"]) or {}

    states = {t["key"]: entry_of(primary, t).get("state", "collecting") for t in treatments}
    if not treatments or all(state == "collecting" for state in states.values()):
        in_test = [a for a in arms if a["in_experiment"]]
        enough_people = bool(in_test) and all(
            (primary["arms"].get(a["key"]) or {}).get("identities", 0) >= stats.MIN_IDENTITIES_PER_VARIANT for a in in_test
        )
        if enough_people and primary["aggregation"] == "occurrence":
            counts = {a["key"]: int((primary["arms"].get(a["key"]) or {}).get("sum") or 0) for a in in_test}
            silent = [key for key, count in counts.items() if count == 0]
            busiest = max(counts.values())
            if silent and busiest >= SILENT_ARM_MIN_EVENTS:
                # One version converts and another records nothing at all.
                # Waiting will not fix that; the tracking in that version is broken.
                reasons.append(
                    "%s recorded no '%s' events at all, while another version recorded %d. That is almost "
                    "certainly broken tracking in that version's code path, not a real difference, and it "
                    "will not resolve by waiting."
                    % (" and ".join("'%s'" % key for key in silent), primary["name"], busiest)
                )
                return result("investigate", "Do not call this yet: one version records no conversions.")
            reasons.append(
                "Every version has enough identities, but Flagsmith also needs %d conversions in each before "
                "it computes a result. Now: %s."
                % (stats.MIN_CONVERSIONS_PER_VARIANT, ", ".join("%s %d" % item for item in counts.items()))
            )
            _add_pace(reasons, progress)
            return result("collecting", "Collecting data.")
        reasons.append(
            "Flagsmith needs at least %d identities in every version%s before it computes a result."
            % (
                stats.MIN_IDENTITIES_PER_VARIANT,
                " and %d conversions in each" % stats.MIN_CONVERSIONS_PER_VARIANT
                if primary["aggregation"] == "occurrence"
                else "",
            )
        )
        reasons.append(
            "Now: %s." % ", ".join("%s %d" % (a["key"], a["identities"]) for a in arms if a["in_experiment"])
        )
        _add_pace(reasons, progress)
        return result("collecting", "Collecting data.")

    # A variant that confidently damages a guardrail cannot win. A guardrail
    # Flagsmith could not compute is judged on the plain difference; one that
    # cannot be judged at all is said so, never passed silently.
    blocked: Dict[str, List[str]] = {}
    blocked_chance: Dict[str, float] = {}
    unjudged: Dict[str, List[str]] = {}
    for guardrail in guardrails:
        for treatment in treatments:
            entry = entry_of(guardrail, treatment)
            chance = entry.get("chance_good")
            if chance is None:
                unjudged.setdefault(treatment["key"], []).append(guardrail["name"])
            elif chance <= harm_threshold:
                blocked_chance[treatment["key"]] = min(chance, blocked_chance.get(treatment["key"], 1.0))
                blocked.setdefault(treatment["key"], []).append(
                    "guardrail '%s' got worse (%s for the variant against %s for control, %s chance it held)"
                    % (
                        guardrail["name"],
                        _value(guardrail, entry),
                        _value(guardrail, guardrail["arms"].get(CONTROL_KEY) or {}),
                        _percent(chance),
                    )
                )

    def chance_of(treatment: Dict[str, Any]) -> float:
        return float(entry_of(primary, treatment).get("chance_good") or 0.0)

    def lift_of(treatment: Dict[str, Any]) -> float:
        return float((entry_of(primary, treatment).get("inference") or {}).get("lift") or 0.0)

    def comparison(treatment: Dict[str, Any], so_far: bool = False) -> str:
        inference = entry_of(primary, treatment).get("inference") or {}
        return "'%s' vs control on '%s': %s lift%s (95%% interval %s to %s), %s chance to beat control (needs %s)." % (
            treatment["key"], primary["name"], _signed(inference.get("lift")), " so far" if so_far else "",
            _signed(inference.get("ci_low")), _signed(inference.get("ci_high")),
            _percent(chance_of(treatment)), _percent(threshold),
        )

    winners = [t for t in treatments if states[t["key"]] == "winning" and t["key"] not in blocked]
    winners.sort(key=lambda t: (chance_of(t), abs(lift_of(t))), reverse=True)
    fraction = progress.get("fraction")
    reached = fraction is not None and fraction >= 1.0
    # A deadline is for planning and warnings only. It never changes the call:
    # running out of time does not make a partial sample conclusive.
    deadline_passed = progress.get("hours_to_deadline") is not None and progress["hours_to_deadline"] <= 0
    days_of_data = progress.get("days_of_data") or 0.0

    for key, why in blocked.items():
        reasons.append("'%s' cannot be shipped: %s." % (key, "; ".join(why)))

    if winners:
        best = winners[0]
        inference = entry_of(primary, best).get("inference") or {}
        reasons.append(
            "'%s' beats control on '%s': %s lift (95%% interval %s to %s), %s chance to beat control (needs %s)."
            % (
                best["key"], primary["name"], _signed(inference.get("lift")),
                _signed(inference.get("ci_low")), _signed(inference.get("ci_high")),
                _percent(chance_of(best)), _percent(threshold),
            )
        )
        if best["key"] in unjudged:
            reasons.append(
                "Guardrail(s) %s could not be judged for '%s' (too little data), so they neither pass nor fail. "
                "Look at them before approving."
                % (", ".join("'%s'" % name for name in unjudged[best["key"]]), best["key"])
            )
        judged = [g for g in guardrails if g["name"] not in unjudged.get(best["key"], [])]
        if judged:
            reasons.append("No guardrail metric got reliably worse for '%s'." % best["key"])
            for guardrail in judged:
                entry = entry_of(guardrail, best)
                chance = entry.get("chance_good")
                if chance is not None and chance < LEANING_WORSE:
                    reasons.append(
                        "Worth a look before approving: guardrail '%s' leans worse for '%s' "
                        "(%s against %s for control, only %s chance it held). That is not conclusive, but it is not nothing."
                        % (
                            guardrail["name"], best["key"], _value(guardrail, entry),
                            _value(guardrail, guardrail["arms"].get(CONTROL_KEY) or {}), _percent(chance),
                        )
                    )
        overwhelming = chance_of(best) >= OVERWHELMING
        if fraction is None:
            # No stopping point was fixed in advance. Calling the first lead
            # that crosses the bar is exactly how lucky streaks get shipped.
            if overwhelming:
                reasons.append(
                    "No planned sample size is on record, but the result is overwhelming (%s), so waiting adds little."
                    % _percent(chance_of(best))
                )
                return result("ship_variant", "Ship '%s'." % best["key"], best, actionable=True)
            if days_of_data >= UNPLANNED_MIN_DAYS:
                reasons.append(
                    "No planned sample size is on record. Data has come in on %d separate days, which covers a full weekly cycle."
                    % int(days_of_data)
                )
                return result("ship_variant", "Ship '%s'." % best["key"], best, actionable=True)
            reasons.append(
                "It is ahead, but no planned sample size is on record%s, so no stopping point was fixed in "
                "advance. Referee waits for data on %d separate days (now %s) or an overwhelming result (%s) before "
                "calling a win like this. Apply a plan with sizing assumptions to get a proper target."
                % (
                    " (the plan has no usable sizing assumptions)" if has_plan else "",
                    int(UNPLANNED_MIN_DAYS), "%d day%s" % (int(days_of_data), "" if int(days_of_data) == 1 else "s"), _percent(OVERWHELMING),
                )
            )
            return result("keep_running", "'%s' is ahead; keep running." % best["key"], best)
        if reached or overwhelming:
            if reached:
                reasons.append(
                    "The planned sample is in: %d identities in the smallest version (planned %d)."
                    % (progress["smallest_arm"], progress["needed_per_arm"])
                )
            else:
                reasons.append(
                    "Only %s of the planned sample is in, but the result is overwhelming (%s), so waiting adds little."
                    % (_percent(fraction), _percent(chance_of(best)))
                )
            return result("ship_variant", "Ship '%s'." % best["key"], best, actionable=True)
        reasons.append(
            "It is ahead, but only %s of the planned sample is in (%d of %d per version). "
            "Calling it now risks shipping a lucky streak."
            % (_percent(fraction), progress["smallest_arm"], progress["needed_per_arm"])
        )
        _add_pace(reasons, progress)
        return result("keep_running", "'%s' is ahead; keep running." % best["key"], best)

    shippable = [t for t in treatments if t["key"] not in blocked]
    all_lost = bool(treatments) and all(states[t["key"]] == "losing" for t in treatments)
    if all_lost or not shippable:
        for treatment in treatments:
            inference = entry_of(primary, treatment).get("inference")
            if inference and states[treatment["key"]] == "losing":
                reasons.append(
                    "'%s' is worse than control on '%s': %s lift, %s chance to beat control."
                    % (treatment["key"], primary["name"], _signed(inference.get("lift")), _percent(chance_of(treatment)))
                )
        # Ending an experiment early because a variant looks bad follows the
        # same discipline as ending it early for a win: the planned sample is
        # in, or a week of data without a plan, or the evidence is overwhelming.
        settled = reached or (fraction is None and days_of_data >= UNPLANNED_MIN_DAYS)
        decisive = all(
            (states[t["key"]] == "losing" and chance_of(t) <= 1 - OVERWHELMING)
            or blocked_chance.get(t["key"], 1.0) <= 1 - OVERWHELMING
            for t in treatments
        )
        if settled or decisive:
            if not settled:
                reasons.append("The sample is still small, but the evidence against the variant is overwhelming, so waiting adds little.")
            return result("keep_control", "Keep control: no variant can be shipped.", actionable=True)
        if fraction is not None:
            reasons.append(
                "That is on %s of the planned sample (%d of %d per version), which is too little to end the "
                "experiment on. If you want to stop exposing users to it meanwhile, pause it."
                % (_percent(fraction), progress["smallest_arm"], progress["needed_per_arm"])
            )
            _add_pace(reasons, progress)
        else:
            reasons.append(
                "There is no planned sample size and data on only %d separate day(s), which is too little to end "
                "the experiment on. If you want to stop exposing users to it meanwhile, pause it." % int(days_of_data)
            )
        return result("keep_running", "The variant is behind; keep running.")

    if reached:
        for treatment in shippable:
            if entry_of(primary, treatment).get("inference"):
                reasons.append(comparison(treatment))
        reasons.append(
            "The planned sample is in (%d of %d per version) and no variant reached the bar. "
            "If a difference of the size the plan cared about existed, this would probably have found it."
            % (progress["smallest_arm"], progress["needed_per_arm"])
        )
        return result("keep_control", "Keep control: no variant proved better.", actionable=True)

    for treatment in shippable:
        if entry_of(primary, treatment).get("inference"):
            reasons.append(comparison(treatment, so_far=True))
    if deadline_passed:
        reasons.append(
            "The plan's deadline has passed. That does not make the sample conclusive; a person can still "
            "decide to stop the experiment in Flagsmith."
        )
    if fraction is None:
        reasons.append(
            "Nothing has won yet and there is no planned sample size, so there is no principled stopping "
            "point. Apply a plan with sizing assumptions, or keep running."
        )
    else:
        reasons.append(
            "%s of the planned sample is in (%d of %d per version)."
            % (_percent(fraction), progress["smallest_arm"], progress["needed_per_arm"])
        )
        _add_pace(reasons, progress)
    return result("keep_running", "No winner yet; keep running.")


def _value(metric: Dict[str, Any], entry: Dict[str, Any]) -> str:
    value = entry.get("value")
    if value is None:
        return "no data"
    if metric["aggregation"] == "occurrence":
        return "%.1f%%" % (value * 100)
    return "%.3g" % value


def _add_pace(reasons: List[str], progress: Dict[str, Any]) -> None:
    eta = progress.get("eta_hours")
    if eta is None:
        return
    reasons.append("At the current pace the planned sample arrives in about %s." % duration(eta))
    if progress.get("will_miss_deadline"):
        reasons.append(
            "That is after the deadline (%s away). Widen the rollout, send more "
            "traffic, or accept a larger minimum lift."
            % duration(max(0.0, progress["hours_to_deadline"]))
        )


def duration(hours: float) -> str:
    if hours * 60 < 1:
        return "under a minute"
    if hours < 1:
        minutes = int(round(hours * 60))
        return "1 minute" if minutes == 1 else "%d minutes" % minutes
    if hours < 48:
        return "%.1f hours" % hours
    return "%.1f days" % (hours / 24.0)


def _percent(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if 0.999 < value < 1:
        return ">99.9%"
    if 0 < value < 0.001:
        return "<0.1%"
    return "%.1f%%" % (value * 100)


def _signed(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return "%+.1f%%" % (value * 100)


def evidence_hash(card: Dict[str, Any]) -> str:
    """A fingerprint of the facts a decision rests on.

    Covers the experiment, the per-arm numbers and the call, but not the time
    the scorecard was produced, so two scorecards of unchanged data match.
    """
    facts = {
        "experiment": {k: card["experiment"].get(k) for k in ("id", "name", "status", "flag")},
        "arms": [[a["key"], a["identities"], a["expected_share"]] for a in card["arms"]],
        "metrics": [
            [
                m["metric_id"],
                m["goal"],
                {
                    key: [arm["identities"], arm["sum"], arm.get("chance_good")]
                    for key, arm in sorted(m["arms"].items())
                },
            ]
            for m in card["metrics"]
        ],
        "verdict": [card["verdict"]["call"], card["verdict"]["variant_key"], card["verdict"]["variant_id"]],
    }
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
