"""Dependency-free interval and agreement statistics."""

import math
from collections import Counter
from collections.abc import Sequence

# Two-sided 95% normal critical value.
Z_95 = 1.959963984540054


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float] | None:
    """Return the Wilson score interval for a binomial proportion.

    Returns ``None`` when ``total`` is zero, because no interval exists yet.

    Raises:
        ValueError: If counts are negative or ``successes`` exceeds ``total``.

    """
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("Counts must satisfy 0 <= successes <= total.")
    if total == 0:
        return None
    proportion = successes / total
    z2 = z * z
    # Wilson centre and half-width (Wilson 1927), well behaved at 0 and n.
    denominator = 1 + z2 / total
    centre = (proportion + z2 / (2 * total)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / total + z2 / (4 * total * total))
    half /= denominator
    # Clamp tiny floating-point excursions outside [0, 1].
    return max(0.0, centre - half), min(1.0, centre + half)


def cohen_kappa(first: Sequence[str], second: Sequence[str]) -> float | None:
    """Return Cohen's kappa for two raters' paired categorical labels.

    Returns ``None`` for empty input. When expected agreement is 1 (both raters
    used one identical category throughout) kappa is undefined; this returns 1.0
    if observed agreement is also perfect and ``None`` otherwise.

    Raises:
        ValueError: If the sequences differ in length.

    """
    if len(first) != len(second):
        raise ValueError("Both raters must label the same items.")
    total = len(first)
    if total == 0:
        return None
    # Observed agreement is the share of identical paired labels.
    observed = sum(a == b for a, b in zip(first, second, strict=True)) / total
    counts_a, counts_b = Counter(first), Counter(second)
    # Expected chance agreement from each rater's marginal distribution.
    expected = sum(counts_a[label] * counts_b[label] for label in counts_a) / (total * total)
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else None
    return (observed - expected) / (1 - expected)
