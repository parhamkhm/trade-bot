"""The pre-registered G3 thresholds (docs/SPEC.md section 7.2 E), applied literally and never adjusted.

Every constant below is the SPEC's number. A function here returns a ``GateResult`` carrying the measured
value, the threshold and the verdict; none of them has an escape hatch, a tolerance or a "close enough".
Where the SPEC leaves a corner undefined (a missing Sharpe, a non-positive benchmark) the rule chosen is the
strict one (undefined -> fail) and the corner is flagged in ``GateResult.note``.

All numbers fed to these functions are PROVISIONAL (SPEC 7.2 F) until the frozen-curve rerun.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tbot.validation.stats import annualised_sharpe

__all__ = [
    "DD_RATIO_MAX",
    "DSR_MIN",
    "NEIGHBOUR_KEEP_MIN",
    "PBO_MAX",
    "SHARPE_RATIO_MIN",
    "GateResult",
    "drawdown_gate",
    "dsr_gate",
    "eth_gate",
    "neighbour_gate",
    "pbo_gate",
    "ratio_variants_gate",
    "sharpe_gate",
    "sharpe_of",
    "stress_gate",
    "total_return",
]

DD_RATIO_MAX = 0.60  # maxDD <= 0.60 x maxDD(B&H)
SHARPE_RATIO_MIN = 0.80  # Sharpe >= 0.80 x Sharpe(B&H)
PBO_MAX = 0.30  # PBO < 0.30 (strict)
DSR_MIN = 0.95  # DSR >= 0.95
NEIGHBOUR_KEEP_MIN = 0.70  # each neighbour keeps >= 70 % of the reference Sharpe


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    value: float | None
    threshold: float | None
    rule: str
    note: str = ""


def total_return(returns: Sequence[float]) -> float:
    """Compounded net return of a return series."""
    equity = 1.0
    for r in returns:
        equity *= 1.0 + r
    return equity - 1.0


def drawdown_gate(candidate_max_dd: float, benchmark_max_dd: float) -> GateResult:
    """maxDD(candidate) <= 0.60 x maxDD(B&H). Both are positive fractions (0.5 = a 50 % drawdown)."""
    limit = DD_RATIO_MAX * benchmark_max_dd
    return GateResult(
        "maxDD vs B&H",
        passed=candidate_max_dd <= limit,
        value=candidate_max_dd,
        threshold=limit,
        rule=f"maxDD <= {DD_RATIO_MAX} x maxDD(B&H)",
    )


def sharpe_gate(candidate_sharpe: float | None, benchmark_sharpe: float | None) -> GateResult:
    """Sharpe(candidate) >= 0.80 x Sharpe(B&H), literally.

    An undefined Sharpe (a flat series, or no benchmark) fails. When the B&H Sharpe is not positive the
    product 0.80 x Sharpe(B&H) is LARGER than Sharpe(B&H), i.e. the literal rule is stricter than "match B&H";
    it is applied as written and the corner is flagged.
    """
    rule = f"Sharpe >= {SHARPE_RATIO_MIN} x Sharpe(B&H)"
    if candidate_sharpe is None or benchmark_sharpe is None:
        return GateResult("Sharpe vs B&H", False, candidate_sharpe, None, rule, "undefined Sharpe: fail")
    limit = SHARPE_RATIO_MIN * benchmark_sharpe
    note = (
        "B&H Sharpe <= 0: the literal 0.80x rule is stricter than B&H itself" if benchmark_sharpe <= 0 else ""
    )
    return GateResult("Sharpe vs B&H", candidate_sharpe >= limit, candidate_sharpe, limit, rule, note)


def pbo_gate(pbo: float) -> GateResult:
    return GateResult("PBO", pbo < PBO_MAX, pbo, PBO_MAX, f"PBO < {PBO_MAX}")


def dsr_gate(dsr: float | None) -> GateResult:
    if dsr is None:
        return GateResult(
            "DSR", False, None, DSR_MIN, f"DSR >= {DSR_MIN}", "flat series: DSR undefined, fail"
        )
    return GateResult("DSR", dsr >= DSR_MIN, dsr, DSR_MIN, f"DSR >= {DSR_MIN}")


def neighbour_gate(
    reference_sharpe: float | None, neighbour_sharpes: Mapping[str, float | None]
) -> GateResult:
    """Every +-1-step neighbour keeps >= 70 % of the reference point's fixed-parameter pooled OOS Sharpe.

    If the reference Sharpe is not positive, "70 % of it" is not a meaningful bar (0.7 x negative lies above
    the value), so the test fails and says why: a candidate whose own chosen point does not earn a positive
    Sharpe cannot have a robust optimum. A neighbour with an undefined Sharpe fails.
    """
    rule = f"each neighbour Sharpe >= {NEIGHBOUR_KEEP_MIN} x Sharpe(P)"
    if reference_sharpe is None or reference_sharpe <= 0:
        return GateResult(
            "neighbours", False, reference_sharpe, None, rule, "reference Sharpe <= 0 or undefined: fail"
        )
    limit = NEIGHBOUR_KEEP_MIN * reference_sharpe
    failing = [k for k, s in neighbour_sharpes.items() if s is None or s < limit]
    note = "failing: " + ", ".join(failing) if failing else ""
    if not neighbour_sharpes:
        note = "no neighbour exists"
    return GateResult(
        "neighbours", not failing and bool(neighbour_sharpes), reference_sharpe, limit, rule, note
    )


def eth_gate(eth_sharpe: float | None) -> GateResult:
    return GateResult(
        "ETH/USDT Sharpe",
        eth_sharpe is not None and eth_sharpe > 0,
        eth_sharpe,
        0.0,
        "Sharpe > 0 (BTC choices reused)",
    )


def stress_gate(stress_returns: Sequence[float]) -> GateResult:
    """x2 costs keep the strategy profitable: the compounded pooled OOS manual-delay net return is > 0."""
    value = total_return(stress_returns)
    return GateResult(
        "x2 cost stress", value > 0.0, value, 0.0, "net total return > 0 at 20 bps fee + 2x slippage"
    )


def ratio_variants_gate(
    reference_sharpe: float | None, variant_sharpes: Mapping[str, float | None]
) -> GateResult:
    """S0's six section-7.1 robustness variants each keep >= 70 % of S0's Sharpe (as the neighbours)."""
    res = neighbour_gate(reference_sharpe, variant_sharpes)
    return GateResult("S0 robustness variants", res.passed, res.value, res.threshold, res.rule, res.note)


def sharpe_of(returns: Sequence[float], periods_per_year: float = 365.0) -> float | None:
    return annualised_sharpe(returns, periods_per_year)
