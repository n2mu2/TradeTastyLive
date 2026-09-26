"""FastAPI dashboard + JSON API for OptionDesk.

Run with:  python run.py serve            (or)  uvicorn optiondesk.app:app
Everything the browser needs is served from this same process with relative
URLs, so it works behind a reverse proxy / preview host without CORS gymnastics.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .alerts import AlertDispatcher, format_signal, send_telegram
from .config import Config
from .data.replay import ReplayProvider
from .indicators import last_finite, rsi, sma
from .model import Leg, ScanResult
from .quant import bs_greeks, expected_move, implied_volatility, years_to_expiry
from .signals.engine import Engine, build_provider
from .signals.rules import trend_of
from .store import SignalStore
from .strategies import analyze_payoff, build_plan, margin_estimate
from .volatility import iv_context, iv_of_series, term_structure

log = logging.getLogger("optiondesk.app")
STATIC_DIR = Path(__file__).resolve().parent / "static"
TZ = ZoneInfo("Asia/Kolkata")


def market_is_open(now: Optional[datetime] = None, open_str: str = "09:15",
                   close_str: str = "15:30", tz: ZoneInfo = TZ) -> bool:
    now = now or datetime.now(tz)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    o = dtime(*[int(x) for x in open_str.split(":")])
    c = dtime(*[int(x) for x in close_str.split(":")])
    return o <= now.time() <= c


def create_app(config_path: Optional[str] = None, autostart: bool = True) -> FastAPI:
    cfg = Config.load(config_path)
    provider = build_provider(cfg)
    engine = Engine(provider, cfg)
    store = SignalStore()
    dispatcher = AlertDispatcher(cfg.alerts, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.task = asyncio.create_task(_scan_loop(app)) if autostart else None
        try:
            yield
        finally:
            task = getattr(app.state, "task", None)
            if task:
                task.cancel()

    app = FastAPI(title="OptionDesk", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.provider = provider
    app.state.engine = engine
    app.state.store = store
    app.state.dispatcher = dispatcher
    app.state.last_scan = None

    # ------------------------------------------------------------------ #
    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health():
        ok, detail = provider.healthy()
        now_ist = datetime.now(TZ)
        return {
            "provider": provider.name,
            "ok": ok,
            "detail": detail,
            "ist_now": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": market_is_open(now_ist, cfg.scheduler.market_open,
                                          cfg.scheduler.market_close),
            "clock": provider.clock().strftime("%Y-%m-%d %H:%M") if hasattr(provider, "clock") else None,
            "telegram_configured": bool(cfg.alerts.telegram_bot_token and cfg.alerts.telegram_chat_id),
            "store": store.stats(),
            "last_scan": store.last_scan(),
        }

    @app.get("/api/config")
    def get_config():
        return cfg.public_dict()

    @app.post("/api/config")
    def put_config(patch: dict = Body(...)):
        """Update non-secret knobs live (thresholds, sizing, universe)."""
        for section, values in (patch or {}).items():
            obj = getattr(cfg, section, None)
            if obj is None or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if not hasattr(obj, key) or key.startswith("angel_"):
                    continue
                current = getattr(obj, key)
                try:
                    setattr(obj, key, type(current)(value) if isinstance(current, (int, float)) else value)
                except (TypeError, ValueError):
                    continue
        return cfg.public_dict()

    @app.get("/api/chain")
    def chain(underlying: str = Query("NIFTY"), expiry_index: int = Query(0),
              around_atm: int = Query(15)):
        try:
            ch = engine.index_chain(underlying.upper(), expiry_index)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        iv_ctx = engine.iv_context_for(underlying.upper(), ch)
        trend = trend_of(ch.closes, cfg.stock_options.trend_fast, cfg.stock_options.trend_slow,
                         cfg.stock_options.rsi_window)
        atm_iv = iv_of_series([q.iv for r in ch.rows for q in (r.ce, r.pe) if q and q.iv > 0])
        return {
            "underlying": ch.underlying,
            "spot": ch.spot,
            "expiry": ch.expiry.isoformat(),
            "dte": ch.dte,
            "lot_size": ch.lot_size,
            "source": ch.source,
            "fetched_at": ch.fetched_at.isoformat(timespec="seconds"),
            "iv_context": iv_ctx.to_dict(),
            "atm_iv": round(atm_iv, 4),
            "expected_move_points": round(expected_move(ch.spot, atm_iv, ch.dte), 1),
            "trend": {"direction": trend.direction, "fast": round(trend.fast, 2),
                      "slow": round(trend.slow, 2), "rsi": round(trend.rsi, 1),
                      "adx": round(trend.adx, 1), "momentum_pct": round(trend.momentum_pct, 2),
                      "strength": trend.strength, "reasons": trend.reasons},
            "expiries": [d.isoformat() for d in provider.expiries(underlying.upper(), limit=6)],
            "rows": [_row_dict(r, ch.spot) for r in ch.rows],
        }

    @app.get("/api/iv")
    def iv(underlying: str = Query("NIFTY"), expiry_index: int = Query(0)):
        ch = engine.index_chain(underlying.upper(), expiry_index)
        iv_ctx = engine.iv_context_for(underlying.upper(), ch)
        structure = term_structure(_term_points(provider, underlying.upper()))
        return {"iv_context": iv_ctx.to_dict(), "history": ch.iv_history[-180:],
                "term_structure": structure,
                "closes": ch.closes[-180:]}

    @app.get("/api/universe")
    def universe():
        """Per-stock verdict table: IV rank, trend, and what the rules would do."""
        out = []
        names = (cfg.market.stock_universe or provider.universe())[:cfg.stock_options.max_universe]
        for name in names:
            try:
                exps = provider.expiries(name, limit=max(cfg.stock_options.expiry_index + 2, 3))
                if len(exps) <= cfg.stock_options.expiry_index:
                    continue
                ch = provider.option_chain(name, exps[cfg.stock_options.expiry_index], around_atm=8)
                ctx = engine.iv_context_for(name, ch)
                trend = trend_of(ch.closes, cfg.stock_options.trend_fast, cfg.stock_options.trend_slow,
                                 cfg.stock_options.rsi_window)
                atm = iv_of_series([q.iv for r in ch.rows for q in (r.ce, r.pe) if q and q.iv > 0])
                out.append({
                    "underlying": name, "spot": ch.spot, "expiry": ch.expiry.isoformat(),
                    "dte": ch.dte, "lot_size": ch.lot_size, "atm_iv": round(atm * 100, 2),
                    "iv_rank": round(ctx.iv_rank, 1), "iv_percentile": round(ctx.iv_percentile, 1),
                    "trend": trend.direction, "rsi": round(trend.rsi, 1), "adx": round(trend.adx, 1),
                    "momentum_pct": round(trend.momentum_pct, 2), "strength": trend.strength,
                    "expected_move_pct": round(expected_move(ch.spot, atm, ch.dte) / ch.spot * 100, 2),
                    "verdict": _verdict(ctx, trend, cfg.stock_options),
                })
            except Exception as exc:
                out.append({"underlying": name, "error": str(exc)})
        out.sort(key=lambda r: r.get("score", r.get("iv_rank", 0)) or 0, reverse=True)
        return out

    @app.get("/api/signals")
    def signals(limit: int = 25, kind: Optional[str] = None, underlying: Optional[str] = None):
        rows = store.recent(limit=limit, kind=kind, underlying=underlying)
        if not rows and app.state.last_scan:
            rows = [s.to_dict() for s in app.state.last_scan.signals][:limit]
        return rows

    @app.post("/api/scan")
    def scan(now: bool = True, alert: bool = True):
        result = _run_scan(app, alert=alert)
        return result.to_dict()

    @app.get("/api/last-scan")
    def last_scan():
        res: Optional[ScanResult] = app.state.last_scan
        return res.to_dict() if res else {"signals": [], "scanned": 0}

    @app.post("/api/payoff")
    def payoff(payload: dict = Body(...)):
        """Interactive payoff builder used by the dashboard's strategy lab."""
        spot = float(payload["spot"])
        dte = int(payload.get("dte", 10))
        lot_size = int(payload.get("lot_size", 1))
        lots = int(payload.get("lots", 1))
        iv = float(payload.get("iv", 0.15))
        legs = []
        for l in payload.get("legs", []):
            strike = float(l["strike"])
            otype = str(l["option_type"]).upper()
            side = str(l["side"]).upper()
            price = float(l.get("premium", 0)) or bs_greeks(
                spot, strike, years_to_expiry(dte), iv, otype,
                cfg.market.risk_free_rate, cfg.market.index_dividend_yield)["price"]
            legs.append(Leg(side=side, option_type=otype, strike=strike, premium=price,
                            lots=lots, lot_size=lot_size, iv=iv,
                            **{k: v for k, v in bs_greeks(
                                spot, strike, years_to_expiry(dte), iv, otype,
                                cfg.market.risk_free_rate,
                                cfg.market.index_dividend_yield).items()
                               if k in ("delta", "gamma", "theta", "vega")}))
        if not legs:
            raise HTTPException(status_code=400, detail="no legs supplied")
        net_credit_total = sum(l.cash for l in legs)
        analysis = analyze_payoff(legs, net_credit_total, spot)
        mp = analysis["max_profit"]
        ml = analysis["max_loss"]
        return {
            "net_credit_per_unit": round(net_credit_total / (lots * lot_size), 3),
            "max_profit": None if math.isinf(mp) else round(mp, 2),
            "max_loss": None if math.isinf(ml) else round(ml, 2),
            "breakevens": [round(b, 2) for b in analysis["breakevens"]],
            "prices": analysis["grid"], "pnl": analysis["pnl"],
            "greeks": {k: round(sum(l.signed_qty * getattr(l, k) for l in legs), 4)
                       for k in ("delta", "gamma", "theta", "vega")},
            "margin_estimate": round(margin_estimate(legs, spot, iv, dte, lot_size, lots), 2),
        }

    @app.post("/api/alerts/test")
    def test_alert():
        if not dispatcher.configured:
            return {"sent": False, "detail": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"}
        text = ("✅ OptionDesk test alert\nIf you can read this, live signals will land here.\n"
                f"Provider: {provider.name} | {datetime.now(TZ):%d %b %H:%M} IST")
        try:
            send_telegram(cfg.alerts.telegram_bot_token, cfg.alerts.telegram_chat_id, text)
            return {"sent": True}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/clock/advance")
    def advance_clock(minutes: int = 5):
        """Replay-provider only: move the simulated market clock."""
        if not isinstance(provider, ReplayProvider):
            raise HTTPException(status_code=400, detail="clock control is only available on the replay provider")
        provider.advance(minutes)
        app.state.last_scan = None
        return {"clock": provider.clock().strftime("%Y-%m-%d %H:%M")}

    @app.get("/api/rules")
    def rules():
        """The exact rule set the engine enforces -- transparency beats magic."""
        n, s = cfg.nifty_spreads, cfg.stock_options
        return {
            "nifty_hedged_spreads": [
                f"Only trade when IV rank >= {n.min_iv_rank} (sell rich vol)",
                f"Short leg chosen at ~{n.short_delta} delta, wing at ~{n.wing_delta} delta "
                f"or {n.wing_width_points:.0f} points, whichever is wider",
                f"Expiry between {n.dte_min} and {n.dte_max} DTE",
                f"Probability of profit must be >= {n.min_pop:.0%}",
                f"Credit must be >= {n.min_credit_pct_of_width:.0%} of the spread width",
                f"Bid-ask <= {n.max_spread_pct}% and OI >= {n.min_oi} on the short leg",
                f"Exit at {n.take_profit_pct:.0%} of max profit, stop at "
                f"{n.stop_loss_multiple}x credit, roll at {n.roll_dte} DTE",
                f"Debit spreads only when IV rank <= {n.debit_iv_rank_max}",
            ],
            "stock_option_buying": [
                f"Only buy premium when IV rank <= {s.max_iv_rank} (cheap vol)",
                f"Expiry #{s.expiry_index} ({s.dte_min}-{s.dte_max} DTE) -- buy time, not lottery tickets",
                f"Trend required: price vs SMA{s.trend_fast}/SMA{s.trend_slow}, "
                f"RSI {s.rsi_long_min:.0f}-{s.rsi_long_max:.0f} long / {s.rsi_short_min:.0f}-{s.rsi_short_max:.0f} short",
                f"Needs ADX >= {s.adx_min} or momentum >= {s.momentum_min_pct}% over 20 sessions",
                f"Strike at {s.delta_min}-{s.delta_max} delta (ITM, low theta bleed)",
                f"Premium <= {s.max_premium_pct_of_spot}% of spot; spread <= {s.max_spread_pct}%; OI >= {s.min_oi}",
                f"Switch to a debit spread when IV rank >= {s.prefer_debit_spread_above_iv_rank}",
                f"Book {s.take_profit_pct:.0%} profit, stop at {s.stop_loss_pct:.0%}, exit with 7-10 DTE left",
            ],
            "risk": [
                f"Capital {cfg.account.capital:,.0f}, max risk {cfg.account.max_risk_per_trade_pct}% per trade "
                f"= {cfg.account.capital * cfg.account.max_risk_per_trade_pct / 100:,.0f}",
                f"Max {cfg.account.max_lots_per_trade} lots per trade, {cfg.account.max_open_trades} open trades",
                "Signals only -- this tool never places an order",
            ],
            "alerts": [
                f"Minimum score to alert: {cfg.alerts.min_score}",
                f"De-duplicate repeat signals for {cfg.alerts.dedupe_minutes} minutes",
                f"Max {cfg.alerts.max_alerts_per_scan} alerts per scan",
            ],
        }

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _run_scan(app: FastAPI, alert: bool = True) -> ScanResult:
    engine: Engine = app.state.engine
    store: SignalStore = app.state.store
    result = engine.scan()
    store.save_scan(result)
    app.state.last_scan = result
    if alert:
        app.state.dispatcher.dispatch(result.signals)
    log.info("scan done: %d signals from %d instruments in %.1fs",
             len(result.signals), result.scanned, result.duration_s)
    return result


async def _scan_loop(app: FastAPI) -> None:
    cfg: Config = app.state.cfg
    interval = max(int(cfg.scheduler.interval_seconds), 15)
    while True:
        try:
            now = datetime.now(TZ)
            in_hours = market_is_open(now, cfg.scheduler.market_open, cfg.scheduler.market_close)
            if in_hours or cfg.scheduler.scan_outside_hours:
                await asyncio.to_thread(_run_scan, app, True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the loop die
            log.exception("scan loop error: %s", exc)
        await asyncio.sleep(interval)


def _verdict(ctx, trend, cfg) -> str:
    """Classify a stock setup for the dashboard: buy | spread | avoid."""
    if trend.direction == "neutral":
        return "avoid"
    if ctx.iv_rank > cfg.max_iv_rank:
        return "avoid"
    if ctx.iv_rank >= cfg.prefer_debit_spread_above_iv_rank:
        return "spread"
    if (trend.direction == "bullish" and cfg.rsi_long_min <= trend.rsi <= cfg.rsi_long_max) or \
       (trend.direction == "bearish" and cfg.rsi_short_min <= trend.rsi <= cfg.rsi_short_max):
        return "buy"
    return "avoid"


def _row_dict(row, spot: float) -> dict:
    def q(x):
        if x is None:
            return None
        return {"strike": x.strike, "ltp": round(x.mid, 2), "bid": x.bid, "ask": x.ask,
                "iv": round(x.iv * 100, 2), "delta": round(x.delta, 3), "theta": round(x.theta, 2),
                "gamma": round(x.gamma, 4), "vega": round(x.vega, 2), "oi": x.oi,
                "oi_change": x.oi_change, "volume": x.volume, "spread_pct": round(x.spread_pct, 2)}
    return {"strike": row.strike, "moneyness": "ATM" if abs(row.strike - spot) < 1e-9
            else ("ITM" if ((row.ce and row.strike < spot) or (row.pe and row.strike > spot)) else "OTM"),
            "ce": q(row.ce), "pe": q(row.pe)}


def _term_points(data_provider, underlying: str) -> list[tuple[int, float]]:
    """(dte, atm iv) for each listed expiry -- a quick term structure read."""
    out = []
    for d in data_provider.expiries(underlying, limit=5):
        try:
            ch = data_provider.option_chain(underlying, d, around_atm=4)
            iv = iv_of_series([q.iv for r in ch.rows for q in (r.ce, r.pe) if q and q.iv > 0])
            if iv:
                out.append((ch.dte, iv))
        except Exception:
            continue
    return out


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
