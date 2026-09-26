"""Next-month stock option buying (long premium) setups.

The tastylive course is a premium-*selling* school, so the buying rules here are
the deliberate mirror image of its selling rules -- and that mirror is the point:

* sell premium when IV rank is HIGH  ->  buy premium when IV rank is LOW
* short options decay for you        ->  long options decay against you, so buy
  *time* (next month, not this week) and *moneyness* (0.55-0.75 delta)
* defined risk via a wing            ->  defined risk is the premium itself, or a
  short strike (debit spread) when vol is not cheap enough to pay full freight
"""
from __future__ import annotations

from typing import Optional

from ..config import AccountCfg, StockOptionCfg
from ..model import ExitRule, OptionChain, Signal, TradePlan
from ..quant import expected_move
from ..strategies import (
    build_plan, entry_price, make_leg, margin_estimate, select_strike_by_delta,
)
from ..volatility import IVContext, iv_of_series
from .rules import (
    Trend, liquidity, liquidity_quality, long_option_risk_per_lot, lots_for_risk,
    risk_budget, score_long_option, trend_of, delta_quality, cost_quality,
)


def scan_stocks(provider, cfg: StockOptionCfg, account: AccountCfg, ts,
                universe: Optional[list[str]] = None,
                iv_context_fn=None, r: float = 0.065, q: float = 0.0
                ) -> tuple[list[Signal], list[dict], int]:
    """Walk the F&O universe and return long-option signals.

    ``iv_context_fn(underlying, chain)`` lets the caller supply live IV history.
    """
    from ..volatility import iv_context as default_iv_context

    signals: list[Signal] = []
    rejected: list[dict] = []
    scanned = 0
    iv_fn = iv_context_fn or (lambda u, c: default_iv_context(
        iv_of_series([qq.iv for row in c.rows for qq in (row.ce, row.pe) if qq and qq.iv > 0]),
        c.iv_history))

    for underlying in (universe or provider.universe())[:cfg.max_universe]:
        try:
            expiries = provider.expiries(underlying, limit=max(cfg.expiry_index + 2, 3))
            if len(expiries) <= cfg.expiry_index:
                rejected.append({"underlying": underlying, "why": "not enough expiries listed"})
                continue
            expiry = expiries[cfg.expiry_index]
            chain = provider.option_chain(underlying, expiry, around_atm=10)
            scanned += 1
            if not (cfg.dte_min <= chain.dte <= cfg.dte_max):
                rejected.append({"underlying": underlying,
                                 "why": f"DTE {chain.dte} outside [{cfg.dte_min},{cfg.dte_max}]"})
                continue
            iv_ctx = iv_fn(underlying, chain)
            trend = trend_of(chain.closes, cfg.trend_fast, cfg.trend_slow, cfg.rsi_window)
            sig = build_stock_signal(chain, cfg, account, iv_ctx, trend, ts, underlying, r, q)
            if sig is None:
                rejected.append({"underlying": underlying, "why": _reject_reason(cfg, iv_ctx, trend)})
                continue
            signals.append(sig)
        except Exception as exc:  # one bad name must never kill the scan
            rejected.append({"underlying": underlying, "why": f"error: {exc}"})
    signals.sort(key=lambda s: s.score, reverse=True)
    return signals, rejected, scanned


def _reject_reason(cfg: StockOptionCfg, iv_ctx: IVContext, trend: Trend) -> str:
    if iv_ctx.iv_rank > cfg.max_iv_rank:
        return (f"IV rank {iv_ctx.iv_rank:.0f} > {cfg.max_iv_rank:.0f} "
                "(options are expensive; selling them is the better trade)")
    if trend.direction == "neutral":
        return f"no trend to buy: RSI {trend.rsi:.0f}, momentum {trend.momentum_pct:.1f}%"
    if trend.direction == "bullish" and not (cfg.rsi_long_min <= trend.rsi <= cfg.rsi_long_max):
        return f"bullish but RSI {trend.rsi:.0f} outside [{cfg.rsi_long_min:.0f},{cfg.rsi_long_max:.0f}]"
    if trend.direction == "bearish" and not (cfg.rsi_short_min <= trend.rsi <= cfg.rsi_short_max):
        return f"bearish but RSI {trend.rsi:.0f} outside [{cfg.rsi_short_min:.0f},{cfg.rsi_short_max:.0f}]"
    return "no strike passed the liquidity / cost filters"


def build_stock_signal(chain: OptionChain, cfg: StockOptionCfg, account: AccountCfg,
                       iv_ctx: IVContext, trend: Trend, ts, underlying: str,
                       r: float = 0.065, q: float = 0.0) -> Optional[Signal]:
    if not cfg.enabled or trend.direction == "neutral":
        return None
    if iv_ctx.iv_rank > cfg.max_iv_rank:
        return None
    if trend.direction == "bullish" and not (cfg.rsi_long_min <= trend.rsi <= cfg.rsi_long_max):
        return None
    if trend.direction == "bearish" and not (cfg.rsi_short_min <= trend.rsi <= cfg.rsi_short_max):
        return None
    if abs(trend.momentum_pct) < cfg.momentum_min_pct and trend.adx < cfg.adx_min:
        return None

    option_type = "CE" if trend.direction == "bullish" else "PE"
    atm_iv = iv_of_series([qq.iv for row in chain.rows for qq in (row.ce, row.pe)
                           if qq and qq.iv > 0]) or iv_ctx.iv or 0.2

    # A long option is the purest expression; a debit spread is the cheaper one.
    plan = _long_option(chain, cfg, account, iv_ctx, trend, atm_iv, option_type, r, q)
    if plan is None:
        plan = _stock_debit_spread(chain, cfg, account, iv_ctx, trend, atm_iv, option_type, r, q)
    elif iv_ctx.iv_rank >= cfg.prefer_debit_spread_above_iv_rank:
        cheaper = _stock_debit_spread(chain, cfg, account, iv_ctx, trend, atm_iv, option_type, r, q)
        if cheaper is not None and cheaper.max_loss < plan.max_loss * 0.75:
            cheaper.reasons.insert(0, f"IV rank {iv_ctx.iv_rank:.0f} is not rock bottom, "
                                      "so a debit spread cuts the cost of the same view")
            plan = cheaper
    if plan is None:
        return None

    em = expected_move(chain.spot, atm_iv, chain.dte, 1.0)
    return _to_signal(plan, iv_ctx, trend, em, ts, underlying, chain)


def _long_option(chain: OptionChain, cfg: StockOptionCfg, account: AccountCfg,
                 iv_ctx: IVContext, trend: Trend, atm_iv: float, option_type: str,
                 r: float, q: float) -> Optional[TradePlan]:
    target_delta = (cfg.delta_min + cfg.delta_max) / 2.0
    quote = select_strike_by_delta(chain, option_type, target_delta, above_spot=False,
                                   min_premium=0.5, max_spread_pct=cfg.max_spread_pct)
    if quote is None:
        return None
    lq = liquidity(quote, cfg.min_oi, cfg.max_spread_pct, 0.5)
    if not lq.ok:
        return None
    if not (cfg.delta_min <= abs(quote.delta) <= cfg.delta_max):
        return None
    if quote.mid / chain.spot * 100.0 > cfg.max_premium_pct_of_spot:
        return None

    price = entry_price(quote, "BUY")
    risk_per_lot = long_option_risk_per_lot(price, chain.lot_size)
    lots = lots_for_risk(risk_per_lot, risk_budget(account), account.max_lots_per_trade)
    if lots < 1:
        return None

    legs = [make_leg(quote, "BUY", lots, chain.lot_size, price)]
    direction = trend.direction
    strategy = f"Long {option_type} ({direction})"
    reasons = [
        f"IV rank {iv_ctx.iv_rank:.0f} (percentile {iv_ctx.iv_percentile:.0f}) -- buying volatility while it is cheap",
        f"trend is {direction}: {trend.reasons[0] if trend.reasons else ''}",
        f"RSI {trend.rsi:.0f}, ADX {trend.adx:.0f}, {trend.momentum_pct:+.1f}% over 20 sessions",
        f"{option_type} {quote.strike:,.0f} at {abs(quote.delta):.2f} delta keeps theta manageable",
        f"premium {price:.2f} = {price / chain.spot * 100:.1f}% of spot, lot size {chain.lot_size}",
    ]
    return build_plan(
        strategy, legs, chain, atm_iv, iv_ctx.iv_rank, iv_ctx.iv_percentile, lots,
        reasons=reasons,
        exit_rules=[
            ExitRule("take_profit", f"Book 50 % at {price * (1 + cfg.take_profit_pct):.2f}", 1),
            ExitRule("stop_loss", f"Exit at {price * (1 - cfg.stop_loss_pct):.2f} "
                                  f"({cfg.stop_loss_pct:.0%} loss)", 1),
            ExitRule("time", f"Exit with 7-10 DTE left (by {max(chain.dte - 10, 1)} DTE) -- "
                             "theta acceleration is the buyer's main enemy", 2),
            ExitRule("volatility", "Sell into an IV spike even if the price target is not hit", 3),
        ],
        score=score_long_option(
            iv_ctx.iv_rank, trend.strength,
            delta_quality(quote.delta, target_delta, 0.25),
            cost_quality(price, chain.spot, cfg.max_premium_pct_of_spot),
            liquidity_quality(quote, cfg.min_oi, cfg.max_spread_pct)),
        margin_estimate=margin_estimate(legs, chain.spot, atm_iv, chain.dte,
                                        chain.lot_size, lots),
        r=r, q=q,
    )


def _stock_debit_spread(chain: OptionChain, cfg: StockOptionCfg, account: AccountCfg,
                        iv_ctx: IVContext, trend: Trend, atm_iv: float, option_type: str,
                        r: float, q: float) -> Optional[TradePlan]:
    long_q = select_strike_by_delta(chain, option_type, 0.60, above_spot=False,
                                    min_premium=0.5, max_spread_pct=cfg.max_spread_pct)
    short_q = select_strike_by_delta(chain, option_type, 0.30, above_spot=True,
                                     min_premium=0.5, max_spread_pct=cfg.max_spread_pct * 1.5)
    if long_q is None or short_q is None or long_q.strike == short_q.strike:
        return None
    width = abs(short_q.strike - long_q.strike)
    cost = entry_price(long_q, "BUY") - entry_price(short_q, "SELL")
    if cost <= 0 or cost / width > 0.62:
        return None
    if not liquidity(long_q, cfg.min_oi, cfg.max_spread_pct, 0.5).ok:
        return None

    risk_per_lot = cost * chain.lot_size
    lots = lots_for_risk(risk_per_lot, risk_budget(account), account.max_lots_per_trade)
    if lots < 1:
        return None

    legs = [make_leg(long_q, "BUY", lots, chain.lot_size, entry_price(long_q, "BUY")),
            make_leg(short_q, "SELL", lots, chain.lot_size, entry_price(short_q, "SELL"))]
    strategy = f"{trend.direction.title()} {option_type} Debit Spread"
    reasons = [
        f"IV rank {iv_ctx.iv_rank:.0f} -- cheap enough to buy, and the short leg pays for part of it",
        f"trend is {trend.direction}: {trend.reasons[0] if trend.reasons else ''}",
        f"long {option_type} {long_q.strike:,.0f} @ {abs(long_q.delta):.2f} delta, "
        f"short {short_q.strike:,.0f} @ {abs(short_q.delta):.2f} delta",
        f"net cost {cost:.2f} of a {width:,.0f}-point width ({cost / width:.0%}), "
        f"max value {width - cost:.2f}",
    ]
    return build_plan(
        strategy, legs, chain, atm_iv, iv_ctx.iv_rank, iv_ctx.iv_percentile, lots,
        reasons=reasons,
        exit_rules=[
            ExitRule("take_profit", f"Close at {(width - cost) * 0.6:.2f}/unit (60 % of max)", 1),
            ExitRule("stop_loss", f"Close at {cost * 0.5:.2f}/unit (50 % of cost)", 1),
            ExitRule("time", f"Exit by {max(chain.dte - 10, 1)} DTE", 2),
        ],
        score=score_long_option(
            iv_ctx.iv_rank, trend.strength,
            delta_quality(long_q.delta, 0.60, 0.25),
            cost_quality(cost, chain.spot, cfg.max_premium_pct_of_spot),
            liquidity_quality(long_q, cfg.min_oi, cfg.max_spread_pct)) * 0.95,
        margin_estimate=margin_estimate(legs, chain.spot, atm_iv, chain.dte,
                                        chain.lot_size, lots),
        r=r, q=q,
    )


def _to_signal(plan: TradePlan, iv_ctx: IVContext, trend: Trend, em: float, ts,
               underlying: str, chain: OptionChain) -> Signal:
    legs_txt = " / ".join(f"{l.side[0]} {l.option_type} {l.strike:,.0f}" for l in plan.legs)
    return Signal(
        id=f"{underlying}-LONGOPT-{plan.expiry.isoformat()}-{trend.direction}",
        ts=ts,
        kind="STOCK_LONG_OPTION",
        direction=trend.direction,
        underlying=underlying,
        headline=(f"{plan.strategy} on {underlying} exp {plan.expiry:%d %b} "
                  f"({plan.dte} DTE) -- {legs_txt}"),
        plan=plan,
        iv_context=iv_ctx.to_dict(),
        score=plan.score,
        reasons=list(plan.reasons) + [
            f"expected move to expiry +/- {em:,.0f} ({em / chain.spot * 100:.1f}% of spot)",
            f"max loss = premium at risk: {plan.max_loss:,.0f} for {plan.lots} lot(s)",
        ],
        data={"trend": {"direction": trend.direction, "rsi": round(trend.rsi, 1),
                        "adx": round(trend.adx, 1), "momentum_pct": round(trend.momentum_pct, 2),
                        "sma_fast": round(trend.fast, 2), "sma_slow": round(trend.slow, 2)},
              "expected_move_points": round(em, 1)},
    )
