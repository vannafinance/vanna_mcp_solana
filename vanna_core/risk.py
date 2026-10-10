"""Deterministic risk and sizing — a 1:1 port of the Copilot's `lib/copilot/risk.ts` (vanna-credit-backend).

The Copilot's rule is that the language model never computes an amount: code does, from numbers the chain
returned. When an MCP client (Claude, Cursor) replaces the Copilot's Gemini, that rule has to live on this
side of the protocol, so the same arithmetic is here, behind tools. Nothing in this module does I/O.

Health model (vanna_core/math_utils.py, program `math/health.rs`): HF = collateral_usd / debt_usd, the program
liquidates at HF <= 1.10, no debt means no health factor. Borrowed funds stay in the margin vault and count as
collateral. The product sizes to a 1.15 floor (policy, `MIN_HEALTH_FACTOR`), stricter than the program.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from vanna_core.math_utils import BALANCE_TO_BORROW_THRESHOLD_WAD, WAD

# The program's liquidation line (BALANCE_TO_BORROW_THRESHOLD_WAD, 1.10): derived, never a second copy.
LIQUIDATION_HF = BALANCE_TO_BORROW_THRESHOLD_WAD / WAD
# Ratio above the floor that is still flagged as thin.
THIN_BUFFER = 0.10
# Slippage a lever plan assumes its swap may cost (matches the MCP's default `slippage_bps`).
DEFAULT_SLIPPAGE_BPS = 100
# Product policy: the largest leverage the planner will size at all.
MAX_LEVERAGE = 10.0
# Keeps a little SOL back from "all" wallet spends for fees and rent (TOOLS_CATALOGUE §2.2).
SOL_FEE_RESERVE = 0.05
# A debt keeps accruing between a plan and the moment a step is built; cover a little more than today's figure.
DEBT_BUFFER = 1.003
# Margin collateral left after repaying that is not worth a withdrawal transaction.
DUST_UNITS = {"SOL": 1e-6, "USDC": 0.01, "USDT": 0.01, "JitoSOL": 1e-6, "JupSOL": 1e-6}

MAX_AMOUNT = 1e12
MAX_FRACTION_DIGITS = 12


def num(value: Any) -> float | None:
    """A finite number from an MCP value (number or decimal string), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str) and value.strip():
        try:
            n = float(value)
        except ValueError:
            return None
        return n if math.isfinite(n) else None
    return None


def token_decimals(symbol: str) -> int:
    from vanna_core.assets import symbol_decimals

    return symbol_decimals(symbol)


def floor_decimals(n: float, dp: int) -> str:
    """Round DOWN to `dp` decimals as a plain decimal string (no exponent, trailing zeros trimmed)."""
    if not math.isfinite(n) or n <= 0:
        return "0"
    factor = 10**dp
    value = math.floor(n * factor + 1e-9) / factor
    text = f"{value:.{dp}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def ceil_units(value: float, symbol: str) -> str:
    """Round UP to the token's decimals (an input sized to cover a target must never round below it)."""
    dp = token_decimals(symbol)
    factor = 10**dp
    text = f"{math.ceil(value * factor - 1e-9) / factor:.{dp}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def usd_to_units(usd: float, price_usd: float, symbol: str) -> str:
    if not price_usd > 0:
        return "0"
    return floor_decimals(usd / price_usd, token_decimals(symbol))


# ------------------------------------------------------------------ typed amounts (what a client may pass)


@dataclass(frozen=True)
class AmountIssue:
    kind: str  # "invalid" | "ambiguous"
    message: str


_PLAIN_AMOUNT = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)$")
_GROUPED_AMOUNT = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def parse_amount(raw: Any, symbol: str | None = None) -> tuple[str | None, AmountIssue | None]:
    """One typed number -> an exact positive decimal string, or the reason it cannot be used.

    Never guesses (mirrors the Copilot's `parseAmountToken`): a sign, zero, NaN/Infinity, a lone comma, an amount
    beyond the product ceiling, scientific notation, or more precision than the token has are reported, never
    reinterpreted or rounded — rounding a user's amount would silently change it.
    """
    shown = str(raw).strip() if raw is not None else ""
    if not shown:
        return None, AmountIssue("invalid", "The amount is empty.")
    text = shown.lower()
    if re.match(r"^[-−–]", text):
        return None, AmountIssue("invalid", f"Amounts have to be positive; {shown!r} is not an amount I can use.")
    text = text.lstrip("+")
    if re.fullmatch(r"nan|\w*infinity|inf", text):
        return None, AmountIssue("invalid", f"{shown!r} is not a number.")
    if "e" in text:
        return None, AmountIssue("invalid", f"{shown!r} uses scientific notation. Write the amount as a plain decimal.")
    if _GROUPED_AMOUNT.match(text):
        text = text.replace(",", "")
    elif not _PLAIN_AMOUNT.match(text):
        if re.fullmatch(r"[\d.,]+", text):
            return None, AmountIssue(
                "ambiguous",
                f"I can't tell how to read {shown!r}: the comma and dot could mean thousands or decimals. "
                "Write it plainly, like 1000.5.",
            )
        return None, AmountIssue("invalid", f"{shown!r} is not a valid amount.")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, AmountIssue("invalid", f"{shown!r} is not a valid amount.")
    if value == 0:
        return None, AmountIssue("invalid", f"The amount has to be above zero ({shown!r} is zero).")
    if value > Decimal(str(MAX_AMOUNT)):
        return None, AmountIssue("invalid", f"{shown!r} is larger than any amount I will prepare.")
    plain = format(value.normalize(), "f")
    if "." in plain:
        plain = plain.rstrip("0").rstrip(".")
    fraction = plain.split(".")[1] if "." in plain else ""
    if len(fraction) > MAX_FRACTION_DIGITS:
        return None, AmountIssue("invalid", f"{shown!r} has more decimal places than any token supports.")
    if symbol is not None:
        dp = token_decimals(symbol)
        if len(fraction) > dp:
            return None, AmountIssue(
                "invalid",
                f"{symbol} has {dp} decimal places, and {plain} has {len(fraction)}. "
                f"I won't round your amount; give it with at most {dp} decimals.",
            )
    return plain, None


# ------------------------------------------------------------------ sizing


def max_borrow_usd(collateral: float, debt: float, floor: float, borrowed_is_collateral: bool = True) -> float:
    """Largest USD borrow that keeps HF >= floor. Borrowed funds count as collateral when they stay in the vault."""
    x = (collateral - floor * debt) / (floor - 1) if borrowed_is_collateral else collateral / floor - debt
    return max(0.0, x)


def max_withdraw_usd(collateral: float, debt: float, floor: float) -> float:
    """Largest USD of collateral that can leave while HF stays >= floor. With no debt everything can leave."""
    if debt <= 0:
        return collateral
    return max(0.0, collateral - floor * debt)


def hf_after(collateral: float, debt: float) -> float | None:
    return collateral / debt if debt > 0 else None


def max_leverage(
    *, collateral_usd: float, debt_usd: float, equity_usd: float, deposit_usd: float, floor: float, slippage_bps: int
) -> float:
    """Highest leverage on `equity_usd` that holds HF >= floor given existing collateral/debt and swap slippage."""
    if not equity_usd > 0:
        return 1.0
    s = 1 - slippage_bps / 10_000
    c0 = collateral_usd + deposit_usd
    denom = floor - s
    b = (c0 - floor * debt_usd) / denom if denom > 0 else math.inf
    if not b > 0:
        return 1.0
    return 1 + b / equity_usd


@dataclass
class LeverSize:
    ok: bool
    borrow_usd: float = 0.0
    hf_after: float | None = None  # None = no debt afterwards
    reason: str = ""
    max_leverage: float | None = None


def size_lever(
    *,
    collateral_usd: float,
    debt_usd: float,
    equity_usd: float,
    deposit_usd: float,
    leverage: float,
    floor: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> LeverSize:
    """"Lever X Nx": borrow (N-1) x equity in a stable, swap it to X inside the margin account.

    Worst-case HF is taken with the swap at its minimum received (slippage), so the floor holds even when the
    quote moves against the user.
    """
    if not equity_usd > 0:
        return LeverSize(False, reason="There is no balance of that asset to lever.")
    borrow_usd = (leverage - 1) * equity_usd
    s = 1 - slippage_bps / 10_000
    worst = hf_after(collateral_usd + deposit_usd + borrow_usd * s, debt_usd + borrow_usd)
    if worst is not None and worst < floor:
        top = max_leverage(
            collateral_usd=collateral_usd, debt_usd=debt_usd, equity_usd=equity_usd,
            deposit_usd=deposit_usd, floor=floor, slippage_bps=slippage_bps,
        )
        safe = math.floor(top * 10) / 10  # round DOWN to one decimal so the offered figure is itself safe
        return LeverSize(
            False,
            reason=f"{leverage:g}x would take your health factor to about {worst:.2f}, below the {floor:g} floor.",
            max_leverage=safe if safe > 1 else None,
        )
    return LeverSize(True, borrow_usd=borrow_usd, hf_after=worst)


# ------------------------------------------------------------------ gate


@dataclass
class RiskResult:
    decision: str  # "allow" | "needs_confirmation" | "block"
    reasons: list[str] = field(default_factory=list)
    projected_health_factor: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": self.reasons,
            "projected_health_factor": self.projected_health_factor,
        }


_UNKNOWN = object()


def risk_gate(*, increases_risk: bool, hf_after_value: Any, floor: float) -> RiskResult:
    """Risk decision for one action. Only actions that can lower health are gated.

    An action that adds collateral or repays debt is always allowed, even from an unhealthy account (that is how a
    user gets out). `hf_after_value`: pass `_UNKNOWN`-like None-vs-missing via the `UNKNOWN` sentinel below.
    """
    if not increases_risk:
        projected = None if hf_after_value is UNKNOWN else hf_after_value
        return RiskResult("allow", [], projected)
    if hf_after_value is UNKNOWN:
        return RiskResult(
            "block", ["I could not get a projected health factor for this action, so it will not be built."], None
        )
    if hf_after_value is None:  # no debt afterwards
        return RiskResult("allow", [], None)
    if hf_after_value < floor:
        return RiskResult(
            "block",
            [
                f"This would take the health factor to {hf_after_value:.2f}, below the {floor:.2f} safety floor "
                f"(liquidation is at {LIQUIDATION_HF:.2f})."
            ],
            hf_after_value,
        )
    if hf_after_value < floor + THIN_BUFFER:
        return RiskResult(
            "needs_confirmation",
            [
                f"The health factor would be {hf_after_value:.2f}, close to the {floor:.2f} floor. "
                "A small price move could push it lower."
            ],
            hf_after_value,
        )
    return RiskResult("allow", [], hf_after_value)


UNKNOWN = _UNKNOWN
