"""Exporter for APTrade mobile application (schema compatible with niftyService.ts).

Generates `docs/data.json` containing:
- NIFTY spot & timestamp
- Next Week (weekly expiry) & Current Month (monthly expiry)
- Conservative V1 (~0.16 Delta) & Balanced V2 (~0.22 Delta)
- Bull Put Credit Spread (bull), Bear Call Credit Spread (bear), Iron Condor (sideways)
- Strike prices, premiums, TradingView/broker symbols, exact metrics and stop-losses
"""
from __future__ import annotations

import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Config, NiftySpreadCfg
from .signals.engine import Engine, build_provider
from .signals.nifty_spreads import _credit_spread, _iron_condor
from .signals.rules import Trend, trend_of
from .volatility import iv_of_series

TZ = ZoneInfo("Asia/Kolkata")


def _format_tv_symbol(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    # NSE format e.g. NIFTY26OCT25200PE or NIFTY2692925200PE
    exp_str = expiry.strftime("%y%b").upper()
    return f"{underlying.upper()}{exp_str}{int(strike)}{option_type.upper()}"


def _format_strategy(plan, strat_key: str, spot: float) -> Optional[dict]:
    if not plan:
        return None
    short_legs = [l for l in plan.legs if l.side == "SELL"]
    long_legs = [l for l in plan.legs if l.side == "BUY"]
    if not short_legs:
        return None

    if strat_key in ("bull", "bear"):
        short_l = short_legs[0]
        long_l = long_legs[0] if long_legs else short_l
        width = abs(short_l.strike - long_l.strike)
        sell_sym = short_l.symbol or _format_tv_symbol(plan.underlying, plan.expiry, short_l.strike, short_l.option_type)
        buy_sym = long_l.symbol or _format_tv_symbol(plan.underlying, plan.expiry, long_l.strike, long_l.option_type)
        sl_spot = plan.breakevens[0] if plan.breakevens else (short_l.strike * 0.99 if strat_key == "bull" else short_l.strike * 1.01)

        return {
            "spot": round(spot, 2),
            "sell_strike": int(short_l.strike),
            "sell_type": short_l.option_type,
            "buy_strike": int(long_l.strike),
            "buy_type": long_l.option_type,
            "sell_price": round(short_l.premium, 2),
            "buy_price": round(long_l.premium, 2),
            "sell_tv_symbol": sell_sym,
            "buy_tv_symbol": buy_sym,
            "stop_loss_spot": round(sl_spot, 1),
            "metrics": {
                "max_profit": round(plan.max_profit, 0),
                "max_loss": round(plan.max_loss, 0),
                "width": int(width),
                "credit": round(plan.net_credit, 2),
                "risk_reward": round(plan.risk_reward, 2),
                "pop": round(plan.prob_short_otm * 100, 1),
            }
        }
    else:  # sideways (Iron Condor)
        short_puts = [l for l in short_legs if l.option_type == "PE"]
        short_calls = [l for l in short_legs if l.option_type == "CE"]
        long_puts = [l for l in long_legs if l.option_type == "PE"]
        long_calls = [l for l in long_legs if l.option_type == "CE"]
        if not (short_puts and short_calls and long_puts and long_calls):
            return None

        sp, sc = short_puts[0], short_calls[0]
        lp, lc = long_puts[0], long_calls[0]
        width = min(abs(sp.strike - lp.strike), abs(sc.strike - lc.strike))
        sl_low = plan.breakevens[0] if len(plan.breakevens) > 0 else sp.strike - 30
        sl_high = plan.breakevens[1] if len(plan.breakevens) > 1 else sc.strike + 30

        return {
            "spot": round(spot, 2),
            "sell_put_strike": int(sp.strike),
            "sell_call_strike": int(sc.strike),
            "buy_put_strike": int(lp.strike),
            "buy_call_strike": int(lc.strike),
            "sell_put_price": round(sp.premium, 2),
            "sell_call_price": round(sc.premium, 2),
            "buy_put_price": round(lp.premium, 2),
            "buy_call_price": round(lc.premium, 2),
            "sell_put_tv": sp.symbol or _format_tv_symbol(plan.underlying, plan.expiry, sp.strike, "PE"),
            "sell_call_tv": sc.symbol or _format_tv_symbol(plan.underlying, plan.expiry, sc.strike, "CE"),
            "buy_put_tv": lp.symbol or _format_tv_symbol(plan.underlying, plan.expiry, lp.strike, "PE"),
            "buy_call_tv": lc.symbol or _format_tv_symbol(plan.underlying, plan.expiry, lc.strike, "CE"),
            "stop_loss_spot_low": round(sl_low, 1),
            "stop_loss_spot_high": round(sl_high, 1),
            "metrics": {
                "max_profit": round(plan.max_profit, 0),
                "max_loss": round(plan.max_loss, 0),
                "width": int(width),
                "credit": round(plan.net_credit, 2),
                "risk_reward": round(plan.risk_reward, 2),
                "pop": round(plan.prob_short_otm * 100, 1),
            }
        }


def generate_aptrade_data(cfg: Optional[Config] = None) -> dict:
    cfg = cfg or Config.load()
    provider = build_provider(cfg)
    engine = Engine(provider, cfg)

    expiries = provider.expiries("NIFTY", limit=6)
    if len(expiries) < 2:
        raise RuntimeError("Not enough NIFTY expiries available to generate APTrade data")

    # Next Week expiry (index 1 or 0 depending on DTE)
    next_week_exp = expiries[1] if expiries[0] and (expiries[0] - date.today()).days <= 2 else expiries[0]
    # Monthly expiry (find last expiry in list or next month)
    monthly_exp = expiries[-1]

    results = {}
    periods = [("next_week", next_week_exp), ("monthly", monthly_exp)]
    spot = provider.spot("NIFTY")

    for period_key, exp in periods:
        chain = provider.option_chain("NIFTY", exp, around_atm=16)
        iv_ctx = engine.iv_context_for("NIFTY", chain)
        trend = trend_of(chain.closes, 20, 50, 14)
        atm_iv = iv_of_series([q.iv for r in chain.rows for q in (r.ce, r.pe) if q and q.iv > 0]) or 0.14

        # Conservative V1 (~0.16 Delta)
        c_cfg = NiftySpreadCfg(short_delta=0.16, wing_delta=0.05, wing_width_points=200.0)
        v1_bull = _credit_spread(chain, c_cfg, cfg.account, iv_ctx, Trend("bullish", 0, 0, 55, 20, 5), atm_iv, "bullish", cfg.market.risk_free_rate, cfg.market.index_dividend_yield)
        v1_bear = _credit_spread(chain, c_cfg, cfg.account, iv_ctx, Trend("bearish", 0, 0, 45, 20, -5), atm_iv, "bearish", cfg.market.risk_free_rate, cfg.market.index_dividend_yield)
        v1_condor = _iron_condor(chain, c_cfg, cfg.account, iv_ctx, Trend("neutral", 0, 0, 50, 15, 0), atm_iv, cfg.market.risk_free_rate, cfg.market.index_dividend_yield)

        # Balanced V2 (~0.22 Delta)
        b_cfg = NiftySpreadCfg(short_delta=0.22, wing_delta=0.06, wing_width_points=250.0)
        v2_bull = _credit_spread(chain, b_cfg, cfg.account, iv_ctx, Trend("bullish", 0, 0, 55, 20, 5), atm_iv, "bullish", cfg.market.risk_free_rate, cfg.market.index_dividend_yield)
        v2_bear = _credit_spread(chain, b_cfg, cfg.account, iv_ctx, Trend("bearish", 0, 0, 45, 20, -5), atm_iv, "bearish", cfg.market.risk_free_rate, cfg.market.index_dividend_yield)
        v2_condor = _iron_condor(chain, b_cfg, cfg.account, iv_ctx, Trend("neutral", 0, 0, 50, 15, 0), atm_iv, cfg.market.risk_free_rate, cfg.market.index_dividend_yield)

        period_dict = {
            "bull": _format_strategy(v1_bull, "bull", chain.spot),
            "bear": _format_strategy(v1_bear, "bear", chain.spot),
            "sideways": _format_strategy(v1_condor, "sideways", chain.spot),
            "v2": {
                "bull": _format_strategy(v2_bull, "bull", chain.spot),
                "bear": _format_strategy(v2_bear, "bear", chain.spot),
                "sideways": _format_strategy(v2_condor, "sideways", chain.spot),
            }
        }
        results[period_key] = period_dict

    now_ist = datetime.now(TZ)
    return {
        "generated_at": now_ist.isoformat(),
        "spot": round(spot, 2),
        "results": results
    }


def export_to_file(out_path: str | Path = "docs/data.json") -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = generate_aptrade_data()
    out.write_text(json.dumps(payload, indent=2))
    return out
