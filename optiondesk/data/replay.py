"""Deterministic offline market simulator.

This is the provider that runs when you have no broker credentials, and it is
also what the test-suite and the dashboard demo use. It is deliberately not
"random noise": the generated chains carry a realistic volatility smile, term
structure, open-interest distribution and bid/ask spread, so delta-based strike
selection and IV-rank filters behave the way they will on live data.

Every number is a function of ``(underlying, seed, simulated clock)``, so a given
scan always produces the same signals -- which is what makes the engine testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

import numpy as np

from ..model import ChainRow, Instrument, OptionChain, OptionQuote
from ..quant import bs_price, years_to_expiry
from .base import Candle, enrich_quotes, last_weekday_of_month, next_weekday

# underlying -> (spot, lot_size, strike_step, atm_iv, dividend_yield, personality)
_UNIVERSE: dict[str, tuple[float, int, float, float, float, str]] = {
    "NIFTY":       (25_480.0, 65, 50.0,  0.128, 0.012, "index"),
    "BANKNIFTY":   (57_850.0, 30, 100.0, 0.141, 0.010, "index"),
    "FINNIFTY":    (24_910.0, 65, 50.0,  0.135, 0.011, "index"),
    "RELIANCE":    (1_462.0,  250, 20.0, 0.176, 0.004, "uptrend_calm"),
    "HDFCBANK":    (1_918.0,  550, 20.0, 0.152, 0.011, "flat"),
    "TCS":         (3_124.0,  175, 50.0, 0.168, 0.021, "downtrend_calm"),
    "INFY":        (1_596.0,  400, 20.0, 0.181, 0.019, "uptrend_calm"),
    "ICICIBANK":   (1_384.0,  700, 20.0, 0.159, 0.008, "uptrend_vol"),
    "SBIN":        (842.0,   1500, 10.0, 0.224, 0.012, "uptrend_vol"),
    "TATAMOTORS":  (704.0,   1100, 10.0, 0.312, 0.003, "downtrend_vol"),
    "BAJFINANCE":  (8_940.0,  125, 100.0, 0.267, 0.002, "highvol"),
    "ADANIENT":    (2_318.0,  300, 50.0, 0.391, 0.001, "highvol"),
    "WIPRO":       (318.0,   3000, 5.0,  0.243, 0.005, "flat"),
    "AXISBANK":    (1_148.0,  625, 20.0, 0.187, 0.004, "downtrend_calm"),
    "LT":          (3_542.0,  150, 50.0, 0.194, 0.009, "flat"),
    "MARUTI":      (12_480.0,  50, 200.0, 0.208, 0.006, "highvol"),
    "HCLTECH":     (1_684.0,  350, 20.0, 0.173, 0.024, "uptrend_calm"),
    "SUNPHARMA":   (1_742.0,  350, 20.0, 0.161, 0.007, "flat"),
    "TATASTEEL":   (162.4,   5500, 2.5,  0.286, 0.018, "downtrend_vol"),
    "ONGC":        (238.6,   3850, 5.0,  0.254, 0.031, "flat"),
}

TRADING_START = dtime(9, 15)
TRADING_END = dtime(15, 30)


@dataclass
class _Profile:
    spot: float
    lot_size: int
    strike_step: float
    atm_iv: float
    div_yield: float
    personality: str


class ReplayProvider:
    """Simulated NSE F&O market."""

    name = "replay"

    def __init__(self, seed: int = 20260926, today: Optional[date] = None,
                 r: float = 0.065, minute_step: int = 5):
        self.seed = seed
        self.today = today or date.today()
        self.r = r
        self.minute_step = minute_step
        self._minutes = 0            # simulated minutes since the open
        self._paths: dict[str, np.ndarray] = {}
        self._iv_paths: dict[str, np.ndarray] = {}
        self._cache: dict[tuple, OptionChain] = {}

    # ------------------------------------------------------------------ #
    # market clock
    # ------------------------------------------------------------------ #
    def advance(self, minutes: Optional[int] = None) -> None:
        """Move the simulated clock forward (drives spot / IV drift)."""
        self._minutes += minutes if minutes is not None else self.minute_step
        self._cache.clear()

    def reset(self) -> None:
        self._minutes = 0
        self._cache.clear()

    def clock(self) -> datetime:
        base = datetime.combine(self.today, TRADING_START)
        return base + timedelta(minutes=self._minutes)

    # ------------------------------------------------------------------ #
    # synthetic history
    # ------------------------------------------------------------------ #
    def _rng(self, tag: str) -> np.random.Generator:
        h = abs(hash((self.seed, tag))) % (2 ** 32)
        return np.random.default_rng(h)

    def _profile(self, underlying: str) -> _Profile:
        key = underlying.upper()
        spot, lot, step, iv, dy, personality = _UNIVERSE.get(
            key, (1000.0, 500, 10.0, 0.2, 0.01, "flat"))
        return _Profile(spot, lot, step, iv, dy, personality)

    def _daily_path(self, underlying: str, n: int = 420) -> np.ndarray:
        key = underlying.upper()
        if key in self._paths:
            return self._paths[key]
        prof = self._profile(key)
        rng = self._rng(f"daily:{key}")
        drift = {"uptrend_calm": 0.00055, "uptrend_vol": 0.00075,
                 "downtrend_calm": -0.00045, "downtrend_vol": -0.00085,
                 "highvol": 0.00015, "flat": 0.00008, "index": 0.00035}[prof.personality]
        vol = prof.atm_iv * (1.25 if "vol" in prof.personality or prof.personality == "highvol" else 1.0)
        shocks = rng.normal(drift, vol / math.sqrt(252), n)
        cum = np.cumsum(shocks)
        path = prof.spot * np.exp(cum - cum[-1])  # smooth anchor to reference spot
        self._paths[key] = path
        return path

    def _iv_path(self, underlying: str, n: int = 300) -> np.ndarray:
        key = underlying.upper()
        if key in self._iv_paths:
            return self._iv_paths[key]
        prof = self._profile(key)
        rng = self._rng(f"iv:{key}")
        mean = prof.atm_iv
        kappa = 0.055
        sigma = mean * 0.28
        out = np.empty(n)
        out[0] = mean
        for i in range(1, n):
            out[i] = out[i - 1] + kappa * (mean - out[i - 1]) + sigma * rng.normal() / math.sqrt(252)
        out = np.clip(out, mean * 0.45, mean * 2.3)
        # personalities: some names sit near the top of their IV range right now
        if prof.personality in ("highvol", "uptrend_vol", "downtrend_vol"):
            out[-40:] = np.linspace(out[-41], mean * 1.75, 40)
        elif prof.personality in ("uptrend_calm", "downtrend_calm"):
            out[-40:] = np.linspace(out[-41], mean * 0.72, 40)
        self._iv_paths[key] = out
        return out

    # ------------------------------------------------------------------ #
    # DataProvider surface
    # ------------------------------------------------------------------ #
    def universe(self) -> list[str]:
        return list(_UNIVERSE)

    def spot(self, underlying: str) -> float:
        path = self._daily_path(underlying)
        base = float(path[-1])
        # small intraday wiggle driven by the simulated clock
        rng = self._rng(f"tick:{underlying.upper()}:{self._minutes // self.minute_step}")
        iv = self._atm_iv(underlying)
        wiggle = rng.normal(0, iv / math.sqrt(252 * 75)) * base
        return round(base + wiggle, 2)

    def _atm_iv(self, underlying: str, dte: Optional[int] = None) -> float:
        prof = self._profile(underlying)
        iv = float(self._iv_path(underlying)[-1])
        if dte is not None:  # mild term structure: short dated = a touch richer
            iv *= 1.0 + 0.06 * math.exp(-max(dte, 0) / 12.0)
        return iv

    def daily_closes(self, underlying: str, days: int = 400) -> list[float]:
        return [float(x) for x in self._daily_path(underlying)[-days:]]

    def candles(self, underlying: str, interval: str = "ONE_DAY",
                days: int = 60) -> list[Candle]:
        closes = self.daily_closes(underlying, days)
        rng = self._rng(f"candles:{underlying.upper()}")
        out: list[Candle] = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            hi = max(o, c) * (1 + abs(rng.normal(0, 0.004)))
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.004)))
            out.append(Candle(ts=datetime.combine(self.today, TRADING_END) - timedelta(days=len(closes) - i),
                              open=o, high=hi, low=lo, close=c,
                              volume=int(rng.integers(500_000, 5_000_000))))
        return out

    def expiries(self, underlying: str, limit: int = 4) -> list[date]:
        key = underlying.upper()
        out: list[date] = []
        if key == "NIFTY":  # weekly expiries exist only on Nifty (NSE)
            nxt = next_weekday(self.today + timedelta(days=1), 1)  # Tuesday
            for _ in range(4):
                out.append(nxt)
                nxt = next_weekday(nxt + timedelta(days=1), 1)
            out.append(last_weekday_of_month(self.today.year, self.today.month, 1))
            m = self.today.month + (1 if self.today.day > 20 else 0)
            y = self.today.year + (1 if m > 12 else 0)
            m = ((m - 1) % 12) + 1
            out.append(last_weekday_of_month(y, m, 1))
        else:
            out.append(last_weekday_of_month(self.today.year, self.today.month, 1))
            for k in range(1, 4):
                m = self.today.month + k
                y = self.today.year + (m - 1) // 12
                m = ((m - 1) % 12) + 1
                out.append(last_weekday_of_month(y, m, 1))
        return sorted(set(d for d in out if d >= self.today))[:limit]

    def instruments(self, underlying: str, expiry: date) -> list[Instrument]:
        chain = self.option_chain(underlying, expiry, around_atm=25)
        out: list[Instrument] = []
        for row in chain.rows:
            for q in (row.ce, row.pe):
                if q is None:
                    continue
                out.append(Instrument(
                    token=f"SIM{underlying.upper()}{expiry.strftime('%d%b%y').upper()}{int(q.strike)}{q.option_type}",
                    symbol=f"{underlying.upper()}{expiry.strftime('%d%b%y').upper()}{int(q.strike)}{q.option_type}",
                    name=underlying.upper(),
                    exchange="NFO",
                    instrument_type="OPTIDX" if underlying.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY") else "OPTSTK",
                    strike=q.strike,
                    expiry=expiry,
                    lot_size=chain.lot_size,
                    tick_size=0.05,
                    option_type=q.option_type,
                ))
        return out

    def option_chain(self, underlying: str, expiry: date, around_atm: int = 12) -> OptionChain:
        key = (underlying.upper(), expiry, around_atm, self._minutes)
        if key in self._cache:
            return self._cache[key]

        prof = self._profile(underlying)
        spot = self.spot(underlying)
        dte = max((expiry - self.today).days, 0)
        t = years_to_expiry(dte)
        atm_iv = self._atm_iv(underlying, dte)
        rng = self._rng(f"chain:{underlying.upper()}:{expiry.isoformat()}:{self._minutes}")

        strikes = self._strike_grid(spot, prof.strike_step, around_atm)
        rows: list[ChainRow] = []
        for strike in strikes:
            ce = self._quote(rng, spot, strike, t, atm_iv, "CE", prof, underlying, expiry, dte)
            pe = self._quote(rng, spot, strike, t, atm_iv, "PE", prof, underlying, expiry, dte)
            rows.append(ChainRow(strike=strike, ce=ce, pe=pe))

        chain = OptionChain(
            underlying=underlying.upper(),
            underlying_symbol=underlying.upper(),
            spot=spot,
            expiry=expiry,
            dte=dte,
            rows=rows,
            lot_size=prof.lot_size,
            source=self.name,
            iv_history=[float(v) for v in self._iv_path(underlying)],
            closes=self.daily_closes(underlying, 400),
            fetched_at=self.clock(),
        )
        self._cache[key] = enrich_quotes(chain, self.r, prof.div_yield)
        return self._cache[key]

    def healthy(self) -> tuple[bool, str]:
        return True, f"simulated market, clock {self.clock():%Y-%m-%d %H:%M}"

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strike_grid(spot: float, step: float, n: int) -> list[float]:
        centre = round(spot / step) * step
        return [round(centre + i * step, 4) for i in range(-n, n + 1)]

    def _smile_iv(self, spot: float, strike: float, atm_iv: float,
                  option_type: str, dte: int) -> float:
        """Skewed smile: puts richer than calls, curvature rising away from ATM."""
        m = (strike / spot) - 1.0
        skew = -0.55 if option_type == "PE" else -0.18   # put skew
        curve = 1.65
        iv = atm_iv * (1.0 + skew * m + curve * m * m)
        # short-dated contracts get a fatter tail
        iv *= 1.0 + 0.35 * math.exp(-max(dte, 0) / 6.0) * abs(m)
        return max(min(iv, 3.0), 0.02)

    def _quote(self, rng: np.random.Generator, spot: float, strike: float, t: float,
               atm_iv: float, option_type: str, prof: _Profile, underlying: str,
               expiry: date, dte: int) -> OptionQuote:
        iv = self._smile_iv(spot, strike, atm_iv, option_type, dte)
        iv *= 1.0 + rng.normal(0, 0.012)
        iv = max(iv, 0.02)
        mid = bs_price(spot, strike, t, iv, option_type, self.r, prof.div_yield)
        mid = max(mid, 0.05)
        # spreads widen for cheap / deep-OTM strikes, exactly like a real book
        dist = abs(math.log(max(strike, 1) / spot))
        spread = max(0.05, mid * (0.008 + 0.09 * dist))
        spread = max(spread, 0.05 * max(1, round(dist * 6)))
        oi = int(abs(rng.normal(1.0, 0.25)) * 60_000 * math.exp(-((strike - spot) / (spot * 0.05)) ** 2))
        if abs(strike % (prof.strike_step * 10)) < 1e-6:
            oi = int(oi * 1.9)  # round strikes hold extra OI
        vol = int(oi * abs(rng.normal(0.35, 0.15)))
        sym = f"{underlying.upper()}{expiry.strftime('%d%b%y').upper()}{int(strike)}{option_type}"
        return OptionQuote(
            strike=strike,
            option_type=option_type,
            ltp=round(mid + rng.normal(0, spread * 0.15), 2),
            bid=round(max(mid - spread / 2, 0.05), 2),
            ask=round(mid + spread / 2, 2),
            volume=max(vol, 0),
            oi=max(oi, 0),
            oi_change=int(rng.normal(0, max(oi * 0.08, 1))),
            iv=iv,
            symbol=sym,
            token=f"SIM{sym}",
        )


def build_default_provider() -> ReplayProvider:
    return ReplayProvider()
