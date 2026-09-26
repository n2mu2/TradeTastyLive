"""Volatility analytics: historical vol, IV rank / percentile, term structure."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from .quant import DAYS_PER_YEAR


def historical_volatility(closes: Sequence[float], window: int = 20,
                          annualise: int = 252) -> np.ndarray:
    """Rolling annualised realised volatility from close prices.

    Returns an array aligned with ``closes`` whose first ``window`` entries are NaN.
    """
    arr = np.asarray(closes, dtype=float)
    if arr.size < 2:
        return np.full(arr.shape, np.nan)
    log_ret = np.diff(np.log(arr))
    out = np.full(arr.shape, np.nan)
    if log_ret.size >= window:
        sd = _rolling_std(log_ret, window)
        out[window:] = sd * math.sqrt(annualise)
    return out


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    """Sample standard deviation over a trailing window (ddof=1)."""
    n = values.size
    out = np.empty(n - window + 1)
    for i in range(out.size):
        chunk = values[i:i + window]
        out[i] = chunk.std(ddof=1)
    return out


def realised_vol(closes: Sequence[float], window: int = 20) -> float:
    hv = historical_volatility(closes, window)
    finite = hv[np.isfinite(hv)]
    return float(finite[-1]) if finite.size else 0.0


@dataclass(frozen=True)
class IVContext:
    """A snapshot of where implied volatility sits in its own history."""

    iv: float
    iv_low: float
    iv_high: float
    iv_mean: float
    iv_rank: float        # 0-100
    iv_percentile: float  # 0-100
    samples: int
    lookback_days: int

    def to_dict(self) -> dict:
        return {
            "iv": round(self.iv, 4),
            "iv_low": round(self.iv_low, 4),
            "iv_high": round(self.iv_high, 4),
            "iv_mean": round(self.iv_mean, 4),
            "iv_rank": round(self.iv_rank, 2),
            "iv_percentile": round(self.iv_percentile, 2),
            "samples": self.samples,
            "lookback_days": self.lookback_days,
        }


def iv_context(iv_now: float, iv_history: Sequence[float], lookback_days: int = 365) -> IVContext:
    """Compute IV rank and IV percentile over a trailing window.

    IV Rank      = (iv - min) / (max - min) x 100   -> where we sit in the range
    IV Percentile = % of days with IV below iv       -> how often we were cheaper
    Both matter: rank reacts to single spikes, percentile does not.
    """
    hist = np.asarray(list(iv_history)[-max(lookback_days, 1):], dtype=float)
    hist = hist[np.isfinite(hist)]
    if hist.size == 0:
        return IVContext(iv_now, iv_now, iv_now, iv_now, 0.0, 0.0, 0, lookback_days)

    lo, hi = float(hist.min()), float(hist.max())
    mean = float(hist.mean())
    rank = 0.0 if hi <= lo else (iv_now - lo) / (hi - lo) * 100.0
    percentile = float((hist < iv_now).sum()) / hist.size * 100.0
    return IVContext(
        iv=float(iv_now),
        iv_low=lo,
        iv_high=hi,
        iv_mean=mean,
        iv_rank=float(np.clip(rank, 0.0, 100.0)),
        iv_percentile=float(np.clip(percentile, 0.0, 100.0)),
        samples=int(hist.size),
        lookback_days=lookback_days,
    )


def iv_of_series(series: Sequence[float], drop_outliers: bool = True) -> float:
    """Robust "the" IV for a chain: median of usable observations."""
    arr = np.asarray([v for v in series if v and math.isfinite(v) and v > 0], dtype=float)
    if arr.size == 0:
        return 0.0
    if drop_outliers and arr.size >= 5:
        lo, hi = np.percentile(arr, [10, 90])
        trimmed = arr[(arr >= lo) & (arr <= hi)]
        if trimmed.size:
            arr = trimmed
    return float(np.median(arr))


def term_structure(points: Iterable[tuple[int, float]]) -> list[dict]:
    """Sort (dte, iv) pairs by DTE and flag contango/backwardation."""
    rows = sorted(({"dte": int(d), "iv": float(v)} for d, v in points if v and v > 0),
                  key=lambda r: r["dte"])
    for i, row in enumerate(rows):
        prev = rows[i - 1]["iv"] if i else None
        row["slope"] = None if prev in (None, 0) else (row["iv"] / prev - 1.0) * 100.0
    if len(rows) >= 2:
        rows[-1]["shape"] = "backwardation" if rows[-1]["iv"] < rows[0]["iv"] else "contango"
    return rows


def variance_risk_premium(implied: float, realised: float) -> float:
    """IV - HV in vol points; positive means options are rich vs realised moves."""
    return (implied - realised) * 100.0
