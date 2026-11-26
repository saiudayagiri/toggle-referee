"""Plain-text and Markdown rendering of plans, preflight runs and scorecards."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import scorecard as sc
from .spec import Plan

MARKS = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "info": "INFO"}
CALL_LABELS = {
    "not_started": "NOT STARTED",
    "collecting": "COLLECTING DATA",
    "investigate": "INVESTIGATE",
    "keep_running": "KEEP RUNNING",
    "ship_variant": "SHIP THE VARIANT",
    "keep_control": "KEEP CONTROL",
    "completed": "COMPLETED",
}


def _pct(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "n/a"
    # Never round a probability up to a flat 100% or down to 0%.
    if 0.999 < value < 1:
        return ">99.9%"
    if 0 < value < 0.001:
        return "<0.1%"
    return ("%." + str(digits) + "f%%") % (value * 100)


def _signed(value: Optional[float]) -> str:
    return "n/a" if value is None else "%+.1f%%" % (value * 100)


def _chance(numbers: Dict[str, Any]) -> str:
    """The chance the metric moved the way the experiment wants, for one arm."""
    chance = numbers.get("chance_good")
    if chance is None:
        return "-"
    # A star marks Referee's own comparison, used where Flagsmith has no inference.
    return _pct(chance) + ("*" if numbers.get("chance_source") == "difference" else "")


def _chance_title(metric: Dict[str, Any]) -> str:
    return "chance it held" if metric["role"] == "guardrail" else "chance to win"


def _table(headers: List[str], rows: List[List[str]]) -> List[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    def line(cells: List[str]) -> str:
        return "  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(row) for row in rows]
    return out


def plan_text(plan: Plan) -> str:
    sizing = plan.sizing()
    lines = [
        "Plan: %s" % plan.name,
        "  Hypothesis: %s" % plan.hypothesis,
        "  Flag:       %s (multivariate)" % plan.flag,
        "  Versions:   control = '%s' (%g%%)" % (plan.control_value, plan.control_share),
    ]
    for variant in plan.variants:
        lines.append("              %s = '%s' (%g%%)" % (variant.key, variant.value, variant.share))
    lines.append("  Audience:   %g%% of identities enter the experiment" % plan.rollout_percentage)
    exposed = plan.rollout_percentage * (100.0 - plan.control_share) / 100.0
    lines.append(
        "  Exposure:   starting it puts %g%% of all users on a new version%s"
        % (exposed, "" if exposed <= 50 else " (over 50%, so a person has to start it; an agent cannot)")
    )
    for metric in plan.metrics:
        role = "guardrail" if metric.is_guardrail else ("primary" if metric is plan.primary else "secondary")
        lines.append(
            "  Metric:     %s: event '%s' (%s), should %s [%s]"
            % (metric.name, metric.event, metric.aggregation, metric.goal.replace("_", " "), role)
        )
    lines.append("")
    lines.append("Sizing")
    a = plan.assumptions
    if sizing.get("per_arm"):
        lines.append(
            "  Assuming a baseline of %s and caring about a %s of %s or more:"
            % (
                _pct(a.baseline_rate),
                "fall" if plan.primary.goal == "decrease" else "lift",
                _pct(a.minimum_lift) if plan.primary.goal == "decrease" else _signed(a.minimum_lift),
            )
        )
        lines.append(
            "  %d identities per version (%d in the experiment) are enough to reach the %s"
            % (
                sizing["per_arm"], sizing["total_in_experiment"],
                _pct(sizing["win_threshold"], 2 if sizing["comparisons"] > 1 else 0),
            )
        )
        lines.append(
            "  chance-to-win bar %s of the time%s."
            % (
                _pct(a.power, 0),
                " (the bar is higher because %d variants are compared)" % sizing["comparisons"]
                if sizing["comparisons"] > 1
                else "",
            )
        )
        if sizing.get("days") is not None:
            lines.append(
                "  At %g identities a day that takes about %s."
                % (a.daily_identities, sc.duration(sizing["days"] * 24))
            )
        if sizing.get("finishes_before_deadline") is True:
            lines.append("  That fits before the deadline (%s away)." % sc.duration(sizing["hours_until_deadline"]))
        elif sizing.get("finishes_before_deadline") is False:
            lift = sizing.get("detectable_lift_by_deadline")
            lines.append(
                "  WARNING: the deadline is %s away. Only %d per version fit%s."
                % (
                    sc.duration(sizing["hours_until_deadline"]),
                    sizing.get("identities_per_arm_by_deadline", 0),
                    ", enough to detect a lift of about %s or more" % _signed(lift) if lift else "",
                )
            )
    for note in sizing.get("notes", []):
        lines.append("  " + note)
    return "\n".join(lines)


def checks_text(checks: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for check in checks:
        lines.append("  [%s] %s" % (MARKS.get(check["status"], check["status"].upper()), check["title"]))
        if check.get("detail"):
            lines.append("         %s" % check["detail"])
        if check.get("fix"):
            lines.append("         Fix: %s" % check["fix"])
        for evidence in (check.get("evidence") or [])[:3]:
            lines.append("         %s:%s  %s" % (evidence["file"], evidence["line"], evidence["text"]))
    return lines


def preflight_text(result: Dict[str, Any]) -> str:
    experiment = result["experiment"]
    lines = [
        "Preflight: %s (experiment %s, flag '%s', %s)"
        % (experiment["name"], experiment["id"], experiment["flag"], experiment["status"]),
    ]
    if result.get("code_root"):
        lines.append("  Code scanned: %s (%s source files%s)" % (
            result["code_root"], result.get("files_scanned"),
            ", %d skipped as too large, unreadable or not regular files" % result["files_skipped"] if result.get("files_skipped") else "",
        ))
    else:
        lines.append("  Code not scanned (pass --code PATH to check the instrumentation).")
    lines.append("")
    lines += checks_text(result["checks"])
    lines.append("")
    lines.append(result["summary"])
    return "\n".join(lines)


def scorecard_text(card: Dict[str, Any]) -> str:
    experiment = card["experiment"]
    verdict = card["verdict"]
    lines = [
        "Scorecard: %s (experiment %s, flag '%s', %s)"
        % (experiment["name"], experiment["id"], experiment["flag"], experiment["status"]),
    ]
    if experiment.get("hypothesis"):
        lines.append("  Hypothesis: %s" % experiment["hypothesis"])
    freshness = card["freshness"]
    if freshness.get("as_of"):
        age = freshness.get("age_minutes")
        lines.append(
            "  Flagsmith results as of %s%s"
            % (
                freshness["as_of"],
                " (%s ago)" % sc.duration(age / 60.0) if age is not None and not freshness.get("is_final") else " (final)" if freshness.get("is_final") else "",
            )
        )
    if freshness.get("note"):
        lines.append("  " + freshness["note"])
    lines.append("")

    rows = []
    for arm in card["arms"]:
        rows.append(
            [
                arm["key"],
                "'%s'" % arm["value"],
                str(arm["identities"]),
                _pct(arm["expected_share"], 0) if arm["expected_share"] is not None else "-",
                _pct(arm["observed_share"]) if arm["observed_share"] is not None else "-",
            ]
        )
    lines.append("Versions")
    lines += _table(["version", "serves", "identities", "planned", "observed"], rows)
    lines.append("")

    for metric in card["metrics"]:
        label = "%s  [%s, %s, should %s]" % (
            metric["name"], metric["role"], metric["aggregation"], metric["goal"].replace("_", " "),
        )
        lines.append(label)
        rows = []
        for arm in card["arms"]:
            numbers = metric["arms"].get(arm["key"], {})
            inference = numbers.get("inference") or {}
            value = numbers.get("value")
            if metric["aggregation"] == "occurrence":
                shown = "%s (%d/%d)" % (_pct(value), numbers.get("sum", 0), numbers.get("identities", 0)) if value is not None else "-"
            else:
                shown = "%.3f" % value if value is not None else "-"
            if arm["key"] == "control":
                rows.append([arm["key"], shown, "baseline", "", "", ""])
            else:
                rows.append(
                    [
                        arm["key"],
                        shown,
                        _signed(inference.get("lift")) if inference else "-",
                        "%s to %s" % (_signed(inference.get("ci_low")), _signed(inference.get("ci_high"))) if inference else "-",
                        _chance(numbers),
                        numbers.get("state", ""),
                    ]
                )
        lines += _table(["version", "value", "lift", "95% interval", _chance_title(metric), "state"], rows)
        if any(a.get("chance_source") == "difference" for a in metric["arms"].values()):
            lines.append("  * Flagsmith has too little data for its own inference here; this is Referee's comparison of the plain difference.")
        lines.append("")

    progress = card["progress"]
    if progress.get("needed_per_arm"):
        if progress["smallest_arm"] >= progress["needed_per_arm"]:
            lines.append(
                "Progress: planned sample reached (%d identities in the smallest version, planned %d)"
                % (progress["smallest_arm"], progress["needed_per_arm"])
            )
        else:
            lines.append(
                "Progress: %d of %d identities in the smallest version (%s)%s"
                % (
                    progress["smallest_arm"],
                    progress["needed_per_arm"],
                    _pct(progress["fraction"], 0),
                    ", about %s to go" % sc.duration(progress["eta_hours"]) if progress.get("eta_hours") else "",
                )
            )
    elif experiment["status"] != "created":
        lines.append(
            "Progress: %d identities so far; no planned sample size on record."
            % progress["total_identities"]
        )
    if card["checks"]:
        lines.append("Checks")
        lines += checks_text(card["checks"])
    lines.append("")
    lines.append("CALL: %s. %s" % (CALL_LABELS.get(verdict["call"], verdict["call"]), verdict["headline"]))
    for reason in verdict["reasons"]:
        lines.append("  - %s" % reason)
    return "\n".join(lines)


def scorecard_markdown(card: Dict[str, Any]) -> str:
    """A scorecard for pull requests, issues and chat."""
    experiment = card["experiment"]
    verdict = card["verdict"]
    out = [
        "## %s: %s" % (CALL_LABELS.get(verdict["call"], verdict["call"]).title(), experiment["name"]),
        "",
        "**%s**" % verdict["headline"],
        "",
    ]
    out += ["- %s" % reason for reason in verdict["reasons"]]
    out += ["", "| Version | Serves | Identities | Planned | Observed |", "|---|---|---:|---:|---:|"]
    for arm in card["arms"]:
        out.append(
            "| %s | `%s` | %d | %s | %s |"
            % (
                arm["key"],
                arm["value"],
                arm["identities"],
                _pct(arm["expected_share"], 0) if arm["expected_share"] is not None else "-",
                _pct(arm["observed_share"]) if arm["observed_share"] is not None else "-",
            )
        )
    for metric in card["metrics"]:
        out += [
            "",
            "**%s** (%s, should %s)" % (metric["name"], metric["role"], metric["goal"].replace("_", " ")),
            "",
            "| Version | Value | Lift | 95%% interval | %s |" % _chance_title(metric).capitalize(),
            "|---|---:|---:|---:|---:|",
        ]
        for arm in card["arms"]:
            numbers = metric["arms"].get(arm["key"], {})
            inference = numbers.get("inference") or {}
            value = numbers.get("value")
            shown = "-" if value is None else (_pct(value) if metric["aggregation"] == "occurrence" else "%.3f" % value)
            if arm["key"] == "control":
                out.append("| %s | %s | - | - | - |" % (arm["key"], shown))
            elif not inference:
                out.append("| %s | %s | - | - | %s |" % (arm["key"], shown, _chance(numbers)))
            else:
                out.append(
                    "| %s | %s | %s | %s to %s | %s |"
                    % (
                        arm["key"], shown, _signed(inference.get("lift")),
                        _signed(inference.get("ci_low")), _signed(inference.get("ci_high")),
                        _chance(numbers),
                    )
                )
    if card["checks"]:
        out += ["", "**Checks**", ""]
        out += ["- %s: %s. %s" % (MARKS.get(c["status"], c["status"]), c["title"], c["detail"]) for c in card["checks"]]
    out += ["", "Evidence fingerprint: `%s`" % card["evidence_hash"][:16]]
    return "\n".join(out)


def decision_text(decision: Dict[str, Any]) -> str:
    lines = [
        "Decision %s: %s" % (decision["id"], decision["status"].upper()),
        "  Experiment: %s (id %s, flag '%s')" % (decision["experiment_name"], decision["experiment_id"], decision["flag"]),
        "  Proposal:   %s" % decision["headline"],
        "  Proposed:   %s by %s" % (decision["proposed_at"], decision["proposed_by"]),
    ]
    if decision["status"] == "pending":
        lines.append("  Expires:    %s" % decision["expires_at"])
    if decision.get("note"):
        lines.append("  Note:       %s" % decision["note"])
    for reason in decision.get("reasons", []):
        lines.append("  - %s" % reason)
    approval = decision.get("approval")
    if approval:
        lines.append("  Approved:   %s by %s (%s)" % (approval["at"], approval["by"], approval["method"]))
        if approval.get("evidence_changed_since_proposal"):
            lines.append("              Data moved between proposal and approval; the call was the same on both.")
    if decision.get("rejection"):
        lines.append("  Rejected:   %s by %s. %s" % (decision["rejection"]["at"], decision["rejection"]["by"], decision["rejection"].get("reason", "")))
    if decision.get("stale_reason"):
        lines.append("  Stale:      %s" % decision["stale_reason"])
    return "\n".join(lines)
