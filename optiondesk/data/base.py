"""Provider interface + the small pieces every provider needs."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Protocol, Sequence

from ..model import Instrument, OptionChain, OptionQuote


class DataError(RuntimeError):
    """Raised when a provider cannot deliver usable data."""


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class DataProvider(Protocol):
    """What the engine needs from a market data source."""

    name: str

    def spot(self, underlying: str) -> float: ...

    def expiries(self, underlying: str, limit: int = 4) -> list[date]: ...

    def option_chain(self, underlying: str, expiry: date,
                     around_atm: int = 12) -> OptionChain: ...

    def daily_closes(self, underlying: str, days: int = 400) -> list[float]: ...

    def candles(self, underlying: str, interval: str = "ONE_DAY",
                days: int = 60) -> list[Candle]: ...

    def universe(self) -> list[str]: ...

    def instruments(self, underlying: str, expiry: date) -> list[Instrument]: ...

    def healthy(self) -> tuple[bool, str]: ...


# --------------------------------------------------------------------------- #
# Expiry helpers (NSE: Nifty weekly = Tuesday, monthly = last Tuesday)
# --------------------------------------------------------------------------- #
def next_weekday(day: date, weekday: int) -> date:
    """Next date on/after ``day`` falling on ``weekday`` (Mon=0 ... Sun=6)."""
    delta = (weekday - day.weekday()) % 7
    return day + timedelta(days=delta)


def last_weekday_of_month(year: int, month: int, weekday: int = 1) -> date:
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    delta = (end.weekday() - weekday) % 7
    return end - timedelta(days=delta)


def dte_from(today: date, expiry: date) -> int:
    return (expiry - today).days


def enrich_quotes(chain: OptionChain, r: float = 0.065, q: float = 0.0,
                  default_iv: Optional[float] = None) -> OptionChain:
    """Fill in any missing greek/IV on quotes using Black-Scholes on the chain's spot.

    Live feeds (Angel optionGreek, NSE option chain) do not always give a usable
    IV for every strike; rather than dropping those strikes we back one out from
    the mid price. That keeps the whole chain usable for delta-based selection.
    """
    from ..quant import bs_greeks, implied_volatility, years_to_expiry

    t = years_to_expiry(max(chain.dte, 0))
    for row in chain.rows:
        for quote in (row.ce, row.pe):
            if quote is None or not quote.tradable:
                continue
            if quote.iv <= 0 and default_iv and default_iv > 0:
                quote.iv = implied_volatility(quote.mid, chain.spot, quote.strike, t,
                                              quote.option_type, r, q) or default_iv
            if quote.iv <= 0:
                quote.iv = default_iv or 0.15
            g = bs_greeks(chain.spot, quote.strike, t, quote.iv, quote.option_type, r, q)
            quote.delta = g["delta"]
            quote.gamma = g["gamma"]
            quote.theta = g["theta"]
            quote.vega = g["vega"]
    return chain
