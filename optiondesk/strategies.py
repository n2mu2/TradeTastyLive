"""Strategy construction, payoff maths and strike selection.

Everything here works on :class:`~optiondesk.model.OptionChain` objects, so the
same builders serve the live scanner, the dashboard and the tests.

Payoff convention: a plan's P&L at expiry for underlying price ``S`` is

    pnl(S) = sum(signed_qty_i * intrinsic_i(S)) + net_credit_total

with ``net_credit_total`` positive when money is *received* at entry.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Optional, Sequence

import numpy as np

from .model import ChainRow, ExitRule, Leg, OptionChain, OptionQuote, TradePlan
from .quant import (
    DAYS_PER_YEAR, breakevens_from_payoff, bs_greeks, expected_move,
    prob_above, prob_touch, years_to_expiry,
)

# --------------------------------------------------------------------------- #
# Payoff maths
# --------------------------------------------------------------------------- #
def _intrinsic(option_type: str, spot: float, strike: float) -> float:
    return max(spot - strike, 0.0) if option_type.upper().startswith("C") else max(strike - spot, 0.0)


def pnl_at(legs: Sequence[Leg], net_credit_total: float, spot: float) -> float:
    total = net_credit_total
    for leg in legs:
        total += leg.signed_qty * _intrinsic(leg.option_type, spot, leg.strike)
    return total


def payoff_curve(legs: Sequence[Leg], net_credit_total: float,
                 grid: Sequence[float]) -> np.ndarray:
    return np.array([pnl_at(legs, net_credit_total, s) for s in grid], dtype=float)


def analyze_payoff(legs: Sequence[Leg], net_credit_total: float, spot: float,
                   pad_pct: float = 0.35, points: int = 161) -> dict:
    """Exact max profit / loss / breakevens for a piecewise-linear payoff.

    Extrema of a piecewise linear function live at the kinks (the strikes) or in
    the two tails, so we evaluate there instead of trusting a coarse grid.
    """
    if not legs:
        return {"max_profit": 0.0, "max_loss": 0.0, "breakevens": [], "grid": [], "pnl": [],
                "unbounded_up": False, "unbounded_down": False}

    strikes = sorted({leg.strike for leg in legs})
    lo = max(min(strikes + [spot]) * (1 - pad_pct), 0.0)
    hi = max(strikes + [spot]) * (1 + pad_pct)

    kinks = [lo] + [s for s in strikes if lo < s < hi] + [hi]
    values = [pnl_at(legs, net_credit_total, k) for k in kinks]

    # As S -> +inf, tail slope is determined by net calls:
    call_qty = sum(l.signed_qty for l in legs if l.option_type.upper().startswith("C"))
    unbounded_up_profit = call_qty > 0
    unbounded_up_loss = call_qty < 0

    # Underlying stock price cannot fall below 0, so downside is always bounded at S=0:
    pnl_at_zero = pnl_at(legs, net_credit_total, 0.0)
    all_values = values + [pnl_at_zero]

    max_profit = max(all_values)
    max_loss = -min(all_values)
    if unbounded_up_profit:
        max_profit = float("inf")
    if unbounded_up_loss:
        max_loss = float("inf")

    kinks = sorted(set([0.0] + [s for s in strikes if s > 0] + [hi]))
    kink_values = [pnl_at(legs, net_credit_total, k) for k in kinks]

    exact_bes: list[float] = []
    for i in range(len(kinks) - 1):
        x0, x1 = kinks[i], kinks[i + 1]
        y0, y1 = kink_values[i], kink_values[i + 1]
        if abs(y0) < 1e-9:
            exact_bes.append(x0)
        elif (y0 < 0 < y1) or (y0 > 0 > y1):
            exact_bes.append(x0 - y0 * (x1 - x0) / (y1 - y0))

    grid = np.linspace(lo, hi, points)
    curve = payoff_curve(legs, net_credit_total, grid)
    return {
        "max_profit": float(max_profit),
        "max_loss": float(max_loss),
        "breakevens": exact_bes,
        "grid": grid.tolist(),
        "pnl": curve.tolist(),
        "unbounded_up": bool(unbounded_up_profit or unbounded_up_loss),
        "unbounded_down": False,
    }


def net_greeks(legs: Sequence[Leg]) -> dict[str, float]:
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        out["delta"] += leg.signed_qty * leg.delta
        out["gamma"] += leg.signed_qty * leg.gamma
        out["theta"] += leg.signed_qty * leg.theta
        out["vega"] += leg.signed_qty * leg.vega
    return {k: round(v, 4) for k, v in out.items()}


# --------------------------------------------------------------------------- #
# Strike selection
# --------------------------------------------------------------------------- #
def atm_strike(chain: OptionChain) -> float:
    if not chain.rows:
        return chain.spot
    return min((r.strike for r in chain.rows), key=lambda k: abs(k - chain.spot))


def select_strike_by_delta(chain: OptionChain, option_type: str, target_delta: float,
                           above_spot: bool = False, min_premium: float = 0.05,
                           max_spread_pct: float = 100.0) -> Optional[OptionQuote]:
    """Pick the strike whose |delta| is closest to ``target_delta``.

    ``above_spot`` filters to OTM calls (strike > spot) or OTM puts (strike < spot),
    which is what premium-selling setups need.
    """
    target = abs(target_delta)
    best: Optional[OptionQuote] = None
    best_err = float("inf")
    for q in chain.quotes(option_type):
        if q.mid < min_premium:
            continue
        if max_spread_pct < 100.0 and q.spread_pct > max_spread_pct:
            continue
        if above_spot and option_type.upper().startswith("C") and q.strike <= chain.spot:
            continue
        if above_spot and option_type.upper().startswith("P") and q.strike >= chain.spot:
            continue
        err = abs(abs(q.delta) - target)
        if err < best_err:
            best, best_err = q, err
    return best


def select_strike_offset(chain: OptionChain, option_type: str, offset_points: float,
                         round_up: bool = True) -> Optional[ChainRow]:
    """Nearest strike ``offset_points`` away from spot (used for wings)."""
    if not chain.rows:
        return None
    target = chain.spot + offset_points if option_type.upper().startswith("C") else chain.spot - offset_points
    candidates = [r for r in chain.rows if (r.strike >= target if round_up else True)]
    pool = candidates or chain.rows
    return min(pool, key=lambda r: abs(r.strike - target))


def find_quote(chain: OptionChain, strike: float, option_type: str) -> Optional[OptionQuote]:
    row = chain.nearest_row(strike)
    return row.side(option_type) if row else None


# --------------------------------------------------------------------------- #
# Leg / plan assembly
# --------------------------------------------------------------------------- #
def make_leg(quote: OptionQuote, side: str, lots: int = 1, lot_size: int = 1,
             price: Optional[float] = None) -> Leg:
    return Leg(
        side=side.upper(),
        option_type=quote.option_type.upper(),
        strike=quote.strike,
        premium=price if price is not None else quote.mid,
        lots=lots,
        lot_size=lot_size,
        iv=quote.iv,
        delta=quote.delta,
        gamma=quote.gamma,
        theta=quote.theta,
        vega=quote.vega,
        token=quote.token,
        symbol=quote.symbol,
    )


def entry_price(quote: OptionQuote, side: str, slippage_ticks: float = 0.0) -> float:
    """Conservative fill assumption: pay the ask when buying, hit the bid when selling."""
    tick = 0.05
    if side.upper() == "BUY":
        base = quote.ask if quote.ask > 0 else quote.mid
        return max(base + slippage_ticks * tick, 0.0)
    base = quote.bid if quote.bid > 0 else quote.mid
    return max(base - slippage_ticks * tick, 0.0)


def build_plan(strategy: str, legs: Sequence[Leg], chain: OptionChain, iv: float,
               iv_rank: float = 0.0, iv_percentile: float = 0.0, lots: int = 1,
               reasons: Optional[list[str]] = None, warnings: Optional[list[str]] = None,
               exit_rules: Optional[list[ExitRule]] = None, score: float = 0.0,
               margin_estimate: float = 0.0, r: float = 0.065, q: float = 0.0) -> TradePlan:
    legs = [replace(l, lots=lots) for l in legs]
    lot_size = legs[0].lot_size if legs else chain.lot_size
    net_credit_per_unit = sum(l.cash for l in legs) / (lots * lot_size) if legs else 0.0
    net_credit_total = net_credit_per_unit * lots * lot_size

    t = years_to_expiry(max(chain.dte, 0))
    analysis = analyze_payoff(legs, net_credit_total, chain.spot)

    short_legs = [l for l in legs if l.side.upper() == "SELL"]
    long_legs = [l for l in legs if l.side.upper() == "BUY"]

    prob_short_otm = _prob_short_otm(chain, short_legs, iv, r, q)
    prob_max_profit = _prob_max_profit(chain, analysis["breakevens"], legs, iv, r, q)
    prob_touch = max((_prob_touch(chain, l, iv, r, q) for l in short_legs), default=0.0)

    breakevens = sorted(analysis["breakevens"])
    grid = analysis["grid"]
    pnl = analysis["pnl"]

    plan = TradePlan(
        strategy=strategy,
        underlying=chain.underlying,
        expiry=chain.expiry,
        dte=chain.dte,
        spot=chain.spot,
        legs=list(legs),
        lot_size=lot_size,
        lots=lots,
        net_credit=round(net_credit_per_unit, 3),
        max_profit=round(analysis["max_profit"], 2) if math.isfinite(analysis["max_profit"]) else float("inf"),
        max_loss=round(analysis["max_loss"], 2) if math.isfinite(analysis["max_loss"]) else float("inf"),
        breakevens=[round(b, 2) for b in breakevens],
        prob_short_otm=round(prob_short_otm, 4),
        prob_max_profit=round(prob_max_profit, 4),
        prob_touch_short=round(prob_touch, 4),
        margin_estimate=round(margin_estimate, 2),
        iv_rank=round(iv_rank, 2),
        iv_percentile=round(iv_percentile, 2),
        iv=round(iv, 4),
        score=round(score, 2),
        reasons=list(reasons or []),
        warnings=list(warnings or []),
        exit_rules=list(exit_rules or []),
        payoff={"prices": [round(p, 2) for p in grid], "pnl": [round(v, 2) for v in pnl]},
        greeks=net_greeks(legs),
    )
    plan.capital_used = round(max(plan.max_loss if math.isfinite(plan.max_loss) else 0.0,
                                  margin_estimate), 2)
    _validate(plan, short_legs, long_legs, t)
    return plan


def _validate(plan: TradePlan, short_legs: Sequence[Leg], long_legs: Sequence[Leg],
              t: float) -> None:
    """Add honest warnings; never silently ship a broken plan."""
    if short_legs and not long_legs:
        plan.warnings.append("Naked short option: loss is unlimited on one side.")
    if "credit" in plan.strategy.lower() and plan.net_credit <= 0:
        plan.warnings.append("Credit spread priced at or below zero -- no premium to collect.")
    if plan.dte <= 0:
        plan.warnings.append("Expiry is today: gamma risk is extreme and theta is front-loaded.")
    if plan.prob_short_otm and plan.prob_short_otm < 0.5:
        plan.warnings.append("Short strikes are currently ITM (POP below 50 %).")
    for leg in plan.legs:
        if leg.premium <= 0:
            plan.warnings.append(f"Leg {leg.symbol or leg.strike} has no usable quote.")
            break


def _prob_short_otm(chain: OptionChain, short_legs: Sequence[Leg], iv: float,
                    r: float, q: float) -> float:
    """Probability every short leg expires OTM.

    For an iron condor (short call + short put) the two events are perfectly
    dependent -- the right answer is P(short_put < S_T < short_call), not the
    product of two marginals -- so that case is computed exactly.
    """
    if not short_legs:
        return 0.0
    t = years_to_expiry(max(chain.dte, 0))
    vol = iv or 1e-6
    short_calls = [l for l in short_legs if l.option_type.upper().startswith("C")]
    short_puts = [l for l in short_legs if l.option_type.upper().startswith("P")]
    if short_calls and short_puts:
        put_strike = max(l.strike for l in short_puts)
        call_strike = min(l.strike for l in short_calls)
        if call_strike <= put_strike:
            return 0.0
        return prob_above(chain.spot, put_strike, t, vol, r, q) - \
            prob_above(chain.spot, call_strike, t, vol, r, q)
    prob = 1.0
    for leg in short_legs:
        if leg.option_type.upper().startswith("C"):
            prob *= 1.0 - prob_above(chain.spot, leg.strike, t, vol, r, q)
        else:
            prob *= prob_above(chain.spot, leg.strike, t, vol, r, q)
    return prob


def _prob_max_profit(chain: OptionChain, breakevens: Sequence[float], legs: Sequence[Leg],
                     iv: float, r: float, q: float) -> float:
    """Probability of finishing past the breakeven(s), based on the payoff shape."""
    if not breakevens:
        return 0.0
    t = years_to_expiry(max(chain.dte, 0))
    vols = [l.iv for l in legs if l.iv > 0] or [iv]
    vol = iv or (sum(vols) / len(vols))
    if vol <= 0:
        return 0.0
    mid = pnl_at(legs, sum(l.cash for l in legs), chain.spot)
    # Which side of the breakevens is profitable? Evaluate just inside each region.
    lo_b, hi_b = min(breakevens), max(breakevens)
    below = pnl_at(legs, sum(l.cash for l in legs), lo_b - 1e-3)
    above = pnl_at(legs, sum(l.cash for l in legs), hi_b + 1e-3)
    total = 0.0
    if below > 0:
        total += 1.0 - prob_above(chain.spot, lo_b, t, vol, r, q)
    if above > 0:
        total += prob_above(chain.spot, hi_b, t, vol, r, q)
    if len(breakevens) >= 2 and below <= 0 and above <= 0:
        total = prob_above(chain.spot, lo_b, t, vol, r, q) - prob_above(chain.spot, hi_b, t, vol, r, q)
    return min(max(total, 0.0), 1.0)


def _prob_touch(chain: OptionChain, leg: Leg, iv: float, r: float, q: float) -> float:
    t = years_to_expiry(max(chain.dte, 0))
    return prob_touch(chain.spot, leg.strike, t, iv or leg.iv or 1e-6, r, q)


# --------------------------------------------------------------------------- #
# Margin approximation (Indian exchange style)
# --------------------------------------------------------------------------- #
def margin_estimate(legs: Sequence[Leg], spot: float, iv: float, dte: int,
                    lot_size: int, lots: int, span_pct: float = 0.12,
                    elmi_pct: float = 0.03) -> float:
    """Rough NSE-style margin.

    Defined-risk spreads are charged close to their maximum loss (the exchange
    gives a spread benefit), so we use ``max_loss`` plus a small buffer. Naked
    short options get a SPAN + ELMI style charge on the notional. **This is an
    estimate only -- always confirm with the broker's RMS before sending orders.**
    """
    net_credit_total = sum(l.cash for l in legs)
    analysis = analyze_payoff(legs, net_credit_total, spot)
    max_loss = analysis["max_loss"]
    if math.isfinite(max_loss):
        return max(max_loss * 1.02, 0.0)
    notional = spot * lot_size * lots
    short_qty = sum(l.quantity for l in legs if l.side.upper() == "SELL") / max(lot_size, 1)
    t = years_to_expiry(max(dte, 0))
    span = span_pct * notional * max(short_qty, 1) * min(max(math.sqrt(t * DAYS_PER_YEAR / 30.0), 0.3), 1.5)
    elmi = elmi_pct * notional * max(short_qty, 1)
    long_offset = sum(l.premium * l.quantity for l in legs if l.side.upper() == "BUY")
    return max(span + elmi - long_offset, 0.0)
