"""Shared filters, trend context, scoring and position sizing.

These are the mechanical rules the course teaches, turned into numbers:
trade with the trend, sell premium when vol is rich, buy premium when it is
cheap, never risk more than a fixed slice of capital, and never trade a strike
whose book is too wide to enter or exit cleanly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from ..config import AccountCfg
from ..indicators import adx, last_finite, momentum, rsi, sma
from ..model import OptionChain, OptionQuote
from ..quant import expected_move


@dataclass
class Trend:
    direction: str          # bullish / bearish / neutral
    fast: float
    slow: float
    rsi: float
    adx: float
    momentum_pct: float
    reasons: list[str] = field(default_factory=list)

    @property
    def strength(self) -> float:
        """0-100 confidence in the trend call."""
        score = 0.0
        if self.direction == "bullish":
            score += 35
        elif self.direction == "bearish":
            score += 35
        if self.adx and self.adx >= 20:
            score += min((self.adx - 20) * 1.5, 25)
        if abs(self.momentum_pct) >= 5:
            score += 15
        elif abs(self.momentum_pct) >= 2.5:
            score += 8
        if 52 <= self.rsi <= 70 or 30 <= self.rsi <= 48:
            score += 15
        return round(min(score, 100.0), 1)


def trend_of(closes: Sequence[float], fast: int = 20, slow: int = 50,
             rsi_window: int = 14, mom_window: int = 20) -> Trend:
    arr = np.asarray(list(closes), dtype=float)
    if arr.size < slow + 2:
        return Trend("neutral", float("nan"), float("nan"), 50.0, 0.0, 0.0,
                     ["not enough history for a trend read"])
    f = last_finite(sma(arr, fast))
    s = last_finite(sma(arr, slow))
    price = float(arr[-1])
    r = last_finite(rsi(arr, rsi_window))
    a = last_finite(adx(_synthetic_hl(arr), _synthetic_hl(arr, invert=True), arr)["adx"])
    m = last_finite(momentum(arr, mom_window))

    reasons: list[str] = []
    if price > f > s:
        direction = "bullish"
        reasons.append(f"strong uptrend: price {price:,.2f} > SMA{fast} {f:,.2f} > SMA{slow} {s:,.2f}")
    elif f > s and price >= s * 0.99 and r >= 48:
        direction = "bullish"
        reasons.append(f"uptrend pullback: SMA{fast} > SMA{slow}, price holding near moving averages (RSI {r:.0f})")
    elif price < f < s:
        direction = "bearish"
        reasons.append(f"strong downtrend: price {price:,.2f} < SMA{fast} {f:,.2f} < SMA{slow} {s:,.2f}")
    elif f < s and price <= s * 1.01 and r <= 52:
        direction = "bearish"
        reasons.append(f"downtrend bounce: SMA{fast} < SMA{slow}, price below moving averages (RSI {r:.0f})")
    else:
        direction = "neutral"
        reasons.append("mixed trend: price and moving averages not aligned")
    return Trend(direction, f, s, float(r), float(a), float(m), reasons)


def _synthetic_hl(closes: np.ndarray, invert: bool = False) -> np.ndarray:
    """Daily closes-only feeds have no high/low; approximate with a small band."""
    band = np.abs(np.diff(closes, prepend=closes[0])) * 0.5 + np.abs(closes) * 0.0015
    return closes + band if not invert else closes - band


# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #
@dataclass
class LiquidityCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok


def liquidity(quote: Optional[OptionQuote], min_oi: int, max_spread_pct: float,
              min_premium: float = 0.05) -> LiquidityCheck:
    problems: list[str] = []
    if quote is None:
        return LiquidityCheck(False, ["no quote for this strike"])
    if quote.mid <= 0:
        problems.append("no tradable price")
    if quote.mid < min_premium:
        problems.append(f"premium {quote.mid:.2f} below {min_premium}")
    if quote.spread_pct > max_spread_pct:
        problems.append(f"bid-ask {quote.spread_pct:.1f}% > {max_spread_pct}%")
    if quote.oi < min_oi:
        problems.append(f"OI {quote.oi} < {min_oi}")
    return LiquidityCheck(not problems, problems)


# --------------------------------------------------------------------------- #
# Position sizing
# --------------------------------------------------------------------------- #
def risk_budget(account: AccountCfg) -> float:
    return account.capital * account.max_risk_per_trade_pct / 100.0


def credit_spread_risk_per_lot(width_points: float, credit_per_unit: float,
                               lot_size: int) -> float:
    return max(width_points - credit_per_unit, 0.0) * lot_size


def long_option_risk_per_lot(premium_per_unit: float, lot_size: int) -> float:
    return premium_per_unit * lot_size


def lots_for_risk(risk_per_lot: float, budget: float, max_lots: int = 10) -> int:
    if risk_per_lot <= 0:
        return 0
    lots = int(math.floor(budget / risk_per_lot))
    if lots == 0 and risk_per_lot <= budget * 1.6:
        # Permit 1 lot if discrete lot size slightly exceeds strict 1-trade budget
        return 1
    return int(max(0, min(max_lots, lots)))


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_credit_setup(pop: float, iv_rank: float, credit_ratio: float,
                       trend_alignment: float, liquidity_quality: float) -> float:
    """0-100 score for a premium-selling (hedged) setup."""
    pop_score = max(0.0, min((pop - 0.55) / 0.30, 1.0)) * 35          # up to 35
    ivr_score = max(0.0, min(iv_rank / 80.0, 1.0)) * 25               # up to 25
    credit_score = max(0.0, min((credit_ratio - 0.20) / 0.30, 1.0)) * 20
    trend_score = trend_alignment * 12
    liq_score = liquidity_quality * 8
    return round(pop_score + ivr_score + credit_score + trend_score + liq_score, 1)


def score_long_option(iv_rank: float, trend_strength: float, delta_quality: float,
                      cost_quality: float, liquidity_quality: float) -> float:
    """0-100 score for a premium-buying (next month stock option) setup."""
    ivr_score = max(0.0, min((60.0 - iv_rank) / 55.0, 1.0)) * 30      # cheaper vol = better
    trend_score = (trend_strength / 100.0) * 30
    delta_score = delta_quality * 20
    cost_score = cost_quality * 12
    liq_score = liquidity_quality * 8
    return round(ivr_score + trend_score + delta_score + cost_score + liq_score, 1)


def liquidity_quality(quote: Optional[OptionQuote], min_oi: int,
                      max_spread_pct: float) -> float:
    if quote is None or quote.mid <= 0:
        return 0.0
    oi_q = min(quote.oi / max(min_oi * 4, 1), 1.0)
    spread_q = max(0.0, 1.0 - quote.spread_pct / max(max_spread_pct * 2, 1e-6))
    vol_q = min(quote.volume / 50_000, 1.0)
    return round(0.45 * oi_q + 0.4 * spread_q + 0.15 * vol_q, 3)


def delta_quality(delta: float, target: float, tolerance: float = 0.20) -> float:
    return max(0.0, 1.0 - abs(abs(delta) - target) / tolerance)


def cost_quality(premium: float, spot: float, max_pct: float) -> float:
    if spot <= 0:
        return 0.0
    pct = premium / spot * 100.0
    return max(0.0, min(1.0, 1.0 - (pct / max(max_pct, 1e-6)) * 0.6))


def trend_alignment(trend: Trend, direction: str) -> float:
    """How well a directional premium-sell lines up with the trend (0-1)."""
    if direction == "neutral":
        return 0.75
    if trend.direction == direction:
        return 1.0
    if trend.direction == "neutral":
        return 0.6
    return 0.15


def expected_move_points(chain: OptionChain, iv: float) -> float:
    return expected_move(chain.spot, iv, max(chain.dte, 0), 1.0)
