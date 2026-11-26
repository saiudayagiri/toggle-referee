"""Checks to run before an experiment is started.

Most failed experiments fail quietly: a variant that records no exposures, a
metric listening for ``Signup`` while the code sends ``signup``, a page that
reads the flag without telling Flagsmith who saw what. Each of those costs days
of traffic before anyone notices. Preflight looks for them up front, in
Flagsmith and in the code, and says how to fix each one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import codescan, scorecard, stats
from .api import CONTROL_KEY, Flagsmith, FlagsmithError
from .provision import variant_value
from .spec import MIN_CONTROL_SHARE, Plan, planned_rollout_from

PROBE_PREFIX = "referee-probe-"


def run(
    client: Flagsmith,
    experiment_id: int,
    plan: Optional[Plan] = None,
    code_root: Optional[Path] = None,
    probe: int = 0,
) -> Dict[str, Any]:
    """Run every check and return them with an overall ``ready`` flag."""
    checks: List[Dict[str, Any]] = []

    def add(check_id: str, status: str, title: str, detail: str = "", fix: str = "", evidence: Any = None) -> None:
        entry: Dict[str, Any] = {"id": check_id, "status": status, "title": title, "detail": detail}
        if fix:
            entry["fix"] = fix
        if evidence:
            entry["evidence"] = evidence
        checks.append(entry)

    experiment = client.experiment(experiment_id)
    feature = experiment.get("feature") or {}
    flag = feature.get("name", "")
    options = feature.get("multivariate_options") or []
    rollout = experiment.get("experiment_rollout") or {}
    status = experiment.get("status")

    # ------------------------------------------------------------ Flagsmith
    version = client.version()
    if version.get("experimentation_enabled") is False:
        add(
            "experimentation_enabled", "fail", "Experimentation is switched off on this Flagsmith",
            "The API reports experimentation_enabled = false.",
            "Set EXPERIMENTATION_ENABLED=true on the Flagsmith API and task processor, then restart them.",
        )

    try:
        connections = client.warehouse_connections()
    except FlagsmithError as error:
        connections = []
        add("warehouse", "fail", "Could not read the warehouse connection", str(error))
    else:
        if not connections:
            add(
                "warehouse", "fail", "No warehouse is connected",
                "Events have nowhere to go, so the experiment would show no data.",
                "Run `referee apply` (it enables the built-in Flagsmith database), or connect one in Environment Settings > Warehouse.",
            )
        elif connections[0].get("status") != "connected":
            add(
                "warehouse", "fail", "The warehouse is not connected",
                "Status is '%s'. %s" % (connections[0].get("status"), connections[0].get("status_detail") or ""),
                "Fix the connection in Environment Settings > Warehouse.",
            )
        else:
            add("warehouse", "pass", "Warehouse connected", "%s." % connections[0].get("name"))

    if status == "completed":
        add("status", "fail", "The experiment is already completed", "A completed experiment cannot be started again.")
    elif status == "running":
        add("status", "info", "The experiment is already running", "Checks below still apply to a running experiment.")

    # ------------------------------------------------------------ variants
    allocations = {
        v["multivariate_feature_option"]: float(v.get("percentage_allocation") or 0)
        for v in rollout.get("multivariate_feature_state_values") or []
    }
    live = [o for o in options if allocations.get(o["id"], 0) > 0]
    unkeyed = [o for o in live if not o.get("key")]
    if not live:
        add(
            "variants", "fail", "No variant receives traffic",
            "Every variant has a 0% share, so everyone would get control.",
            "Give at least one variant a share in the experiment's rollout.",
        )
    elif unkeyed:
        add(
            "variants", "fail", "A variant has no key",
            "Variant(s) serving %s have no key. Flagsmith records no exposures for a variant without one, "
            "so that arm would stay empty forever." % ", ".join("'%s'" % variant_value(o) for o in unkeyed),
            "Run `referee apply` with a plan that names the variant, or set the key on the flag's Variations tab.",
        )
    else:
        add(
            "variants", "pass", "Every variant has a key",
            ", ".join("%s (%g%%)" % (o["key"], allocations[o["id"]]) for o in live) + ".",
        )
    control_share = 100.0 - sum(allocations.values())
    if live and 0 < control_share < MIN_CONTROL_SHARE:
        add(
            "control_share", "fail", "Control keeps almost no traffic",
            "Control gets %g%% of the experiment. With so little there is nothing to compare against, and "
            "nearly everyone gets a new version: that is a rollout, which needs a person's approval."
            % control_share,
            "Give control at least %g%% of the experiment's traffic." % MIN_CONTROL_SHARE,
        )
    if live and control_share <= 0:
        add(
            "control_share", "fail", "Control gets no traffic",
            "Variant shares add up to %g%%, leaving nothing to compare against." % sum(allocations.values()),
            "Lower the variant shares so control keeps a share.",
        )
    control_served = (rollout.get("feature_state_value") or {}).get("value")
    try:
        default_state = client.environment_default(feature["id"]) if feature.get("id") is not None else None
    except (FlagsmithError, AttributeError):
        default_state = None
    base_value = (default_state or {}).get("feature_state_value")
    if status == "created" and control_served is not None and base_value is not None and (
        not isinstance(base_value, str) or control_served != base_value
    ):
        add(
            "control_baseline", "fail", "Control is not what users get today",
            "The flag serves '%s' to everyone now, but this experiment's control arm serves '%s'. Starting it "
            "would move the control arm's users to something new as well, so there is no baseline, and far "
            "more users change than the split suggests." % (base_value, control_served),
            "Make '%s' the control value and test '%s' as a variant." % (base_value, control_served),
        )
    # Referee holds an experiment's live rollout at 0% until it is started and
    # keeps the intended percentage in the hypothesis note.
    live_pct = float(rollout.get("rollout_percentage") or 0)
    noted_pct = planned_rollout_from(experiment.get("hypothesis"))
    rollout_pct = noted_pct if noted_pct is not None else live_pct
    if rollout and not rollout.get("enabled", True):
        add("rollout_enabled", "warn", "The experiment's flag is disabled", "Identities in the experiment are served a disabled flag.")
    if rollout and rollout_pct <= 0:
        add("rollout", "fail", "Nobody enters the experiment", "The rollout percentage is 0.", "Raise the rollout percentage.")
    if status in ("created", "paused") and live_pct > 0:
        add(
            "serving_before_start", "warn", "The split is being served although the experiment is not running",
            "Flagsmith serves an experiment's rollout from the moment it exists, whatever its status. %g%% of "
            "identities are getting this experiment's versions right now, and none of it is being measured."
            % live_pct,
            "Run `referee %s` to set the live rollout to 0%% until the experiment is started."
            % ("apply <plan>" if status == "created" else "pause %s" % experiment.get("id")),
        )

    # -------------------------------------------------------------- metrics
    attached = experiment.get("metrics") or []
    all_metrics = {m["id"]: m for m in client.metrics()} if attached else {}
    goal_metrics = [m for m in attached if m.get("expected_direction") in ("increase", "decrease")]
    if not attached:
        add("metrics", "fail", "No metric is attached", "Without a metric there is nothing to win on.", "Attach a metric to the experiment.")
    elif not goal_metrics:
        add(
            "metrics", "fail", "Every attached metric is a guardrail",
            "Guardrails say what must not get worse; none of them says what should improve.",
            "Attach a metric expected to increase or decrease.",
        )
    seen_events: List[str] = []
    if connections and connections[0].get("status") == "connected":
        try:
            seen_events = client.warehouse_event_names(connections[0]["id"])
        except FlagsmithError:
            seen_events = []
    events_needed: Dict[str, str] = {}
    for item in attached:
        metric = all_metrics.get(item.get("metric")) or {}
        event = (metric.get("definition") or {}).get("event")
        if event:
            events_needed[event] = item.get("metric_name") or metric.get("name") or event

    # ----------------------------------------------------------------- code
    scan = None
    if code_root is not None:
        values = [variant_value(o) for o in live] + [o["key"] for o in live if o.get("key")]
        scan = codescan.scan(Path(code_root), flag, values)
        reads = [r for r in scan.references if r.kind in ("exposing", "plain_read")]
        if not scan.references and scan.unnamed_reads:
            first = scan.unnamed_reads[0]
            add(
                "code_exposure", "warn", "Could not confirm the code reads this flag",
                "The flag's name does not appear in the code, but %d experiment call(s) read a flag whose name "
                "is a variable (first at %s:%d), which this text scan cannot follow. Check that one of them "
                "reads '%s'." % (len(scan.unnamed_reads), first.file, first.line, flag),
            )
        elif not scan.references:
            add(
                "code_flag", "fail", "The code never mentions the flag",
                "No string literal '%s' in %d source files under %s." % (flag, scan.files_scanned, code_root),
                "Evaluate the flag with the SDK's experiment call, for example get_experiment_flag(\"%s\", identifier=...)." % flag,
            )
        elif not scan.exposing and not reads and scan.unnamed_reads:
            first = scan.unnamed_reads[0]
            add(
                "code_exposure", "warn", "Could not confirm the flag is read through an experiment call",
                "The code mentions '%s' but this text scan cannot see it being passed to a call. %d experiment "
                "call(s) read a flag whose name is a variable (first at %s:%d); check that one of them reads this flag."
                % (flag, len(scan.unnamed_reads), first.file, first.line),
                "Passing the name as a literal or a simple constant lets preflight verify it.",
                [r.to_dict() for r in scan.references[:5]],
            )
        elif not scan.exposing:
            add(
                "code_exposure", "fail", "No experiment call reads this flag",
                "The code %s '%s', but this scan found no call that records who saw which version "
                "(get_experiment_flag / getExperimentFlag / useExperiment). If the flag is only read with a "
                "plain call, Flagsmith would show zero identities."
                % ("reads" if reads else "mentions", flag),
                "Read the flag through the experiment call, with its name as a literal or a simple constant "
                "(preflight is a text scan and cannot follow a name built at run time).",
                [r.to_dict() for r in scan.references[:5]],
            )
        else:
            detail = "%d exposure-recording read(s), first at %s:%d." % (
                len(scan.exposing), scan.exposing[0].file, scan.exposing[0].line,
            )
            if scan.plain_reads:
                add(
                    "code_exposure", "warn", "Some reads of the flag record no exposure",
                    detail + " But %d other read(s) use a plain flag call; users who only hit those paths "
                    "see a version without being counted." % len(scan.plain_reads),
                    "Route every read of this flag through the experiment call.",
                    [r.to_dict() for r in scan.plain_reads[:5]],
                )
            else:
                add("code_exposure", "pass", "The code records exposures for the flag", detail)
        if scan.references:
            unhandled = [
                o for o in live
                if variant_value(o) not in scan.values_seen and (o.get("key") or "") not in scan.values_seen
            ]
            if unhandled:
                add(
                    "code_variants", "warn", "A version may not be handled in code",
                    "No string literal for %s near the flag. If the code only branches on other values, "
                    "these users silently get the default experience."
                    % ", ".join("'%s'" % variant_value(o) for o in unhandled),
                    "Check the branch that renders each version.",
                )
            else:
                add("code_variants", "pass", "Every version appears in the code", "")
        for event, metric_name in events_needed.items():
            if event in scan.tracked_events:
                first = scan.tracked_events[event][0]
                add(
                    "code_event:" + event, "pass", "The code sends '%s'" % event,
                    "For metric '%s', first at %s:%d." % (metric_name, first.file, first.line),
                )
            else:
                near = codescan.closest(event, scan.tracked_events)
                if near:
                    add(
                        "code_event:" + event, "fail", "Event name mismatch for metric '%s'" % metric_name,
                        "The metric counts '%s' but the code sends '%s' (%s:%d). Event names are case-sensitive, "
                        "so this metric would stay at zero."
                        % (event, near, scan.tracked_events[near][0].file, scan.tracked_events[near][0].line),
                        "Rename the event in code to '%s', or point the metric at '%s'." % (event, near),
                    )
                elif scan.unnamed_tracks:
                    first = scan.unnamed_tracks[0]
                    add(
                        "code_event:" + event, "warn", "Could not confirm the code sends '%s'" % event,
                        "No call names this event literally, but %d call(s) send an event whose name is a "
                        "constant or variable (first at %s:%d), which this scan cannot read. Check that one of "
                        "them sends exactly '%s'." % (len(scan.unnamed_tracks), first.file, first.line, event),
                    )
                else:
                    add(
                        "code_event:" + event, "fail", "The code never sends '%s'" % event,
                        "Metric '%s' counts this event, but no track_event / trackEvent call with that name was found. "
                        "(Preflight is a text scan: it sees the name as a literal or a simple constant in the call, "
                        "not one passed through a wrapper with another name.)" % metric_name,
                        "Call track_event(\"%s\", identifier=...) at the point in the code where it happens." % event,
                    )
        if scan.leaked_keys:
            add(
                "code_secret", "warn", "A server-side environment key is written in source",
                "Found at %s:%d. Server-side keys can read every flag and should come from the environment."
                % (scan.leaked_keys[0].file, scan.leaked_keys[0].line),
                "Move it to an environment variable and rotate the key.",
            )

    # ----------------------------------------------------- events received
    for event, metric_name in events_needed.items():
        if event in seen_events:
            add(
                "event_seen:" + event, "info", "Flagsmith has received '%s' events before" % event,
                "At some point, from some client: this does not show that the code scanned here is sending them. "
                "After the start, the scorecard is what confirms events are arriving.",
            )
            continue
        # Events that some metric in this environment counts are somebody
        # else's; only unclaimed names can be this one misspelt.
        claimed = {(m.get("definition") or {}).get("event") for m in all_metrics.values()}
        unclaimed = [e for e in seen_events if not e.startswith("$") and e not in claimed]
        same = codescan.same_but_for_spelling(event, unclaimed)
        near = same or codescan.closest(event, unclaimed)
        if same:
            add(
                "event_seen:" + event, "fail", "Events arrive under a different name",
                "Metric '%s' counts '%s', but Flagsmith is receiving '%s', which differs only in case or "
                "punctuation. Names are case-sensitive." % (metric_name, event, same),
                "Send '%s' from the code, or change the metric's event to '%s'." % (event, same),
            )
        elif near:
            add(
                "event_seen:" + event, "warn", "A similarly named event is arriving",
                "Metric '%s' counts '%s', which Flagsmith has not received yet. It is receiving '%s', "
                "which no metric counts. If that is meant to be the same event, one of the two names is wrong."
                % (metric_name, event, near),
            )
        else:
            add(
                "event_seen:" + event, "info", "No '%s' events received yet" % event,
                "Normal before launch. If the experiment is already live, the event is not being sent "
                "(Flagsmith's list of event names can lag by up to five minutes).",
            )

    # ----------------------------------------------------------- live probe
    if probe > 0 and status in ("running",):
        add(*_probe(client, flag, probe, rollout, options))
    elif probe > 0:
        add(
            "probe", "info", "Assignment probe skipped",
            "Flagsmith only assigns versions once the experiment is running; run preflight again after starting.",
        )

    # --------------------------------------------------------------- sizing
    if plan is not None:
        sizing = plan.sizing()
        if sizing.get("per_arm"):
            detail = "%d identities per version (%d in the experiment)" % (sizing["per_arm"], sizing["total_in_experiment"])
            if sizing.get("days") is not None:
                detail += ", about %s at the planned traffic" % scorecard.duration(sizing["days"] * 24)
            if sizing.get("finishes_before_deadline") is False:
                lift = sizing.get("detectable_lift_by_deadline")
                add(
                    "sizing", "warn", "This will not finish before the deadline",
                    detail + ". Only %d per version fit before the deadline%s."
                    % (
                        sizing.get("identities_per_arm_by_deadline", 0),
                        ", enough to detect a lift of about %+.0f%% or larger" % (lift * 100) if lift else "",
                    ),
                    "Raise the rollout percentage, pick a metric that happens earlier in the funnel, or plan for a larger minimum lift.",
                )
            else:
                add("sizing", "pass", "The plan can finish in time", detail + ".")
        for note in sizing.get("notes", []):
            add("sizing_note", "info", "Sizing", note)

    # ------------------------------------------------- overlapping experiments
    try:
        others = [e for e in client.experiments(("running",)) if e.get("id") != experiment_id]
    except FlagsmithError:
        others = []
    if others:
        add(
            "overlap", "info", "Other experiments are running in this environment",
            "%s. Flagsmith assigns each experiment independently, so they do not bias each other, "
            "but they share the same users." % ", ".join("'%s'" % e["name"] for e in others[:5]),
        )

    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    return {
        "experiment": {"id": experiment.get("id"), "name": experiment.get("name"), "status": status, "flag": flag},
        "ready": not failed,
        "summary": _summary(failed, warned, checks),
        "checks": checks,
        "code_root": str(code_root) if code_root is not None else None,
        "files_scanned": scan.files_scanned if scan is not None else None,
        "files_skipped": scan.files_skipped if scan is not None else None,
    }


def _summary(failed: List[Dict[str, Any]], warned: List[Dict[str, Any]], checks: List[Dict[str, Any]]) -> str:
    if failed:
        return "Not ready: %d check(s) failed%s." % (
            len(failed), " and %d need a look" % len(warned) if warned else "",
        )
    passed = len([c for c in checks if c["status"] == "pass"])
    if warned:
        return "Ready, with %d warning(s) worth reading (%d checks passed)." % (len(warned), passed)
    return "Ready: all %d checks passed." % passed


def _probe(client: Flagsmith, flag: str, count: int, rollout: Dict[str, Any], options: List[Dict[str, Any]]) -> tuple:
    """Ask Flagsmith which version a batch of made-up identities would get.

    This checks assignment end to end without sending any exposure event, so
    the probe identities never count towards the experiment.
    """
    counts: Dict[str, int] = {}
    outside = 0
    errors = 0
    for index in range(count):
        try:
            data = client.identity_flags("%s%06d" % (PROBE_PREFIX, index))
        except FlagsmithError:
            errors += 1
            continue
        found = next((f for f in data.get("flags", []) if (f.get("feature") or {}).get("name") == flag), None)
        if found is None:
            errors += 1
            continue
        info = ((found.get("metadata") or {}).get("experiment")) or {}
        if not info.get("in_experiment"):
            outside += 1
            continue
        key = found.get("variant") or CONTROL_KEY
        counts[key] = counts.get(key, 0) + 1
    if errors > count // 2:
        return ("probe", "fail", "Flag evaluation failed for most probe identities", "%d of %d requests failed." % (errors, count))
    inside = sum(counts.values())
    summary = ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items())) or "none"
    keys = {o["id"]: o.get("key") for o in options}
    shares = stats.expected_shares(rollout, {k: v for k, v in keys.items() if v})
    expected_arms = [k for k, share in shares.items() if share > 0]
    missing = [k for k in expected_arms if counts.get(k, 0) == 0]
    if inside == 0:
        return (
            "probe", "fail", "No probe identity entered the experiment",
            "%d identities were evaluated and none was in the experiment." % count,
            "Check the rollout percentage and audience.",
        )
    if missing and inside >= 30:
        return (
            "probe", "fail", "A version is never served",
            "Of %d probe identities in the experiment, none received %s (got: %s)."
            % (inside, ", ".join("'%s'" % m for m in missing), summary),
            "Check the variant's share in the experiment rollout.",
        )
    p_value = stats.srm_p_value([counts.get(k, 0) for k in expected_arms], [shares[k] for k in expected_arms])
    if p_value is not None and inside >= stats.SRM_MIN_TOTAL and p_value < stats.SRM_ALARM:
        return (
            "probe", "fail", "Assignment does not follow the configured split",
            "Probe identities were assigned %s (p = %.2g)." % (summary, p_value),
        )
    return (
        "probe", "pass", "Flagsmith assigns versions as configured",
        "%d probe identities: %d in the experiment (%s), %d outside. No exposure was recorded for them."
        % (count, inside, summary, outside),
    )
