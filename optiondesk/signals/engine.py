"""Scan orchestration: provider -> chains -> rules -> ranked signals."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Optional

from ..config import Config
from ..data.base import DataProvider
from ..model import OptionChain, ScanResult, Signal
from ..volatility import iv_context, iv_of_series
from .nifty_spreads import build_index_signals
from .rules import trend_of
from .stock_options import scan_stocks

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, provider: DataProvider, cfg: Config):
        self.provider = provider
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def iv_context_for(self, underlying: str, chain: OptionChain):
        series = [q.iv for row in chain.rows for q in (row.ce, row.pe) if q and q.iv > 0]
        now = iv_of_series(series) or 0.15
        history = chain.iv_history
        if not history and chain.closes:
            from ..volatility import historical_volatility
            import numpy as np

            hv = historical_volatility(chain.closes, 20)
            history = [float(v) for v in hv if v == v]
        return iv_context(now, history, lookback_days=365)

    def index_chain(self, underlying: str = "NIFTY",
                    expiry_index: Optional[int] = None) -> OptionChain:
        idx = self.cfg.nifty_spreads.expiry_index if expiry_index is None else expiry_index
        expiries = self.provider.expiries(underlying, limit=max(idx + 2, 3))
        if not expiries:
            raise RuntimeError(f"no expiries available for {underlying}")
        expiry = expiries[min(idx, len(expiries) - 1)]
        return self.provider.option_chain(underlying, expiry, around_atm=15)

    # ------------------------------------------------------------------ #
    def scan(self, ts: Optional[datetime] = None,
             index_underlyings: Optional[list[str]] = None,
             stock_universe: Optional[list[str]] = None) -> ScanResult:
        start = time.time()
        ts = ts or datetime.now()
        result = ScanResult(ts=ts, provider=self.provider.name)
        index_underlyings = index_underlyings or [self.cfg.market.index_underlying]

        for underlying in index_underlyings:
            try:
                chain = self.index_chain(underlying)
                iv_ctx = self.iv_context_for(underlying, chain)
                trend = trend_of(chain.closes, self.cfg.stock_options.trend_fast,
                                 self.cfg.stock_options.trend_slow,
                                 self.cfg.stock_options.rsi_window)
                signals, rejected = build_index_signals(
                    chain, self.cfg.nifty_spreads, self.cfg.account, iv_ctx, trend, ts,
                    underlying=underlying, r=self.cfg.market.risk_free_rate,
                    q=self.cfg.market.index_dividend_yield)
                result.signals.extend(signals)
                result.rejected.extend(rejected)
                result.scanned += 1
            except Exception as exc:
                msg = f"index scan {underlying} failed: {exc}"
                log.exception(msg)
                result.errors.append(msg)

        if self.cfg.stock_options.enabled:
            try:
                universe = stock_universe or self.cfg.market.stock_universe or self.provider.universe()
                sigs, rejected, scanned = scan_stocks(
                    self.provider, self.cfg.stock_options, self.cfg.account, ts,
                    universe=universe,
                    iv_context_fn=lambda u, c: self.iv_context_for(u, c),
                    r=self.cfg.market.risk_free_rate,
                    q=self.cfg.market.stock_dividend_yield)
                result.signals.extend(sigs)
                result.rejected.extend(rejected)
                result.scanned += scanned
            except Exception as exc:
                msg = f"stock scan failed: {exc}"
                log.exception(msg)
                result.errors.append(msg)

        result.signals.sort(key=lambda s: s.score, reverse=True)
        result.duration_s = time.time() - start
        return result


def build_provider(cfg: Config) -> DataProvider:
    """Instantiate the configured market data provider."""
    if cfg.market.provider == "angel":
        from ..data.angel import AngelClient, AngelProvider, ScripMaster
        from pathlib import Path

        missing = [k for k in ("angel_api_key", "angel_client_code", "angel_pin",
                               "angel_totp_secret") if not getattr(cfg.market, k)]
        if missing:
            raise RuntimeError(f"provider=angel but missing config: {', '.join(missing)}")
        client = AngelClient(cfg.market.angel_api_key, cfg.market.angel_client_code,
                             cfg.market.angel_pin, cfg.market.angel_totp_secret)
        scrip = ScripMaster(path=Path(cfg.market.scrip_cache_path)) if cfg.market.scrip_cache_path \
            else ScripMaster()
        scrip.load()
        return AngelProvider(client, scrip, r=cfg.market.risk_free_rate,
                             q=cfg.market.index_dividend_yield)

    from ..data.replay import ReplayProvider

    return ReplayProvider()
