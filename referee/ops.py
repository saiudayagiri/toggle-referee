"""The operations Referee offers, shared by the CLI, the MCP server and the web page.

Each function takes settings and plain arguments and returns plain data, so the
three front ends stay thin and behave identically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import codescan, decisions, preflight as preflight_module, provision, scorecard, spec
from .api import CONTROL_KEY, Flagsmith, FlagsmithError
from .config import Settings
from .provision import variant_value


MAX_PROBE = 2000
# The share of all users an agent may put on new versions by starting an
# experiment. A wider one is closer to a rollout and is started by a person.
AGENT_MAX_EXPOSURE = 50.0


class OpError(Exception):
    """Something the caller asked for cannot be done; the message says why."""


def _client(settings: Settings) -> Flagsmith:
    missing = settings.missing()
    if missing:
        raise OpError(
            "Referee is not connected to Flagsmith yet. Set %s (environment variables, "
            "a flagsmith.env file, or referee.json)." % " and ".join(missing)
        )
    return settings.client()


def resolve_experiment(client: Flagsmith, reference: Any) -> int:
    """Accept an experiment id, its exact name, or the name of its flag."""
    text = str(reference).strip()
    if text.isascii() and text.isdigit() and len(text) < 12:
        return int(text)
    experiments = client.experiments()
    by_name = [e for e in experiments if e.get("name") == text]
    by_flag = [e for e in experiments if (e.get("feature") or {}).get("name") == text]
    matches = by_name or by_flag
    if not matches:
        known = ", ".join("%s (%d)" % (e["name"], e["id"]) for e in experiments[:10]) or "none"
        raise OpError("No experiment called '%s'. Experiments here: %s." % (text, known))
    active = [e for e in matches if e.get("status") != "completed"]
    if len(active) > 1:
        # Flagsmith allows two experiments with the same name; guessing between them is not safe.
        raise OpError(
            "'%s' matches %d experiments that are not completed (ids %s). Use the id."
            % (text, len(active), ", ".join(str(e["id"]) for e in sorted(active, key=lambda e: e["id"])))
        )
    chosen = sorted(active or matches, key=lambda e: e["id"])[-1]
    return int(chosen["id"])


def doctor(settings: Settings) -> Dict[str, Any]:
    """Can Referee reach Flagsmith, and is experimentation usable there?"""
    out: Dict[str, Any] = {"settings": settings.public(), "ok": False, "problems": []}
    missing = settings.missing()
    if missing:
        out["problems"].append("Missing settings: %s." % ", ".join(missing))
        return out
    client = settings.client()
    version = client.version()
    out["flagsmith"] = {
        "version": version.get("image_tag") or (version.get("package_versions") or {}).get("."),
        "experimentation_enabled": version.get("experimentation_enabled"),
        "self_hosted": version.get("is_saas") is False,
    }
    try:
        environment = client.environment()
        out["environment"] = {"name": environment.get("name"), "project": environment.get("project")}
    except FlagsmithError as error:
        out["problems"].append(str(error))
        return out
    if version.get("experimentation_enabled") is False:
        out["problems"].append("Experimentation is disabled on this Flagsmith (set EXPERIMENTATION_ENABLED=true).")
    try:
        connections = client.warehouse_connections()
        out["warehouse"] = (
            {"name": connections[0].get("name"), "status": connections[0].get("status")}
            if connections
            else None
        )
        out["experiments"] = len(client.experiments())
    except FlagsmithError as error:
        out["problems"].append(str(error))
        return out
    out["ok"] = not out["problems"]
    return out


def check_plan(plan_data: Any) -> Dict[str, Any]:
    """Validate a plan and size it. Touches nothing."""
    try:
        plan = spec.parse(plan_data)
    except spec.PlanError as error:
        return {"valid": False, "problems": error.problems}
    try:
        sizing = plan.sizing()
    except ValueError as error:
        return {"valid": False, "problems": ["The sizing assumptions do not work together: %s." % error]}
    return {"valid": True, "problems": [], "plan": plan.to_dict(), "sizing": sizing}


def apply_plan(settings: Settings, plan_data: Any, dry_run: bool = False) -> Dict[str, Any]:
    try:
        plan = spec.parse(plan_data)
    except spec.PlanError as error:
        raise OpError("The plan is not valid: " + " ".join(error.problems))
    try:
        sizing = plan.sizing()  # before anything is created, so a bad plan changes nothing
    except ValueError as error:
        raise OpError("The plan's sizing assumptions do not work together: %s." % error)
    client = _client(settings)
    try:
        done = provision.apply(client, plan, dry_run=dry_run)
    except provision.ProvisionError as error:
        raise OpError(str(error))
    out = done.to_dict()
    out["sizing"] = sizing
    if done.experiment_id is not None and not dry_run:
        stored = provision.recall_plan(settings.state_dir, done.experiment_id, settings.environment_key)
        if stored is None and done.experiment_status not in (None, "created"):
            # Already started, with no plan on record here. Assumptions that
            # arrive after the data has started coming in cannot become the
            # target; the planned sample recorded in Flagsmith (if any) stands.
            out["actions"].append(
                "This experiment had already started. The sizing assumptions in this plan were not stored: "
                "a target chosen after the start is not a target. The planned sample recorded in Flagsmith, "
                "if there is one, still applies."
            )
        elif stored is not None and done.experiment_status not in (None, "created"):
            # The planned sample is the stopping rule. Once the experiment has
            # started it is frozen, or a lead could be turned into a "win" by
            # re-applying the plan with easier assumptions.
            if stored.to_dict() != plan.to_dict():
                out["actions"].append(
                    "The plan on record from before the start was kept. A started experiment's planned "
                    "sample cannot be changed, so the new assumptions were not stored."
                )
                out["sizing"] = stored.sizing()
        else:
            provision.remember_plan(settings.state_dir, done.experiment_id, plan, settings.environment_key)
        out["next"] = (
            "Instrument the code, then run preflight for experiment %d. Nothing is started yet." % done.experiment_id
            if done.experiment_status in (None, "created")
            else "Experiment %d is %s; applying the plan again did not change that."
            % (done.experiment_id, done.experiment_status)
        )
    return out


def run_preflight(
    settings: Settings, experiment: Any, code_root: Optional[str] = None, probe: int = 0
) -> Dict[str, Any]:
    client = _client(settings)
    experiment_id = resolve_experiment(client, experiment)
    plan = provision.recall_plan(settings.state_dir, experiment_id, settings.environment_key)
    root = Path(code_root) if code_root else None
    if root is not None and not root.is_dir():
        raise OpError("There is no directory at %s to scan." % root)
    try:
        probe = max(0, min(int(probe or 0), MAX_PROBE))
    except (TypeError, ValueError):
        raise OpError("probe must be a whole number.")
    return preflight_module.run(client, experiment_id, plan=plan, code_root=root, probe=probe)


def planned_rollout(experiment: Dict[str, Any]) -> float:
    """The rollout percentage an experiment is meant to run at.

    Referee keeps an experiment's live rollout at 0% whenever it is not
    running and records the intended value in the hypothesis note. An
    experiment made elsewhere has only its live value; if even that is
    missing, assume everyone.
    """
    noted = spec.planned_rollout_from(experiment.get("hypothesis"))
    if noted is not None:
        return noted
    live = (experiment.get("experiment_rollout") or {}).get("rollout_percentage")
    return 100.0 if live is None else float(live)


def live_rollout(experiment: Dict[str, Any]) -> float:
    return float((experiment.get("experiment_rollout") or {}).get("rollout_percentage") or 0)


def exposure(experiment: Dict[str, Any]) -> float:
    """Percent of all identities an experiment will serve a non-control version."""
    rollout = experiment.get("experiment_rollout") or {}
    variants = sum(
        float(v.get("percentage_allocation") or 0)
        for v in rollout.get("multivariate_feature_state_values") or []
    )
    return planned_rollout(experiment) * min(100.0, variants) / 100.0


def start(
    settings: Settings,
    experiment: Any,
    code_root: Optional[str] = None,
    force: bool = False,
    max_exposure: Optional[float] = None,
) -> Dict[str, Any]:
    """Start (or resume) an experiment, but only if preflight finds nothing broken.

    Starting is two steps in Flagsmith: the experiment's status becomes
    running, and its live rollout, which Referee holds at 0% until now, is
    raised to the planned percentage. Only then is anyone served a variant.
    """
    client = _client(settings)
    experiment_id = resolve_experiment(client, experiment)
    checks = run_preflight(settings, experiment_id, code_root)
    code_checked = bool(code_root)
    detail = client.experiment(experiment_id)
    wanted = planned_rollout(detail)
    exposed = exposure(detail)
    running = checks["experiment"]["status"] == "running"
    needs_restore = (
        running
        and abs(live_rollout(detail) - wanted) > 1e-6
        and spec.planned_rollout_from(detail.get("hypothesis")) is not None
    )
    if running and not needs_restore:
        return {
            "started": False, "already_running": True, "rollout_restored": False,
            "preflight": checks, "code_checked": code_checked,
        }
    # From here on the call would put users on variants (a start, a resume, or
    # restoring the rollout of a running experiment that is at 0%), so the
    # agent's exposure cap applies to all of them alike.
    if max_exposure is not None and exposed > max_exposure + 1e-9:
        return {
            "started": False,
            "reason": (
                "Not started. This experiment would give new versions to %.4g%% of all users, and an agent may "
                "start one that exposes at most %g%%. A person can start it with `referee start %d`."
                % (exposed, max_exposure, experiment_id)
            ),
            "preflight": checks,
            "code_checked": code_checked,
        }
    if not checks["ready"] and not force:
        return {
            "started": False,
            "reason": "Preflight failed, so the experiment was not started. Fix the failed checks and try again.",
            "preflight": checks,
            "code_checked": code_checked,
        }
    if needs_restore:
        _set_rollout(client, detail, wanted)
        return {
            "started": False, "already_running": True, "rollout_restored": True, "rollout_percentage": wanted,
            "preflight": checks, "code_checked": code_checked,
        }
    try:
        started = client.start_experiment(experiment_id)
    except FlagsmithError as error:
        raise OpError("Flagsmith would not start the experiment: %s" % error)
    if abs(live_rollout(detail) - wanted) > 1e-6:
        try:
            _set_rollout(client, detail, wanted)
        except OpError as error:
            # Running with nobody in it is harmless but useless; put it back.
            try:
                client.pause_experiment(experiment_id)
                state = "It has been paused again."
            except FlagsmithError:
                state = "It is still marked running, with a live rollout of 0%, so nobody is in it; pause it or start it again."
            raise OpError("The experiment was started but its rollout could not be raised to %g%% (%s). %s" % (wanted, error, state))
    return {
        "started": True,
        "forced": bool(force and not checks["ready"]),
        "code_checked": code_checked,
        "rollout_percentage": wanted,
        "experiment": {"id": started["id"], "name": started["name"], "status": started["status"], "started_at": started.get("started_at")},
        "preflight": checks,
    }


def _set_rollout(client: Flagsmith, experiment: Dict[str, Any], percentage: float) -> None:
    try:
        client.set_rollout_percentage(experiment, percentage)
    except FlagsmithError as error:
        raise OpError("Flagsmith would not change the rollout: %s" % error)


def pause(settings: Settings, experiment: Any) -> Dict[str, Any]:
    """Pause an experiment and stop serving it.

    Flagsmith's own pause changes the status only: the split keeps being
    served. Referee also drops the live rollout to 0%, so everyone gets the
    flag's normal value until the experiment is started again. Assignment is
    by identity, so the same people get the same versions after a resume.
    """
    client = _client(settings)
    experiment_id = resolve_experiment(client, experiment)
    detail = client.experiment(experiment_id)
    if detail.get("status") == "completed":
        raise OpError("'%s' is completed; there is nothing to pause." % detail.get("name"))
    if detail.get("status") == "running":
        try:
            paused = client.pause_experiment(experiment_id)
        except FlagsmithError as error:
            raise OpError("Flagsmith would not pause the experiment: %s" % error)
    else:
        # Not running, but it may still be serving its split (Flagsmith serves
        # a rollout in any status), so the rollout is still brought to 0%.
        paused = detail
    out = {
        "id": paused["id"], "name": paused["name"], "status": paused["status"], "serving": True,
        "control_value": ((detail.get("experiment_rollout") or {}).get("feature_state_value") or {}).get("value"),
    }
    if live_rollout(detail) <= 0:
        out["serving"] = False
        return out
    if spec.planned_rollout_from(detail.get("hypothesis")) is None:
        # Made elsewhere: note its rollout before zeroing it, so a later start
        # can put it back.
        try:
            client.update_hypothesis(
                experiment_id,
                spec.without_marker(detail.get("hypothesis"))
                + spec.marker(round(live_rollout(detail), 2), spec.planned_sample_from(detail.get("hypothesis"))),
            )
        except FlagsmithError as error:
            out["warning"] = (
                "The split is still being served. Its rollout was left alone because the percentage could not "
                "be recorded with the experiment first (%s), and zeroing it without that record would lose it." % error
            )
            return out
    try:
        client.set_rollout_percentage(detail, 0)
        out["serving"] = False
    except FlagsmithError as error:
        out["warning"] = "The split is still being served: the rollout could not be set to 0%% (%s)." % error
    return out


PREVIEW_PREFIX = "referee-preview"


def preview(
    settings: Settings, experiment: Any, version: str = "", identity: str = "", clear: bool = False
) -> Dict[str, Any]:
    """Let one test identity see a chosen version, before or during the experiment.

    Uses a Flagsmith identity override, which applies to that identity alone.
    Only identities whose name starts with ``referee-preview`` can be used, so
    this can never be pointed at a real user. A preview identity is outside
    the experiment and does not enter its results.
    """
    client = _client(settings)
    experiment_id = resolve_experiment(client, experiment)
    detail = client.experiment(experiment_id)
    feature = detail.get("feature") or {}
    options = feature.get("multivariate_options") or []
    identity = (identity or PREVIEW_PREFIX).strip()
    if not identity.startswith(PREVIEW_PREFIX) or len(identity) > 100 or not all(c.isalnum() or c in "-_." for c in identity):
        raise OpError(
            "A preview identity must be named '%s' or start with it (letters, digits, '-', '_', '.'), "
            "so that a preview can never override what a real user gets." % PREVIEW_PREFIX
        )
    control_value = ((detail.get("experiment_rollout") or {}).get("feature_state_value") or {}).get("value")
    if control_value is None:
        control_value = feature.get("initial_value", "")
    out: Dict[str, Any] = {
        "experiment": {"id": detail["id"], "name": detail["name"], "flag": feature.get("name")},
        "identity": identity,
    }
    try:
        if clear:
            identity_id = client.identity_id(identity)
            removed = 0
            if identity_id is not None:
                for override in client.identity_overrides(identity_id, feature["id"]):
                    client.delete_identity_override(identity_id, override["id"])
                    removed += 1
            out.update(cleared=True, removed=removed)
            return out
        keys = ["control"] + [o["key"] for o in options if o.get("key")]
        if version not in keys:
            raise OpError("Choose a version to preview: %s." % ", ".join(keys))
        shares = {o["id"]: (100.0 if o.get("key") == version else 0.0) for o in options}
        identity_id = client.identity_id(identity, create=True)
        client.set_identity_override(identity_id, feature["id"], str(control_value), shares)
        served = next(
            (f for f in client.identity_flags(identity).get("flags", []) if (f.get("feature") or {}).get("name") == feature.get("name")),
            {},
        )
    except FlagsmithError as error:
        raise OpError("Flagsmith would not set the preview: %s" % error)
    out.update(
        cleared=False,
        version=version,
        serves=served.get("feature_state_value"),
        how=(
            "Use the application as the identity '%s' (however this app identifies a user: a user id, or the "
            "value of its visitor cookie) and it will get '%s'. Nobody else is affected. Run the same command "
            "with --clear when you are done." % (identity, served.get("feature_state_value"))
        ),
    )
    return out


def list_experiments(settings: Settings) -> List[Dict[str, Any]]:
    client = _client(settings)
    ledger = decisions.Ledger(settings.state_dir)
    pending = {d["experiment_id"]: d["id"] for d in ledger.pending(environment_key=settings.environment_key)}
    out = []
    for experiment in sorted(client.experiments(), key=lambda e: e["id"], reverse=True):
        out.append(
            {
                "id": experiment["id"],
                "name": experiment["name"],
                "flag": (experiment.get("feature") or {}).get("name"),
                "status": experiment["status"],
                "started_at": experiment.get("started_at"),
                "ended_at": experiment.get("ended_at"),
                "rollout_decision": experiment.get("rollout_decision"),
                "pending_decision": pending.get(experiment["id"]),
                "has_plan": provision.plan_path(settings.state_dir, experiment["id"]).is_file(),
            }
        )
    return out


def get_scorecard(settings: Settings, experiment: Any, refresh: bool = True) -> Dict[str, Any]:
    client = _client(settings)
    experiment_id = resolve_experiment(client, experiment)
    plan = provision.recall_plan(settings.state_dir, experiment_id, settings.environment_key)
    card = scorecard.build(client, experiment_id, plan=plan, refresh=refresh)
    pending = decisions.Ledger(settings.state_dir).pending(experiment_id, settings.environment_key)
    card["pending_decision"] = pending[0]["id"] if pending else None
    return card


def propose(
    settings: Settings,
    experiment: Any,
    proposed_by: str = "agent",
    note: str = "",
    valid_hours: Optional[float] = None,
    void_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a rollout proposal for a person to approve. Changes nothing in Flagsmith."""
    card = get_scorecard(settings, experiment)
    card.pop("pending_decision", None)
    ledger = decisions.Ledger(settings.state_dir)
    try:
        decision = ledger.propose(
            card, settings.environment_key, proposed_by=proposed_by, note=note, valid_hours=valid_hours,
            void_reason=void_reason,
        )
    except decisions.DecisionError as error:
        raise OpError(str(error))
    return decision


def approve(
    settings: Settings,
    decision_id: str,
    approver_token: str,
    note: str = "",
    method: str = "cli",
) -> Dict[str, Any]:
    """Approve as a signed-in Flagsmith user; that user's token ends the experiment."""
    client = _client(settings)
    ledger = decisions.Ledger(settings.state_dir)
    try:
        decision = ledger.get(decision_id)
    except decisions.DecisionError as error:
        raise OpError(str(error))
    if decision.get("environment_key") != settings.environment_key:
        ledger.note("approval_refused", decision["id"], "referee", {"reason": "different Flagsmith environment"})
        raise OpError("Decision %s belongs to a different Flagsmith environment." % decision_id)

    def rebuild(experiment_id: int) -> Dict[str, Any]:
        plan = provision.recall_plan(settings.state_dir, experiment_id, settings.environment_key)
        return scorecard.build(client, experiment_id, plan=plan, refresh=True)

    try:
        return ledger.approve(decision_id, client.as_user(approver_token), rebuild, note=note, method=method)
    except decisions.DecisionError as error:
        raise OpError(str(error))


def note_refused_sign_in(settings: Settings, decision_id: str, email: str, method: str) -> None:
    """Record a failed sign-in on an approval attempt in the ledger."""
    ledger = decisions.Ledger(settings.state_dir)
    try:
        ledger.get(decision_id)
    except decisions.DecisionError:
        return
    ledger.note("approval_refused", decision_id, str(email)[:200] or "unknown", {"reason": "Flagsmith sign-in failed", "method": method})


def reject(settings: Settings, decision_id: str, by: str, reason: str = "") -> Dict[str, Any]:
    try:
        ledger = decisions.Ledger(settings.state_dir)
        if settings.environment_key and ledger.get(decision_id).get("environment_key") != settings.environment_key:
            raise OpError("Decision %s belongs to a different Flagsmith environment." % decision_id)
        return ledger.reject(decision_id, by, reason)
    except decisions.DecisionError as error:
        raise OpError(str(error))


def get_decision(settings: Settings, decision_id: str) -> Dict[str, Any]:
    try:
        return decisions.Ledger(settings.state_dir).get(decision_id)
    except decisions.DecisionError as error:
        raise OpError(str(error))


def list_decisions(settings: Settings) -> List[Dict[str, Any]]:
    return [decisions.summary(d) for d in decisions.Ledger(settings.state_dir).all()]


def cleanup(
    settings: Settings,
    experiment: Any,
    code_root: str,
    repository_url: str = "",
    revision: str = "",
) -> Dict[str, Any]:
    """After an experiment ends: what is left in the code to remove.

    Lists every place the flag and the losing versions still appear. With a
    repository URL and revision the references are also uploaded to Flagsmith,
    where they show on the flag's page.
    """
    client = _client(settings)
    experiment_id = resolve_experiment(client, experiment)
    detail = client.experiment(experiment_id)
    feature = detail.get("feature") or {}
    flag = feature.get("name", "")
    options = feature.get("multivariate_options") or []
    root = Path(code_root)
    if not root.is_dir():
        raise OpError("There is no directory at %s to scan." % root)
    # What control serves is the experiment's control value, which may differ
    # from the flag's initial value; after completion only the plan remembers it.
    plan = provision.recall_plan(settings.state_dir, experiment_id, settings.environment_key)
    control_value = ((detail.get("experiment_rollout") or {}).get("feature_state_value") or {}).get("value")
    if control_value is None:
        control_value = plan.control_value if plan is not None else feature.get("initial_value", "")
    versions = [{"id": None, "key": CONTROL_KEY, "value": str(control_value)}]
    for option in options:
        versions.append({"id": option["id"], "key": option.get("key") or variant_value(option), "value": variant_value(option)})
    literals = sorted({v["value"] for v in versions} | {v["key"] for v in versions if v["key"] != CONTROL_KEY})
    scan = codescan.scan(root, flag, literals)

    out: Dict[str, Any] = {
        "experiment": {"id": detail["id"], "name": detail["name"], "status": detail["status"], "flag": flag},
        "references": [r.to_dict() for r in scan.references],
        "files_scanned": scan.files_scanned,
    }
    if detail.get("status") != "completed":
        out["ready_to_clean"] = False
        out["advice"] = (
            "This experiment is %s. Leave the flag in the code until the experiment is completed "
            "and the winner has been serving everyone for a while." % detail.get("status")
        )
    else:
        decision = detail.get("rollout_decision")
        winner = versions[0]
        known = decision in ("variant", "control")
        if decision == "variant":
            shipped = next((v for v in versions if v["id"] is not None and v["id"] == detail.get("rollout_variant")), None)
            if shipped is None:
                known = False
            else:
                winner = shipped
        if not known:
            out["ready_to_clean"] = False
            out["advice"] = (
                "The experiment ended by disabling the flag; remove the feature's code path entirely."
                if decision == "disable"
                else "Flagsmith does not say clearly which version was rolled out (decision '%s'), so check the "
                "flag in Flagsmith before deleting any code." % decision
            )
        else:
            losers = [v for v in versions if v is not winner]
            out["ready_to_clean"] = True
            out["winner"] = {"key": winner["key"], "value": winner["value"]}
            out["losers"] = [{"key": v["key"], "value": v["value"]} for v in losers]
            out["advice"] = (
                "Flagsmith now serves '%s' to everyone. In the code: keep the branch for '%s', delete the "
                "branches for %s, replace the flag read with the winning behaviour, drop the exposure and "
                "event tracking that only served this experiment, then archive the flag '%s' in Flagsmith."
                % (
                    winner["value"], winner["value"],
                    ", ".join("'%s'" % v["value"] for v in losers) or "(none)", flag,
                )
            )
            loser_literals = {v["value"] for v in losers} | {v["key"] for v in losers if v["key"] != CONTROL_KEY}
            loser_literals -= {winner["value"], winner["key"]}
            out["loser_code"] = {
                value: [r.to_dict() for r in refs] for value, refs in scan.values_seen.items() if value in loser_literals
            }
    if repository_url and revision:
        references = [
            {"feature_name": flag, "file_path": r.file, "line_number": r.line} for r in scan.references
        ]
        try:
            client.upload_code_references(repository_url, revision, references)
            out["uploaded_to_flagsmith"] = len(references)
        except FlagsmithError as error:
            out["upload_error"] = str(error)
    return out


def stale_flags(settings: Settings, code_root: str) -> Dict[str, Any]:
    """Flags whose experiment is finished but which the code still reads."""
    client = _client(settings)
    root = Path(code_root)
    if not root.is_dir():
        raise OpError("There is no directory at %s to scan." % root)
    finished: Dict[str, Dict[str, Any]] = {}
    active_flags = set()
    for experiment in client.experiments():
        flag = (experiment.get("feature") or {}).get("name")
        if not flag:
            continue
        if experiment.get("status") == "completed":
            current = finished.get(flag)
            if current is None or (experiment.get("ended_at") or "") > (current.get("ended_at") or ""):
                finished[flag] = experiment
        else:
            active_flags.add(flag)
    for flag in active_flags:
        finished.pop(flag, None)
    found = codescan.flags_in_code(root, finished.keys())
    stale = []
    for flag, references in sorted(found.items()):
        if not references:
            continue
        experiment = finished[flag]
        stale.append(
            {
                "flag": flag,
                "experiment": experiment["name"],
                "experiment_id": experiment["id"],
                "ended_at": experiment.get("ended_at"),
                "rollout_decision": experiment.get("rollout_decision"),
                "references": [r.to_dict() for r in references],
            }
        )
    return {"code_root": str(root), "finished_experiments": len(finished), "stale": stale}
