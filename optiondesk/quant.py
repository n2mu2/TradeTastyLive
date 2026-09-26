"""Black-Scholes pricing, greeks and probability maths.

Everything in this module is pure math on floats / numpy arrays so it can be
unit-tested without any market data. Conventions used throughout the project:

* ``t``  -> time to expiry in **calendar years** (days / 365).
* ``r``  -> risk free rate as a decimal (0.065 = 6.5 %).
* ``q``  -> continuous dividend yield as a decimal (Nifty ~0.012).
* ``vol``-> annualised implied volatility as a decimal (0.14 = 14 %).
* ``delta``/``gamma``/``rho`` are per 1 unit of underlying / rate,
  ``vega`` and ``theta`` are returned **per 1 vol point** and **per day**
  respectively, because that is how terminals display them.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np

SQRT_2PI = math.sqrt(2.0 * math.pi)
DAYS_PER_YEAR = 365.0
MIN_T = 1.0 / (365.0 * 24 * 60)  # ~1 minute, avoids divide-by-zero on expiry


# --------------------------------------------------------------------------- #
# Normal distribution helpers (no scipy dependency)
# --------------------------------------------------------------------------- #
def norm_cdf(x: float | np.ndarray) -> float | np.ndarray:
    """Standard normal CDF via the error function."""
    if isinstance(x, np.ndarray):
        from scipy.special import ndtr  # local import: only needed for vectors

        return ndtr(x)
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float | np.ndarray) -> float | np.ndarray:
    if isinstance(x, np.ndarray):
        return np.exp(-0.5 * x * x) / SQRT_2PI
    return math.exp(-0.5 * x * x) / SQRT_2PI


def years_to_expiry(days: float) -> float:
    """Calendar days -> years, floored at :data:`MIN_T`."""
    return max(days, 0.0) / DAYS_PER_YEAR or MIN_T


# --------------------------------------------------------------------------- #
# Black-Scholes
# --------------------------------------------------------------------------- #
def d1_d2(spot: float, strike: float, t: float, vol: float, r: float = 0.0,
          q: float = 0.0) -> tuple[float, float]:
    t = max(t, MIN_T)
    vol = max(vol, 1e-6)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / (vol * sqrt_t)
    return d1, d1 - vol * sqrt_t


def bs_price(spot: float, strike: float, t: float, vol: float,
             option_type: str = "CE", r: float = 0.0, q: float = 0.0) -> float:
    """European option price. ``option_type`` is ``CE``/``call`` or ``PE``/``put``."""
    d1, d2 = d1_d2(spot, strike, t, vol, r, q)
    disc_r = math.exp(-r * max(t, MIN_T))
    disc_q = math.exp(-q * max(t, MIN_T))
    if _is_call(option_type):
        return spot * disc_q * norm_cdf(d1) - strike * disc_r * norm_cdf(d2)
    return strike * disc_r * norm_cdf(-d2) - spot * disc_q * norm_cdf(-d1)


def intrinsic_value(spot: float, strike: float, option_type: str = "CE") -> float:
    return max(spot - strike, 0.0) if _is_call(option_type) else max(strike - spot, 0.0)


def extrinsic_value(spot: float, strike: float, t: float, vol: float,
                    option_type: str = "CE", r: float = 0.0, q: float = 0.0) -> float:
    return bs_price(spot, strike, t, vol, option_type, r, q) - intrinsic_value(spot, strike, option_type)


def bs_greeks(spot: float, strike: float, t: float, vol: float,
              option_type: str = "CE", r: float = 0.0, q: float = 0.0) -> dict[str, float]:
    """Price + delta, gamma, vega (per 1 vol point), theta (per day), rho (per 1 %)."""
    t = max(t, MIN_T)
    vol = max(vol, 1e-6)
    d1, d2 = d1_d2(spot, strike, t, vol, r, q)
    sqrt_t = math.sqrt(t)
    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)
    pdf_d1 = norm_pdf(d1)
    call = _is_call(option_type)

    price = bs_price(spot, strike, t, vol, option_type, r, q)
    gamma = disc_q * pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * disc_q * pdf_d1 * sqrt_t
    common_theta = -spot * disc_q * pdf_d1 * vol / (2.0 * sqrt_t)

    if call:
        delta = disc_q * norm_cdf(d1)
        theta = common_theta - r * strike * disc_r * norm_cdf(d2) + q * spot * disc_q * norm_cdf(d1)
        rho = strike * t * disc_r * norm_cdf(d2)
    else:
        delta = -disc_q * norm_cdf(-d1)
        theta = common_theta + r * strike * disc_r * norm_cdf(-d2) - q * spot * disc_q * norm_cdf(-d1)
        rho = -strike * t * disc_r * norm_cdf(-d2)

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "vega": vega / 100.0,       # per 1 vol point
        "theta": theta / DAYS_PER_YEAR,  # per calendar day
        "rho": rho / 100.0,         # per 1 % move in rates
        "intrinsic": intrinsic_value(spot, strike, option_type),
        "extrinsic": price - intrinsic_value(spot, strike, option_type),
    }


# --------------------------------------------------------------------------- #
# Implied volatility
# --------------------------------------------------------------------------- #
def implied_volatility(market_price: float, spot: float, strike: float, t: float,
                       option_type: str = "CE", r: float = 0.0, q: float = 0.0,
                       tol: float = 1e-6, max_iter: int = 200) -> float:
    """Solve for implied vol with Newton-Raphson, falling back to bisection.

    Returns ``0.0`` when the price is not reachable (below intrinsic, non-positive,
    or above the arbitrage bound) instead of raising -- a scanner must never die
    on one illiquid strike.
    """
    if market_price is None or not math.isfinite(market_price) or market_price <= 0:
        return 0.0
    t = max(t, MIN_T)
    intrinsic = intrinsic_value(spot, strike, option_type)
    upper = spot if _is_call(option_type) else strike
    if market_price >= upper * math.exp(-r * t):
        return 0.0

    # Newton from a vega-weighted starting guess, kept inside a sane band.
    vol = min(max(market_price / max(spot, 1e-9) * math.sqrt(DAYS_PER_YEAR * t) * 2.0, 0.05), 3.0)
    lo, hi = 1e-4, 5.0
    for _ in range(max_iter):
        g = bs_greeks(spot, strike, t, vol, option_type, r, q)
        diff = g["price"] - market_price
        if abs(diff) < tol:
            return vol
        if diff > 0:
            hi = vol
        else:
            lo = vol
        vega = g["vega"] * 100.0
        if vega < 1e-8:
            break
        nxt = vol - diff / vega
        if not math.isfinite(nxt) or nxt <= lo or nxt >= hi:
            nxt = 0.5 * (lo + hi)
        if abs(nxt - vol) < tol * 1e-3:
            return nxt
        vol = nxt
    # Bisection clean-up on the bracket we maintained.
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, option_type, r, q) > market_price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Probability / expected move
# --------------------------------------------------------------------------- #
def prob_above(spot: float, strike: float, t: float, vol: float,
               r: float = 0.0, q: float = 0.0) -> float:
    """Risk-neutral P(S_T > strike) -- i.e. N(d2) for that strike."""
    _, d2 = d1_d2(spot, strike, t, vol, r, q)
    return float(norm_cdf(d2))


def prob_below(spot: float, strike: float, t: float, vol: float,
               r: float = 0.0, q: float = 0.0) -> float:
    return 1.0 - prob_above(spot, strike, t, vol, r, q)


def prob_between(spot: float, low_strike: float, high_strike: float, t: float,
                 vol: float, r: float = 0.0, q: float = 0.0) -> float:
    """P(low < S_T < high) using the vol supplied."""
    return prob_above(spot, low_strike, t, vol, r, q) - prob_above(spot, high_strike, t, vol, r, q)


def prob_touch(spot: float, strike: float, t: float, vol: float,
               r: float = 0.0, q: float = 0.0) -> float:
    """Approximate probability the underlying touches ``strike`` before expiry.

    Uses the standard 2 x N(d2) heuristic (the "probability of touching" rule of
    thumb taught by tastylive / TastyTrade research). Capped at 1.0.
    """
    return float(min(1.0, 2.0 * prob_above(spot, strike, t, vol, r, q)))


def expected_move(spot: float, vol: float, days: float, sd: float = 1.0) -> float:
    """One-standard-deviation expected move in index points (``sd`` scales it)."""
    return spot * max(vol, 0.0) * math.sqrt(max(days, 0.0) / DAYS_PER_YEAR) * sd


def expected_move_from_straddle(straddle_price: float, spot: float) -> float:
    """Options-implied expected move: ATM straddle price / spot."""
    if spot <= 0:
        return 0.0
    return straddle_price / spot


def move_to_strike(spot: float, strike: float, t: float, vol: float,
                   r: float = 0.0, q: float = 0.0) -> float:
    """Number of standard deviations from spot to strike."""
    sd = expected_move(spot, vol, t * DAYS_PER_YEAR, 1.0)
    if sd <= 0:
        return 0.0
    return (strike - spot) / sd


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def _is_call(option_type: str) -> bool:
    ot = str(option_type).strip().upper()
    if ot in ("CE", "CALL", "C"):
        return True
    if ot in ("PE", "PUT", "P"):
        return False
    raise ValueError(f"unknown option type {option_type!r}")


def breakevens_from_payoff(prices: Sequence[float], pnl: Sequence[float]) -> list[float]:
    """Linear-interpolated zero crossings of a payoff curve."""
    out: list[float] = []
    for i in range(1, len(prices)):
        y0, y1 = pnl[i - 1], pnl[i]
        if y0 == 0.0:
            out.append(prices[i - 1])
            continue
        if (y0 < 0 < y1) or (y0 > 0 > y1):
            x0, x1 = prices[i - 1], prices[i]
            out.append(x0 + (x1 - x0) * (-y0) / (y1 - y0))
    return out


def round_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    return round(value / tick) * tick


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


__all__ = [
    "DAYS_PER_YEAR", "norm_cdf", "norm_pdf", "years_to_expiry", "d1_d2",
    "bs_price", "bs_greeks", "intrinsic_value", "extrinsic_value",
    "implied_volatility", "prob_above", "prob_below", "prob_between",
    "prob_touch", "expected_move", "expected_move_from_straddle",
    "move_to_strike", "breakevens_from_payoff", "round_to_tick", "clamp",
]
