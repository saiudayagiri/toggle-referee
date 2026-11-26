"""The experiment plan: one JSON document a person can read and review.

A plan says what is being tested, how success is measured, how traffic is
split, and what was assumed about traffic when the plan was written. Referee
turns a plan into a Flagsmith flag, metrics and experiment, and later judges
the experiment against the same plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import stats

GOALS = ("increase", "decrease", "not_increase", "not_decrease")
AGGREGATIONS = ("occurrence", "count", "sum", "mean")
_FLAG_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_FLAG_NAME = 150
MAX_NAME = 255
MAX_TEXT = 2000
# Control has to keep a real share of the traffic. Below this the "experiment"
# is a rollout of the new version under another name, and rollouts are for a
# person to approve.
MIN_CONTROL_SHARE = 10.0
MIN_LIFT = 0.001
MIN_PLANNED_SAMPLE = 200
PLAN_KEYS = ("name", "hypothesis", "flag", "control", "variants", "rollout_percentage", "metrics", "assumptions")
ASSUMPTION_KEYS = ("baseline_rate", "minimum_lift", "daily_identities", "power", "deadline")
# What Referee records at the end of an experiment's hypothesis in Flagsmith,
# so that it travels with the experiment instead of living in one checkout:
# the rollout the experiment is meant to run at (Referee keeps the live rollout
# at 0% until the experiment is started, because Flagsmith serves a rollout
# from the moment it exists), and the planned sample that is the stopping point.
_MARKER = re.compile(
    r"\s*\[Referee: (?:rollout (\d+(?:\.\d+)?)%; )?"
    r"(?:planned sample (\d+) identities per version, fixed before start|no planned sample)\]\s*$"
)


def marker(rollout_percentage: float, per_arm: Optional[int]) -> str:
    sample = "planned sample %d identities per version, fixed before start" % per_arm if per_arm else "no planned sample"
    # Two decimals at most (the plan validator guarantees it), written plainly
    # so that what is read back is exactly what was planned.
    rollout = ("%.2f" % rollout_percentage).rstrip("0").rstrip(".")
    return "\n\n[Referee: rollout %s%%; %s]" % (rollout, sample)


def planned_sample_from(hypothesis: Optional[str]) -> Optional[int]:
    """The planned sample recorded in an experiment's hypothesis, if any."""
    found = _MARKER.search(hypothesis or "")
    return int(found.group(2)) if found and found.group(2) else None


def planned_rollout_from(hypothesis: Optional[str]) -> Optional[float]:
    """The rollout percentage the experiment is meant to run at, if recorded.

    A note that has been edited into something impossible is ignored.
    """
    found = _MARKER.search(hypothesis or "")
    if not found or not found.group(1) or len(found.group(1)) > 8:
        return None
    value = float(found.group(1))
    return value if 0 < value <= 100 else None


def without_marker(hypothesis: Optional[str]) -> str:
    return _MARKER.sub("", hypothesis or "").strip()


_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")

# Flagsmith stores a metric's own direction and, separately, what an
# experiment expects of it. A guardrail ("must not increase") is a metric where
# lower is better and the expectation is that it does not get worse.
_DIRECTION_FOR_GOAL = {
    "increase": "up",
    "not_decrease": "up",
    "decrease": "down",
    "not_increase": "down",
}


class PlanError(ValueError):
    """The plan cannot be used as written. ``problems`` lists every reason."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class Variant:
    key: str
    value: str
    share: float
    description: str = ""


@dataclass
class MetricPlan:
    name: str
    event: str
    goal: str = "increase"
    aggregation: str = "occurrence"
    description: str = ""

    @property
    def direction(self) -> str:
        return _DIRECTION_FOR_GOAL[self.goal]

    @property
    def is_guardrail(self) -> bool:
        return self.goal in ("not_increase", "not_decrease")


@dataclass
class Assumptions:
    """What the plan assumes about the world; used for sizing only."""

    baseline_rate: Optional[float] = None
    minimum_lift: Optional[float] = None
    daily_identities: Optional[float] = None
    power: float = 0.8
    deadline: Optional[str] = None


@dataclass
class Plan:
    name: str
    hypothesis: str
    flag: str
    control_value: str
    variants: List[Variant]
    metrics: List[MetricPlan]
    rollout_percentage: float = 100.0
    control_description: str = ""
    assumptions: Assumptions = field(default_factory=Assumptions)

    # ------------------------------------------------------------- derived

    @property
    def primary(self) -> MetricPlan:
        for metric in self.metrics:
            if not metric.is_guardrail:
                return metric
        return self.metrics[0]

    @property
    def guardrails(self) -> List[MetricPlan]:
        return [m for m in self.metrics if m.is_guardrail]

    @property
    def control_share(self) -> float:
        return 100.0 - sum(v.share for v in self.variants)

    @property
    def smallest_share(self) -> float:
        return min([self.control_share] + [v.share for v in self.variants]) / 100.0

    def hypothesis_with_marker(self) -> str:
        """The hypothesis as stored in Flagsmith: with the planned rollout and sample appended."""
        try:
            per_arm = self.sizing().get("per_arm")
        except ValueError:
            per_arm = None
        return self.hypothesis + marker(self.rollout_percentage, per_arm)

    def sizing(self) -> Dict[str, Any]:
        """Sample size and duration implied by the plan's assumptions."""
        a = self.assumptions
        out: Dict[str, Any] = {
            "comparisons": len(self.variants),
            "win_threshold": stats.threshold_for(len(self.variants)),
            "per_arm": None,
            "total_in_experiment": None,
            "days": None,
            "finishes_before_deadline": None,
            "detectable_lift_by_deadline": None,
            "notes": [],
        }
        if self.primary.aggregation != "occurrence":
            out["notes"].append(
                "Sizing is only estimated for occurrence (conversion) metrics; "
                "the primary metric here is '%s'." % self.primary.aggregation
            )
            return out
        if a.baseline_rate is None or a.minimum_lift is None:
            out["notes"].append(
                "Add assumptions.baseline_rate and assumptions.minimum_lift to "
                "get a sample size."
            )
            return out
        lift = a.minimum_lift if self.primary.goal != "decrease" else -abs(a.minimum_lift)
        per_arm = stats.sample_size_per_arm(
            a.baseline_rate, lift, a.power, comparisons=len(self.variants)
        )
        if per_arm < MIN_PLANNED_SAMPLE:
            out["notes"].append(
                "The assumptions alone would ask for only %d identities per version (they describe a very "
                "large effect). The planned sample is held at %d, the smallest Referee will treat as a stopping point."
                % (per_arm, MIN_PLANNED_SAMPLE)
            )
        per_arm = max(per_arm, MIN_PLANNED_SAMPLE)
        out["per_arm"] = per_arm
        out["total_in_experiment"] = int(round(per_arm / self.smallest_share))
        if a.daily_identities:
            days = stats.days_to_reach(
                per_arm, a.daily_identities, self.rollout_percentage, self.smallest_share
            )
            out["days"] = days
            hours_left = hours_until(a.deadline)
            if hours_left is not None and days is not None:
                out["hours_until_deadline"] = hours_left
                out["finishes_before_deadline"] = days * 24 <= hours_left
                reachable = int(
                    a.daily_identities
                    * (hours_left / 24.0)
                    * (self.rollout_percentage / 100.0)
                    * self.smallest_share
                )
                out["identities_per_arm_by_deadline"] = reachable
                out["detectable_lift_by_deadline"] = stats.detectable_lift(
                    a.baseline_rate, reachable, a.power, comparisons=len(self.variants)
                )
        else:
            out["notes"].append(
                "Add assumptions.daily_identities to estimate how long this takes."
            )
        return out

    # ------------------------------------------------------- serialisation

    def to_dict(self) -> Dict[str, Any]:
        a = self.assumptions
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "flag": self.flag,
            "control": {
                "value": self.control_value,
                "description": self.control_description,
            },
            "variants": [
                {
                    "key": v.key,
                    "value": v.value,
                    "share": v.share,
                    "description": v.description,
                }
                for v in self.variants
            ],
            "rollout_percentage": self.rollout_percentage,
            "metrics": [
                {
                    "name": m.name,
                    "event": m.event,
                    "goal": m.goal,
                    "aggregation": m.aggregation,
                    "description": m.description,
                }
                for m in self.metrics
            ],
            "assumptions": {
                "baseline_rate": a.baseline_rate,
                "minimum_lift": a.minimum_lift,
                "daily_identities": a.daily_identities,
                "power": a.power,
                "deadline": a.deadline,
            },
        }

    def experiment_body(self, feature_id: int, variant_ids: Dict[str, int], metric_ids: Dict[str, int]) -> Dict[str, Any]:
        """The request body for Flagsmith's create-experiment endpoint."""
        return {
            "feature": feature_id,
            "name": self.name,
            "hypothesis": self.hypothesis_with_marker(),
            "metrics": [
                {"metric": metric_ids[m.event + "|" + m.aggregation], "expected_direction": m.goal}
                for m in self.metrics
            ],
            "experiment_rollout": {
                "enabled": True,
                # Flagsmith serves an experiment's rollout as soon as it exists,
                # started or not. It is therefore created at 0% and raised to
                # the planned percentage (kept in the hypothesis note) on start.
                "rollout_percentage": 0,
                "feature_state_value": {"type": "string", "value": self.control_value},
                "multivariate_feature_state_values": [
                    {
                        "multivariate_feature_option": variant_ids[v.key],
                        "percentage_allocation": v.share,
                    }
                    for v in self.variants
                ],
            },
        }


def hours_until(deadline: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    when = parse_time(deadline)
    if when is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds() / 3600.0)


def parse_time(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 time; a time without an offset is read as UTC."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.9 only accepts 3 or 6 fractional digits.
    text = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), text)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _number(value: Any, label: str, problems: List[str], low: float, high: float, optional: bool = True) -> Optional[float]:
    if value is None:
        if not optional:
            problems.append("%s is required." % label)
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append("%s must be a number." % label)
        return None
    try:
        in_range = low <= float(value) <= high and float(value) == float(value)
    except OverflowError:
        in_range = False
    if not in_range:
        problems.append("%s must be between %g and %g." % (label, low, high))
        return None
    return float(value)


def parse(data: Any) -> Plan:
    """Validate a plan document and return it, or raise ``PlanError``."""
    problems: List[str] = []
    if not isinstance(data, dict):
        raise PlanError(["The plan must be a JSON object."])
    for key in data:
        if key not in PLAN_KEYS:
            problems.append("Unknown key '%s'%s." % (str(key)[:60], _did_you_mean(str(key), PLAN_KEYS)))

    def text(key: str, label: str, required: bool = True, source: Optional[Dict[str, Any]] = None) -> str:
        value = (source if source is not None else data).get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                problems.append("%s is required." % label)
            return ""
        if not isinstance(value, str):
            problems.append("%s must be text." % label)
            return ""
        if _CONTROL_CHARS.search(value):
            problems.append("%s contains control characters." % label)
            return ""
        if len(value) > MAX_TEXT:
            problems.append("%s must be at most %d characters." % (label, MAX_TEXT))
            return ""
        return value.strip()

    name = text("name", "name")
    hypothesis = text("hypothesis", "hypothesis")
    flag = text("flag", "flag")
    if flag and not _FLAG_NAME.match(flag):
        problems.append(
            "flag '%s' must be lower case letters, digits, '_' or '-' "
            "(Flagsmith projects enforce lower-case names by default)." % flag
        )
    if len(name) > MAX_NAME:
        problems.append("name must be at most %d characters." % MAX_NAME)
    if len(flag) > MAX_FLAG_NAME:
        problems.append("flag must be at most %d characters." % MAX_FLAG_NAME)
    if _MARKER.search(hypothesis):
        problems.append("hypothesis must not end with a '[Referee: ...]' note; Referee writes that itself.")

    control = data.get("control")
    if isinstance(control, str):
        control = {"value": control}
    if not isinstance(control, dict):
        problems.append("control must be an object with a 'value'.")
        control = {}
    control_value = text("value", "control.value", source=control)
    control_description = text("description", "control.description", False, control)

    variants: List[Variant] = []
    raw_variants = data.get("variants")
    if not isinstance(raw_variants, list) or not raw_variants:
        problems.append("variants must list at least one variant to test against control.")
        raw_variants = []
    seen_keys = set()
    for index, raw in enumerate(raw_variants):
        label = "variants[%d]" % index
        if not isinstance(raw, dict):
            problems.append("%s must be an object." % label)
            continue
        key = text("key", label + ".key", source=raw)
        if key:
            if not _KEY.match(key):
                problems.append("%s.key '%s' may only use letters, digits, '_' and '-'." % (label, key))
            if key == "control":
                problems.append("%s.key cannot be 'control'; Flagsmith reserves it for the flag's own value." % label)
            if key in seen_keys:
                problems.append("%s.key '%s' is used twice." % (label, key))
            seen_keys.add(key)
        value = raw.get("value", key)
        if not isinstance(value, str) or not value or _CONTROL_CHARS.search(value) or len(value) > MAX_TEXT:
            problems.append("%s.value must be non-empty text (quote numbers and booleans, for example \"2\")." % label)
            value = ""
        if value and value in [v.value for v in variants]:
            problems.append("%s.value '%s' is served by another variant too, so the two could not be told apart." % (label, value[:60]))
        if len(key) > MAX_NAME:
            problems.append("%s.key must be at most %d characters." % (label, MAX_NAME))
        if value and value == control_value:
            problems.append("%s.value is the same as the control value, so the two arms would be identical." % label)
        share = _number(raw.get("share"), label + ".share", problems, 0.0, 100.0, optional=False)
        if share is not None and share <= 0:
            problems.append("%s.share must be above 0, or the variant gets no traffic." % label)
        variants.append(
            Variant(key=key, value=value, share=share or 0.0, description=str(raw.get("description") or ""))
        )
    total_share = sum(v.share for v in variants)
    if total_share > 100.0 + 1e-9:
        problems.append("Variant shares add up to %g%%; they must leave room for control." % total_share)
    elif variants and 100.0 - total_share <= 0:
        problems.append("Variant shares add up to 100%, leaving no traffic for control.")
    elif variants and all(v.share > 0 for v in variants) and 100.0 - total_share < MIN_CONTROL_SHARE:
        problems.append(
            "Control keeps only %g%% of the experiment's traffic; it must keep at least %g%%. Giving nearly "
            "everyone the new version is a rollout, not an experiment, and a rollout needs a person's approval."
            % (100.0 - total_share, MIN_CONTROL_SHARE)
        )

    rollout = _number(data.get("rollout_percentage", 100), "rollout_percentage", problems, 0.0, 100.0)
    if rollout is not None and rollout <= 0:
        problems.append("rollout_percentage must be above 0, or nobody enters the experiment.")
    elif rollout is not None and (rollout < 0.01 or abs(rollout * 100 - round(rollout * 100)) > 1e-6):
        problems.append("rollout_percentage must be at least 0.01 and have at most two decimal places.")

    metrics: List[MetricPlan] = []
    raw_metrics = data.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        problems.append("metrics must list at least one metric.")
        raw_metrics = []
    seen_metrics = set()
    for index, raw in enumerate(raw_metrics):
        label = "metrics[%d]" % index
        if not isinstance(raw, dict):
            problems.append("%s must be an object." % label)
            continue
        event = text("event", label + ".event", source=raw)
        if event.startswith("$"):
            problems.append("%s.event cannot start with '$'; Flagsmith reserves that prefix." % label)
        goal = raw.get("goal", "increase")
        if goal not in GOALS:
            problems.append("%s.goal must be one of %s." % (label, ", ".join(GOALS)))
            goal = "increase"
        aggregation = raw.get("aggregation", "occurrence")
        if aggregation not in AGGREGATIONS:
            problems.append("%s.aggregation must be one of %s." % (label, ", ".join(AGGREGATIONS)))
            aggregation = "occurrence"
        identity = (event, aggregation)
        if event and identity in seen_metrics:
            problems.append("%s repeats the metric '%s' (%s)." % (label, event, aggregation))
        seen_metrics.add(identity)
        metric_name = text("name", label + ".name", False, raw) or event
        if metric_name and metric_name in [m.name for m in metrics]:
            problems.append("%s.name '%s' is used by another metric in this plan." % (label, metric_name[:60]))
        if len(event) > 256 or len(metric_name) > MAX_NAME:
            problems.append("%s: the event and name must be at most 256 and %d characters." % (label, MAX_NAME))
        metrics.append(
            MetricPlan(
                name=metric_name,
                event=event,
                goal=goal,
                aggregation=aggregation,
                description=str(raw.get("description") or ""),
            )
        )
    if metrics and all(m.is_guardrail for m in metrics):
        problems.append(
            "Every metric is a guardrail. At least one metric needs the goal "
            "'increase' or 'decrease' so there is something to win on."
        )

    raw_assumptions = data.get("assumptions") or {}
    if not isinstance(raw_assumptions, dict):
        problems.append("assumptions must be an object.")
        raw_assumptions = {}
    for key in raw_assumptions:
        if key not in ASSUMPTION_KEYS:
            problems.append("Unknown key 'assumptions.%s'%s." % (str(key)[:60], _did_you_mean(str(key), ASSUMPTION_KEYS)))
    baseline = _number(raw_assumptions.get("baseline_rate"), "assumptions.baseline_rate", problems, 0.0, 1.0)
    if baseline is not None and not 0 < baseline < 1:
        problems.append("assumptions.baseline_rate must be strictly between 0 and 1.")
        baseline = None
    lift = _number(raw_assumptions.get("minimum_lift"), "assumptions.minimum_lift", problems, -1.0, 100.0)
    if lift is not None and abs(lift) < MIN_LIFT:
        problems.append("assumptions.minimum_lift must be at least %g (0.1%%) in size; smaller lifts cannot be measured in practice." % MIN_LIFT)
        lift = None
    goals = [m for m in metrics if not m.is_guardrail]
    shrinking = bool(goals) and goals[0].goal == "decrease"
    if baseline is not None and lift is not None:
        if shrinking and abs(lift) >= 1:
            problems.append("assumptions.minimum_lift must be below 1 for a metric that should decrease (1 would mean it drops to zero).")
            lift = None
        elif not shrinking and not 0 < baseline * (1 + abs(lift)) < 1:
            problems.append("assumptions: baseline_rate with minimum_lift applied must stay below 100%.")
            lift = None
    daily = _number(raw_assumptions.get("daily_identities"), "assumptions.daily_identities", problems, 0.0, 1e12)
    # The sizing is the stopping rule, so it may not be made trivially easy:
    # at least the conventional 80% power, and a planned sample no smaller
    # than MIN_PLANNED_SAMPLE whatever the assumptions say.
    power = _number(raw_assumptions.get("power", 0.8), "assumptions.power", problems, 0.8, 0.99)
    deadline = raw_assumptions.get("deadline")
    if deadline is not None and parse_time(deadline) is None:
        problems.append("assumptions.deadline must be an ISO 8601 time, for example 2026-11-29T20:00:00+05:30.")
        deadline = None

    if problems:
        raise PlanError(problems)
    return Plan(
        name=name,
        hypothesis=hypothesis,
        flag=flag,
        control_value=control_value,
        control_description=control_description,
        variants=variants,
        metrics=metrics,
        rollout_percentage=rollout if rollout is not None else 100.0,
        assumptions=Assumptions(
            baseline_rate=baseline,
            minimum_lift=abs(lift) if lift is not None else None,
            daily_identities=daily,
            power=power if power is not None else 0.8,
            deadline=deadline,
        ),
    )


def _did_you_mean(key: str, known: Any) -> str:
    import difflib

    close = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
    return " (did you mean '%s'?)" % close[0] if close else ""


def load(path: Path) -> Plan:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlanError(["No plan file at %s." % path])
    except ValueError as error:
        raise PlanError(["%s is not valid JSON: %s" % (path, error)])
    return parse(data)


def save(plan: Plan, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "experiment"
