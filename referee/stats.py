"""The statistics Referee adds on top of Flagsmith's results.

Flagsmith computes lift, the credible interval and the chance to beat control.
Referee does not recompute those; it reads them. What lives here is what a
careful experimenter checks around those numbers:

* a sample ratio check against the split the experiment was actually given,
* how many identities an experiment needs before its answer can be trusted,
* how long that will take at the traffic it is really getting.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Dict, List, NamedTuple, Optional, Sequence

_NORMAL = NormalDist()

# Flagsmith's own thresholds, repeated here so the two agree.
WIN_THRESHOLD = 0.95
LOSE_THRESHOLD = 0.05
SRM_ALARM = 0.001
MIN_IDENTITIES_PER_VARIANT = 50
MIN_CONVERSIONS_PER_VARIANT = 5
SRM_MIN_TOTAL = 100


class Moments(NamedTuple):
    """One arm of one metric, as the three totals Flagsmith reports."""

    n: int
    total: float
    squares: float

    @property
    def mean(self) -> float:
        return self.total / self.n

    @property
    def variance(self) -> float:
        return max(0.0, (self.squares - self.total**2 / self.n) / (self.n - 1))


def chance_higher(control: Optional[Moments], treatment: Optional[Moments]) -> Optional[float]:
    """Probability that the treatment's mean is really above control's.

    A plain two-sample comparison of the difference. Flagsmith's own inference
    is about the *relative* lift and is withheld below its data floors or when
    control's mean is zero; this still works there, which is what a guardrail
    on a rare bad event needs. ``None`` when either arm has under two
    identities.
    """
    if control is None or treatment is None or control.n < 2 or treatment.n < 2:
        return None
    difference = treatment.mean - control.mean
    standard_error = math.sqrt(control.variance / control.n + treatment.variance / treatment.n)
    if standard_error == 0:
        return 0.5 if difference == 0 else float(difference > 0)
    return _NORMAL.cdf(difference / standard_error)


def chi_squared_survival(statistic: float, degrees_of_freedom: int) -> float:
    """P(X >= statistic) for a chi-squared variable, exact for whole degrees."""
    if statistic <= 0:
        return 1.0
    y = statistic / 2.0
    if degrees_of_freedom % 2:
        a = 0.5
        survival = math.erfc(math.sqrt(y))
    else:
        a = 1.0
        survival = math.exp(-y)
    while a + 1.0 <= degrees_of_freedom / 2.0:
        survival += math.exp(a * math.log(y) - y - math.lgamma(a + 1.0))
        a += 1.0
    return min(1.0, max(0.0, survival))


def srm_p_value(
    observed: Sequence[int], expected_shares: Sequence[float]
) -> Optional[float]:
    """Probability that chance alone produced a split this far from the plan.

    Returns ``None`` when the question cannot be asked: fewer than two arms, no
    traffic, or an arm that was configured to receive nothing.
    """
    total = sum(observed)
    if len(observed) < 2 or len(observed) != len(expected_shares) or total == 0:
        return None
    share_total = sum(expected_shares)
    if share_total <= 0 or any(share <= 0 for share in expected_shares):
        return None
    shares = [share / share_total for share in expected_shares]
    statistic = sum(
        (count - total * share) ** 2 / (total * share)
        for count, share in zip(observed, shares)
    )
    return chi_squared_survival(statistic, len(observed) - 1)


def expected_shares(rollout: Dict[str, object], variant_keys: Dict[int, str]) -> Dict[str, float]:
    """The share of experiment traffic each arm should get.

    ``rollout`` is the ``experiment_rollout`` object Flagsmith returns. Variants
    take their configured allocation and control takes the remainder. Shares are
    relative to identities inside the experiment, so the rollout percentage
    itself does not matter here.
    """
    shares: Dict[str, float] = {}
    allocated = 0.0
    for value in rollout.get("multivariate_feature_state_values") or []:  # type: ignore[union-attr]
        option_id = value["multivariate_feature_option"]
        key = variant_keys.get(option_id)
        if key is None:
            continue
        share = float(value["percentage_allocation"])
        shares[key] = share / 100.0
        allocated += share
    shares["control"] = max(0.0, 100.0 - allocated) / 100.0
    return shares


def sample_size_per_arm(
    baseline_rate: float,
    relative_lift: float,
    power: float = 0.8,
    win_threshold: float = WIN_THRESHOLD,
    comparisons: int = 1,
) -> int:
    """Identities needed in each arm of a conversion experiment.

    The experiment "wins" when the chance to beat control passes
    ``win_threshold``. With the normal approximation Flagsmith uses, that is a
    one-sided test at ``1 - win_threshold``. ``power`` is the probability of
    reaching the threshold when the true relative lift is ``relative_lift``.
    With several treatment arms the threshold is tightened (Bonferroni) so that
    testing more versions does not make a false win more likely.
    """
    if not 0 < baseline_rate < 1:
        raise ValueError("baseline_rate must be between 0 and 1")
    if relative_lift == 0:
        raise ValueError("relative_lift must not be zero")
    if not 0 < power < 1:
        raise ValueError("power must be between 0 and 1")
    treated_rate = baseline_rate * (1 + relative_lift)
    if not 0 < treated_rate < 1:
        raise ValueError(
            "baseline_rate * (1 + relative_lift) must stay between 0 and 1"
        )
    alpha = (1 - win_threshold) / max(1, comparisons)
    z_alpha = _NORMAL.inv_cdf(1 - alpha)
    z_power = _NORMAL.inv_cdf(power)
    # Flagsmith judges the *relative* lift and gets its uncertainty with the
    # delta method, which is wider than a test on the plain difference. Sizing
    # with the same formula keeps the plan honest about what Flagsmith will
    # be able to conclude. `unit_variance` is n times the variance of the lift.
    control_variance = baseline_rate * (1 - baseline_rate)
    treated_variance = treated_rate * (1 - treated_rate)
    unit_variance = (
        treated_variance / baseline_rate**2
        + treated_rate**2 * control_variance / baseline_rate**4
    )
    return int(math.ceil((z_alpha + z_power) ** 2 * unit_variance / relative_lift**2))


def detectable_lift(
    baseline_rate: float,
    identities_per_arm: int,
    power: float = 0.8,
    win_threshold: float = WIN_THRESHOLD,
    comparisons: int = 1,
) -> Optional[float]:
    """Smallest relative lift an experiment of this size can reliably find."""
    if identities_per_arm < 2 or not 0 < baseline_rate < 1:
        return None
    low, high = 1e-4, (1 - baseline_rate) / baseline_rate - 1e-6
    if high <= low:
        return None
    try:
        if sample_size_per_arm(baseline_rate, high, power, win_threshold, comparisons) > identities_per_arm:
            return None
    except ValueError:
        return None
    for _ in range(60):
        middle = (low + high) / 2
        needed = sample_size_per_arm(baseline_rate, middle, power, win_threshold, comparisons)
        if needed > identities_per_arm:
            low = middle
        else:
            high = middle
    return high


def days_to_reach(
    identities_needed_per_arm: int,
    daily_identities: float,
    rollout_percentage: float,
    smallest_share: float,
) -> Optional[float]:
    """Days until the least-served arm has the identities it needs."""
    per_day = daily_identities * (rollout_percentage / 100.0) * smallest_share
    if per_day <= 0:
        return None
    return identities_needed_per_arm / per_day


def threshold_for(comparisons: int, win_threshold: float = WIN_THRESHOLD) -> float:
    """Chance-to-win a variant must reach when several variants are tested."""
    return 1 - (1 - win_threshold) / max(1, comparisons)


def rate(successes: float, total: float) -> Optional[float]:
    return successes / total if total else None


def totals_by_variant(points: List[Dict[str, object]], field: str) -> Dict[str, int]:
    """Sum a Flagsmith timeseries (``new_identities`` and the like) per variant."""
    totals: Dict[str, int] = {}
    for point in points or []:
        for variant, count in (point.get(field) or {}).items():  # type: ignore[union-attr]
            totals[variant] = totals.get(variant, 0) + int(count)
    return totals
