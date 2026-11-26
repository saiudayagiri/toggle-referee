"""Turn a plan into a Flagsmith flag, metrics and experiment.

Applying a plan is safe to repeat: anything that already exists and matches is
reused, anything that conflicts stops the run with an explanation, and nothing
is started. Starting is a separate, explicit step after preflight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api import Flagsmith, FlagsmithError
from .spec import Plan, parse, planned_rollout_from

ACTIVE = ("created", "running", "paused")


class ProvisionError(Exception):
    pass


@dataclass
class Provisioned:
    feature_id: Optional[int] = None
    variant_ids: Dict[str, int] = field(default_factory=dict)
    metric_ids: Dict[str, int] = field(default_factory=dict)
    experiment_id: Optional[int] = None
    experiment_status: Optional[str] = None
    actions: List[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "variant_ids": self.variant_ids,
            "metric_ids": self.metric_ids,
            "experiment_id": self.experiment_id,
            "experiment_status": self.experiment_status,
            "actions": self.actions,
            "dry_run": self.dry_run,
        }


def variant_value(variant: Dict[str, Any]) -> str:
    """The value a Flagsmith variant serves, as text."""
    for name in ("string_value", "integer_value", "boolean_value"):
        if variant.get(name) is not None:
            return str(variant[name])
    return ""


def apply(client: Flagsmith, plan: Plan, dry_run: bool = False) -> Provisioned:
    """Create whatever the plan needs that Flagsmith does not have yet."""
    out = Provisioned(dry_run=dry_run)
    try:
        return _apply(client, plan, out)
    except FlagsmithError as error:
        # Flagsmith has no transaction across these calls. Say what exists
        # now; running apply again picks up from there.
        done = [a for a in out.actions if a.startswith(("Created", "Added", "Connected", "Updated"))]
        raise ProvisionError(
            "Flagsmith refused a step: %s%s"
            % (
                error,
                " Before that, this run did the following, which stays in place and is reused when you "
                "apply again: %s" % " ".join(done) if done else " Nothing had been created yet.",
            )
        )


def _apply(client: Flagsmith, plan: Plan, out: Provisioned) -> Provisioned:
    dry_run = out.dry_run
    say = out.actions.append

    # 1. The flag that holds the versions. Look first; create later.
    feature = client.feature_by_name(plan.flag)
    if feature is not None:
        if feature.get("type") != "MULTIVARIATE":
            raise ProvisionError(
                "Flag '%s' exists but is a %s flag. Experiments need a multivariate "
                "flag; pick another flag name or convert it in Flagsmith."
                % (plan.flag, str(feature.get("type")).lower())
            )
        if feature.get("is_archived"):
            raise ProvisionError("Flag '%s' is archived in Flagsmith." % plan.flag)

    # Everything that could stop the run is checked here, before a single
    # variant or metric is created, so a refused plan leaves Flagsmith as it was.
    active: List[Dict[str, Any]] = []
    if feature is not None:
        conflicts = variant_conflicts(plan, client.variants(feature["id"]))
        if conflicts:
            raise ProvisionError(" ".join(conflicts) + " Change the plan or the flag so they agree.")
        active = [e for e in client.experiments(ACTIVE) if _feature_id(e) == feature["id"]]
        if active:
            current = active[0]
            if current["name"] != plan.name:
                raise ProvisionError(
                    "Flag '%s' already has an active experiment, '%s' (%s). Complete or "
                    "delete it before starting '%s'."
                    % (plan.flag, current["name"], current["status"], plan.name)
                )
            differences = differences_from_plan(
                plan, client.experiment(current["id"]), client.variants(feature["id"]), client.metrics()
            )
            if differences:
                raise ProvisionError(
                    "Experiment '%s' already exists in Flagsmith (%s) but is set up differently from this plan: %s "
                    "%s"
                    % (
                        current["name"], current["status"], " ".join(differences),
                        "Delete it in Flagsmith and apply again, or change the plan to match."
                        if current["status"] == "created"
                        else "A started experiment's setup should not change under it; finish this one first, "
                        "or change the plan back to match.",
                    )
                )
        else:
            problem = baseline_problem(client, feature, plan)
            if problem:
                raise ProvisionError(problem)

    # 2. Somewhere for events to go.
    connections = client.warehouse_connections()
    if not connections:
        if dry_run:
            say("Would connect the Flagsmith database as this environment's event warehouse.")
        else:
            try:
                client.connect_flagsmith_database()
                say("Connected the Flagsmith database as this environment's event warehouse.")
            except FlagsmithError as error:
                raise ProvisionError(
                    "This environment has no warehouse and the built-in one could not "
                    "be enabled (%s). Connect a warehouse in Environment Settings > "
                    "Warehouse, then run this again." % error
                )
    else:
        say("Warehouse already connected: %s (%s)." % (connections[0].get("name"), connections[0].get("status")))

    if feature is None:
        if dry_run:
            say("Would create multivariate flag '%s' with control value '%s'." % (plan.flag, plan.control_value))
        else:
            feature = client.create_multivariate_feature(
                plan.flag, plan.control_value, description=plan.hypothesis[:500]
            )
            say("Created multivariate flag '%s' with control value '%s'." % (plan.flag, plan.control_value))
    else:
        say("Flag '%s' already exists; reusing it." % plan.flag)
    out.feature_id = feature["id"] if feature else None

    # 3. One keyed variant per version. A variant without a key records no
    #    exposures, so keys are checked even on variants that already exist.
    existing = client.variants(out.feature_id) if out.feature_id else []
    by_key = {v["key"]: v for v in existing if v.get("key")}
    unkeyed_by_value = {variant_value(v): v for v in existing if not v.get("key")}
    for variant in plan.variants:
        if variant.key in by_key:
            found = by_key[variant.key]
            if variant_value(found) != variant.value:
                raise ProvisionError(
                    "Variant '%s' on flag '%s' serves '%s' in Flagsmith but the plan "
                    "says '%s'." % (variant.key, plan.flag, variant_value(found), variant.value)
                )
            out.variant_ids[variant.key] = found["id"]
            say("Variant '%s' already exists." % variant.key)
        elif variant.value in unkeyed_by_value:
            found = unkeyed_by_value[variant.value]
            if dry_run:
                say("Would give the existing variant serving '%s' the key '%s'." % (variant.value, variant.key))
            else:
                client.set_variant_key(out.feature_id, found, variant.key)
                say(
                    "Variant serving '%s' had no key (it would record no exposures); "
                    "set its key to '%s'." % (variant.value, variant.key)
                )
            out.variant_ids[variant.key] = found["id"]
        elif dry_run:
            say("Would add variant '%s' serving '%s'." % (variant.key, variant.value))
        else:
            created = client.create_variant(out.feature_id, variant.key, variant.value)
            if created.get("key") != variant.key:
                created = client.set_variant_key(out.feature_id, created, variant.key)
            out.variant_ids[variant.key] = created["id"]
            say("Added variant '%s' serving '%s'." % (variant.key, variant.value))
    planned_keys = {v.key for v in plan.variants}
    for stray in existing:
        if stray.get("key") not in planned_keys and stray["id"] not in out.variant_ids.values():
            say(
                "Note: flag '%s' has another variant (%s) that this plan does not use; "
                "it gets 0%% of experiment traffic."
                % (plan.flag, stray.get("key") or "no key, serves '%s'" % variant_value(stray))
            )

    # 4. Metrics, matched on the event and how it is aggregated.
    existing_metrics = client.metrics()
    for metric in plan.metrics:
        ident = metric.event + "|" + metric.aggregation
        match = next(
            (
                m
                for m in existing_metrics
                if (m.get("definition") or {}).get("event") == metric.event
                and m.get("aggregation") == metric.aggregation
                and m.get("direction") == metric.direction
            ),
            None,
        )
        if match:
            out.metric_ids[ident] = match["id"]
            shared = len(match.get("experiments") or [])
            say(
                "Metric '%s' already exists with the same event ('%s'), aggregation and direction; reusing it%s."
                % (match["name"], metric.event, " (%d other experiment(s) use it too)" % shared if shared else "")
            )
        elif dry_run:
            say("Would create metric '%s' counting the event '%s' (%s)." % (metric.name, metric.event, metric.aggregation))
        else:
            name = metric.name
            taken = {m["name"] for m in existing_metrics}
            if name in taken:
                name = "%s (%s, %s is better)" % (name, metric.aggregation, "lower" if metric.direction == "down" else "higher")
            created = client.create_metric(
                name, metric.event, metric.aggregation, metric.direction, metric.description
            )
            existing_metrics.append(created)
            out.metric_ids[ident] = created["id"]
            say("Created metric '%s' counting the event '%s' (%s)." % (name, metric.event, metric.aggregation))

    # 5. The experiment itself. Flagsmith allows one active experiment per flag
    #    and environment.
    if active:
        current = active[0]
        out.experiment_id = current["id"]
        out.experiment_status = current["status"]
        say("Experiment '%s' already exists (%s) and matches the plan; reusing it." % (current["name"], current["status"]))
        wanted = plan.hypothesis_with_marker()
        live = float(((client.experiment(current["id"]).get("experiment_rollout") or {}).get("rollout_percentage")) or 0)
        if current["status"] == "created" and live > 0:
            if dry_run:
                say("Would set its live rollout to 0%% until it is started (it is serving its split to %g%% of identities now, although not started)." % live)
            else:
                client.update_hypothesis(current["id"], wanted)
                client.set_rollout_percentage(client.experiment(current["id"]), 0)
                say(
                    "It was already serving its split to %g%% of identities although it had not been started "
                    "(Flagsmith serves a rollout from the moment it exists). Set its live rollout to 0%% until "
                    "it is started; the planned %g%% is recorded with it." % (live, plan.rollout_percentage)
                )
                return out
        if (current.get("hypothesis") or "") != wanted:
            if current["status"] == "created" and not dry_run:
                # Not started yet, so the hypothesis and the planned sample
                # recorded with it can still be set.
                client.update_hypothesis(current["id"], wanted)
                say("Updated its hypothesis and recorded the planned sample in Flagsmith.")
            elif current["status"] == "created":
                say("Would update its hypothesis and record the planned sample in Flagsmith.")
            else:
                say(
                    "Note: its hypothesis or planned sample in Flagsmith differs from this plan. It has already "
                    "started, so what Flagsmith holds was kept."
                )
    elif dry_run:
        say(
            "Would create experiment '%s': %g%% of identities, split %s."
            % (plan.name, plan.rollout_percentage, describe_split(plan))
        )
    else:
        created = client.create_experiment(
            plan.experiment_body(out.feature_id, out.variant_ids, out.metric_ids)
        )
        out.experiment_id = created["id"]
        out.experiment_status = created.get("status", "created")
        say(
            "Created experiment '%s' (id %d): planned for %g%% of identities, split %s. It is not started, "
            "and its live rollout is 0%% until it is, so nobody is served a variant yet."
            % (plan.name, created["id"], plan.rollout_percentage, describe_split(plan))
        )
    return out


def baseline_problem(client: Flagsmith, feature: Dict[str, Any], plan: Plan) -> Optional[str]:
    """Why this flag cannot host the planned experiment as it stands, if it cannot.

    Control has to be exactly what users get today. Flagsmith resets a flag's
    default variant shares to zero the moment an experiment is created on it,
    and serves the experiment's control value, enabled, to the control arm.
    Each of those is a silent change for real users unless control already is
    the current behaviour. Anything that cannot be confirmed is refused.
    """
    try:
        state = client.environment_default(feature["id"])
    except FlagsmithError as error:
        return (
            "Could not read what flag '%s' serves today (%s), so there is no way to confirm that the plan's "
            "control is the current behaviour." % (plan.flag, str(error)[:160])
        )
    if not state:
        return (
            "Flagsmith reports no value for flag '%s' in this environment, so there is no way to confirm that "
            "the plan's control is what users get today." % plan.flag
        )
    served = [
        (v.get("multivariate_feature_option"), float(v.get("percentage_allocation") or 0))
        for v in state.get("multivariate_feature_state_values") or []
        if float(v.get("percentage_allocation") or 0) > 0
    ]
    if served:
        names = {v["id"]: v.get("key") or variant_value(v) for v in client.variants(feature["id"])}
        return (
            "Flag '%s' currently serves %s to users by default. Creating an experiment on it makes Flagsmith "
            "reset that to the flag's base value ('%s') straight away, which would change what those users get "
            "before the experiment even starts. Use a new flag for this experiment, or have a person make the "
            "current version the flag's base value in Flagsmith first."
            % (
                plan.flag,
                " and ".join("'%s' (%g%%)" % (names.get(option, option), share) for option, share in served),
                state.get("feature_state_value"),
            )
        )
    if state.get("enabled") is False:
        return (
            "Flag '%s' is switched off in this environment. An experiment serves an enabled flag to everyone "
            "in it, so creating one would switch the feature on for those users. Have a person enable the "
            "flag first if that is intended." % plan.flag
        )
    base = state.get("feature_state_value")
    if not isinstance(base, str) or isinstance(base, bool):
        return (
            "Flag '%s' serves %s today. Referee runs experiments on text-valued flags only, because it "
            "serves the control value as text and that would change the value's type for the control arm. "
            "Use a text-valued flag for the experiment."
            % (plan.flag, "no value" if base is None else "the %s %r" % (type(base).__name__, base))
        )
    if base != plan.control_value:
        return (
            "The plan's control is '%s', but flag '%s' serves '%s' to everyone today. Control has to be what "
            "users get now; otherwise the control arm is itself a change, and most users would be moved to "
            "something new without anyone approving it. Make '%s' the control and put '%s' in a variant."
            % (plan.control_value, plan.flag, base, base, plan.control_value)
        )
    return None


def variant_conflicts(plan: Plan, variants: List[Dict[str, Any]]) -> List[str]:
    """Variants already on the flag that contradict the plan."""
    by_key = {v["key"]: v for v in variants if v.get("key")}
    found = []
    for variant in plan.variants:
        existing = by_key.get(variant.key)
        if existing is not None and variant_value(existing) != variant.value:
            found.append(
                "Variant '%s' on flag '%s' serves '%s' in Flagsmith but the plan says '%s'."
                % (variant.key, plan.flag, variant_value(existing), variant.value)
            )
    return found


def differences_from_plan(
    plan: Plan, experiment: Dict[str, Any], variants: List[Dict[str, Any]], metrics: List[Dict[str, Any]]
) -> List[str]:
    """Where an experiment in Flagsmith is set up differently from the plan.

    Works only from what already exists (the flag's variants and the
    environment's metrics), so it can run before anything is created.
    """
    found: List[str] = []
    variant_ids = {v["key"]: v["id"] for v in variants if v.get("key")}
    rollout = experiment.get("experiment_rollout") or {}
    if rollout:
        # Referee keeps the live rollout at 0% until the start and records the
        # intended one in the hypothesis note; an experiment made elsewhere
        # only has the live value.
        percentage = planned_rollout_from(experiment.get("hypothesis"))
        if percentage is None:
            percentage = rollout.get("rollout_percentage")
        if percentage is not None and abs(float(percentage) - plan.rollout_percentage) > 1e-6:
            found.append("It takes %g%% of identities; the plan says %g%%." % (percentage, plan.rollout_percentage))
        control = (rollout.get("feature_state_value") or {}).get("value")
        if control is not None and str(control) != plan.control_value:
            found.append("Its control serves '%s'; the plan says '%s'." % (control, plan.control_value))
        live = {
            v.get("multivariate_feature_option"): float(v.get("percentage_allocation") or 0)
            for v in rollout.get("multivariate_feature_state_values") or []
        }
        for variant in plan.variants:
            if variant.key not in variant_ids:
                found.append("Variant '%s' is in the plan but not on the flag." % variant.key)
                continue
            share = live.get(variant_ids[variant.key], 0.0)
            if abs(share - variant.share) > 1e-6:
                found.append("Variant '%s' gets %g%%; the plan says %g%%." % (variant.key, share, variant.share))
        planned_ids = {variant_ids.get(v.key) for v in plan.variants}
        extra = [option for option, share in live.items() if share > 0 and option not in planned_ids]
        if extra:
            found.append("It gives traffic to %d variant(s) the plan does not have." % len(extra))
    by_id = {m["id"]: m for m in metrics}
    attached = {}
    for item in experiment.get("metrics") or []:
        metric = by_id.get(item.get("metric")) or {}
        signature = ((metric.get("definition") or {}).get("event"), metric.get("aggregation"))
        attached[signature] = item.get("expected_direction")
    planned = set()
    for metric in plan.metrics:
        signature = (metric.event, metric.aggregation)
        planned.add(signature)
        if signature not in attached:
            found.append("Metric '%s' (%s) is in the plan but not attached to it." % (metric.name, metric.event))
        elif attached[signature] != metric.goal:
            found.append(
                "Metric '%s' is expected to '%s' there; the plan says '%s'."
                % (metric.name, str(attached[signature]).replace("_", " "), metric.goal.replace("_", " "))
            )
    unplanned = [signature for signature in attached if signature not in planned and signature[0]]
    if unplanned:
        found.append("It has %d metric(s) the plan does not list." % len(unplanned))
    return found


def describe_split(plan: Plan) -> str:
    parts = ["control %g%%" % plan.control_share]
    parts += ["%s %g%%" % (v.key, v.share) for v in plan.variants]
    return " / ".join(parts)


def _feature_id(experiment: Dict[str, Any]) -> Optional[int]:
    feature = experiment.get("feature")
    if isinstance(feature, dict):
        return feature.get("id")
    return feature


# ----------------------------------------------------------- stored plans


def plan_path(state_dir: Path, experiment_id: int) -> Path:
    return Path(state_dir) / "plans" / ("experiment-%d.json" % experiment_id)


def remember_plan(state_dir: Path, experiment_id: int, plan: Plan, environment_key: str) -> Path:
    """Keep the plan next to the experiment id so later verdicts can use it."""
    path = plan_path(state_dir, experiment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "experiment_id": experiment_id,
        "environment_key": environment_key,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan": plan.to_dict(),
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def recall_plan(state_dir: Path, experiment_id: int, environment_key: str = "") -> Optional[Plan]:
    path = plan_path(state_dir, experiment_id)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if environment_key and document.get("environment_key") not in ("", None, environment_key):
            return None
        return parse(document["plan"])
    except (ValueError, KeyError):
        return None
