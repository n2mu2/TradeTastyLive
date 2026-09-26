"""Trend / momentum indicators used to time the long-option (buyer) signals.

Pure numpy so the same code runs in the backtester, the live scanner and tests.
"""
from __future__ import annotations

import numpy as np


def sma(values, window: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape, np.nan)
    if arr.size >= window and window > 0:
        csum = np.cumsum(np.insert(arr, 0, 0.0))
        out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


def ema(values, window: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or window <= 0:
        return np.full(arr.shape, np.nan)
    alpha = 2.0 / (window + 1.0)
    out = np.empty(arr.size)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    out[:min(window - 1, arr.size)] = np.nan if arr.size >= window else out[:min(window - 1, arr.size)]
    return out


def rsi(closes, window: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    arr = np.asarray(closes, dtype=float)
    out = np.full(arr.shape, np.nan)
    if arr.size <= window:
        return out
    delta = np.diff(arr)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:window].mean()
    avg_loss = loss[:window].mean()
    for i in range(window, delta.size):
        avg_gain = (avg_gain * (window - 1) + gain[i]) / window
        avg_loss = (avg_loss * (window - 1) + loss[i]) / window
        out[i + 1] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    if avg_loss == 0:
        out[window] = 100.0
    else:
        g0, l0 = gain[:window].mean(), loss[:window].mean()
        out[window] = 100.0 if l0 == 0 else 100.0 - 100.0 / (1.0 + g0 / l0)
    return out


def true_range(high, low, closes) -> np.ndarray:
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(closes, dtype=float)
    prev_close = np.concatenate(([c[0]], c[:-1]))
    return np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])


def atr(high, low, closes, window: int = 14) -> np.ndarray:
    tr = true_range(high, low, closes)
    return _wilder(tr, window)


def adx(high, low, closes, window: int = 14) -> dict[str, np.ndarray]:
    """Wilder's ADX with +DI / -DI."""
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = h.size
    up = np.diff(h, prepend=h[0])
    down = -np.diff(l, prepend=l[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr_s = _wilder(true_range(h, l, c), window)
    plus_s = _wilder(plus_dm, window)
    minus_s = _wilder(minus_dm, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(tr_s > 0, 100.0 * plus_s / tr_s, np.nan)
        minus_di = np.where(tr_s > 0, 100.0 * minus_s / tr_s, np.nan)
        dx = 100.0 * np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) > 0, plus_di + minus_di, np.nan)
    adx_arr = _wilder(np.nan_to_num(dx, nan=0.0), window)
    adx_arr[~np.isfinite(dx)] = np.nan
    return {"adx": adx_arr, "plus_di": plus_di, "minus_di": minus_di, "atr": tr_s}


def _wilder(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan)
    if values.size <= window:
        return out
    seed = values[1:window + 1].mean()
    out[window] = seed
    for i in range(window + 1, values.size):
        out[i] = (out[i - 1] * (window - 1) + values[i]) / window
    return out


def momentum(closes, window: int = 20) -> np.ndarray:
    """Percentage return over ``window`` bars."""
    arr = np.asarray(closes, dtype=float)
    out = np.full(arr.shape, np.nan)
    if arr.size > window:
        out[window:] = (arr[window:] / arr[:-window] - 1.0) * 100.0
    return out


def last_finite(values: np.ndarray) -> float:
    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    return float(finite[-1]) if finite.size else float("nan")


def oi_change_pct(current, previous) -> float:
    if not previous:
        return 0.0
    return (current - previous) / previous * 100.0
