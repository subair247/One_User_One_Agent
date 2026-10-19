"""Small statistics helpers (standard library only)."""
from __future__ import annotations

import math


def diff_ci(s1, n1, s0, n0, z=1.96):
    """Difference in proportions (group 1 - group 0) with a Wald 95% CI and a two-sided z-test p-value."""
    if n1 == 0 or n0 == 0:
        return {"p1": None, "p0": None, "diff": None, "ci_low": None, "ci_high": None, "p_value": None}
    p1, p0 = s1 / n1, s0 / n0
    se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    d = p1 - p0
    pp = (s1 + s0) / (n1 + n0)
    se0 = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n0))
    zstat = d / se0 if se0 > 0 else 0.0
    p = math.erfc(abs(zstat) / math.sqrt(2))
    return {"p1": p1, "p0": p0, "diff": d, "ci_low": d - z * se, "ci_high": d + z * se, "p_value": p}
