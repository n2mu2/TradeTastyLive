"""Nifty hedged-spread setups (credit spreads, iron condors, debit spreads).

Decision logic, straight out of the course and adapted to NSE mechanics:

1. Read where volatility sits in its own history (IV rank / percentile).
2. Read the trend of the underlying (SMA cross + RSI + ADX + momentum).
3. Rich vol  -> *sell* premium, hedged: bull put / bear call credit spread, or an
   iron condor when the market is directionless.
4. Cheap vol -> *buy* premium with defined risk: call / put debit spread.
5. Choose strikes by **delta**, not by eyeballing points away from spot.
6. Reject anything that fails POP, credit-to-width, spread width, OI or sizing.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional

from ..config import AccountCfg, NiftySpreadCfg
from ..model import ExitRule, Leg, OptionChain, OptionQuote, Signal, TradePlan
from ..quant import expected_move
from ..strategies import (
    build_plan, entry_price, find_quote, make_leg, margin_estimate,
    select_strike_by_delta, select_strike_offset,
)
from ..volatility import IVContext, iv_context, iv_of_series
from .rules import (
    Trend, credit_spread_risk_per_lot, liquidity, liquidity_quality,
    lots_for_risk, risk_budget, score_credit_setup, trend_alignment, trend_of,
)


def build_index_signals(chain: OptionChain, cfg: NiftySpreadCfg, account: AccountCfg,
                        iv_ctx: IVContext, trend: Trend, ts,
                        underlying: str = "NIFTY",
                        r: float = 0.065, q: float = 0.0) -> tuple[list[Signal], list[dict]]:
    """Return (signals, rejected) for one index chain."""
    rejected: list[dict] = []
    if not cfg.enabled:
        return [], rejected
    if not (cfg.dte_min <= chain.dte <= cfg.dte_max):
        rejected.append({"underlying": underlying, "why": f"DTE {chain.dte} outside "
                                                        f"[{cfg.dte_min},{cfg.dte_max}]"})
        return [], rejected

    atm_iv = iv_of_series([q.iv for row in chain.rows for q in (row.ce, row.pe) if q and q.iv > 0]) \
        or iv_ctx.iv or 0.15
    em = expected_move(chain.spot, atm_iv, chain.dte, 1.0)

    candidates: list[tuple[str, Optional[TradePlan]]] = []
    sell_side = iv_ctx.iv_rank >= cfg.min_iv_rank
    buy_side = cfg.allow_debit_spreads and iv_ctx.iv_rank <= cfg.debit_iv_rank_max

    if sell_side and cfg.strategy_bias in ("auto", "iron_condor", "credit_spread"):
        if trend.direction == "neutral" and cfg.strategy_bias in ("auto", "iron_condor"):
            candidates.append(("iron_condor", _iron_condor(chain, cfg, account, iv_ctx, trend,
                                                           atm_iv, r, q)))
        if trend.direction == "bullish" and cfg.strategy_bias in ("auto", "credit_spread"):
            candidates.append(("bull_put_credit_spread",
                               _credit_spread(chain, cfg, account, iv_ctx, trend, atm_iv,
                                              "bullish", r, q)))
        if trend.direction == "bearish" and cfg.strategy_bias in ("auto", "credit_spread"):
            candidates.append(("bear_call_credit_spread",
                               _credit_spread(chain, cfg, account, iv_ctx, trend, atm_iv,
                                              "bearish", r, q)))
        if trend.direction != "neutral" and cfg.strategy_bias == "auto":
            # also offer the neutral version so the trader can compare risk/reward
            candidates.append(("iron_condor", _iron_condor(chain, cfg, account, iv_ctx, trend,
                                                           atm_iv, r, q)))

    if buy_side and cfg.strategy_bias in ("auto", "debit_spread"):
        direction = trend.direction if trend.direction != "neutral" else "bullish"
        candidates.append((f"{direction}_debit_spread",
                           _debit_spread(chain, cfg, account, iv_ctx, trend, atm_iv,
                                         direction, r, q)))

    signals: list[Signal] = []
    for name, plan in candidates:
        pop_thresh = max(cfg.min_pop - 0.08, 0.58) if name.startswith("iron") else cfg.min_pop
        if plan is None:
            rejected.append({"underlying": underlying, "why": f"{name}: no valid strike combo"})
            continue
        if plan.prob_short_otm < pop_thresh and not name.endswith("debit_spread"):
            rejected.append({"underlying": underlying,
                             "why": f"{name}: POP {plan.prob_short_otm:.0%} < {pop_thresh:.0%}"})
            continue
        if name.endswith("debit_spread") and plan.prob_max_profit < cfg.debit_min_pop:
            rejected.append({"underlying": underlying,
                             "why": f"{name}: P(profit) {plan.prob_max_profit:.0%} < {cfg.debit_min_pop:.0%}"})
            continue
        signals.append(_to_signal(plan, iv_ctx, trend, em, ts, underlying, name))

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals, rejected


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _credit_spread(chain: OptionChain, cfg: NiftySpreadCfg, account: AccountCfg,
                   iv_ctx: IVContext, trend: Trend, atm_iv: float, direction: str,
                   r: float, q: float) -> Optional[TradePlan]:
    option_type = "PE" if direction == "bullish" else "CE"
    short_q = select_strike_by_delta(chain, option_type, cfg.short_delta, above_spot=True,
                                     min_premium=cfg.min_premium,
                                     max_spread_pct=cfg.max_spread_pct)
    if short_q is None:
        return None
    wing = _pick_wing(chain, cfg, option_type, short_q)
    if wing is None:
        return None
    lq_short = liquidity(short_q, cfg.min_oi, cfg.max_spread_pct, cfg.min_premium)
    lq_wing = liquidity(wing, max(cfg.min_oi // 3, 100), cfg.max_spread_pct * 1.5, cfg.min_premium * 0.5)
    if not lq_short.ok:
        return None

    width = abs(short_q.strike - wing.strike)
    if width <= 0:
        return None
    credit = entry_price(short_q, "SELL") - entry_price(wing, "BUY")
    if credit <= 0:
        return None
    min_credit_ratio = max(0.05, cfg.min_credit_pct_of_width * math.sqrt(max(chain.dte, 1) / 45.0))
    if credit / width < min_credit_ratio:
        return None

    risk_per_lot = credit_spread_risk_per_lot(width, credit, chain.lot_size)
    lots = lots_for_risk(risk_per_lot, risk_budget(account), account.max_lots_per_trade)
    lots = max(min(lots, account.max_lots_per_trade), 1)

    legs = [
        make_leg(short_q, "SELL", lots, chain.lot_size, entry_price(short_q, "SELL")),
        make_leg(wing, "BUY", lots, chain.lot_size, entry_price(wing, "BUY")),
    ]
    strategy = "Bull Put Credit Spread" if direction == "bullish" else "Bear Call Credit Spread"
    reasons = [
        f"IV rank {iv_ctx.iv_rank:.0f} (percentile {iv_ctx.iv_percentile:.0f}) -- vol is rich, so sell it",
        f"trend is {trend.direction}: {trend.reasons[0] if trend.reasons else ''}",
        f"short {option_type} {short_q.strike:,.0f} at {abs(short_q.delta):.2f} delta "
        f"= ~{abs(short_q.delta) * 100:.0f} % chance of finishing ITM",
        f"credit {credit:.2f} on a {width:,.0f}-point width = {credit / width:.0%} of the risk",
    ]
    if lq_wing.problems:
        reasons.append("note: wing book is thin (" + "; ".join(lq_wing.problems) + ")")
    plan = build_plan(
        strategy, legs, chain, atm_iv, iv_ctx.iv_rank, iv_ctx.iv_percentile, lots,
        reasons=reasons,
        exit_rules=_credit_exit_rules(cfg, credit, risk_per_lot),
        score=score_credit_setup(
            plan_pop_guess(chain, short_q, atm_iv, r, q), iv_ctx.iv_rank, credit / width,
            trend_alignment(trend, direction),
            liquidity_quality(short_q, cfg.min_oi, cfg.max_spread_pct)),
        margin_estimate=margin_estimate(legs, chain.spot, atm_iv, chain.dte,
                                        chain.lot_size, lots),
        r=r, q=q,
    )
    return plan


def _iron_condor(chain: OptionChain, cfg: NiftySpreadCfg, account: AccountCfg,
                 iv_ctx: IVContext, trend: Trend, atm_iv: float,
                 r: float, q: float) -> Optional[TradePlan]:
    sp = select_strike_by_delta(chain, "PE", cfg.short_delta, above_spot=True,
                                min_premium=cfg.min_premium, max_spread_pct=cfg.max_spread_pct)
    sc = select_strike_by_delta(chain, "CE", cfg.short_delta, above_spot=True,
                                min_premium=cfg.min_premium, max_spread_pct=cfg.max_spread_pct)
    if sp is None or sc is None:
        return None
    lp = _pick_wing(chain, cfg, "PE", sp)
    lc = _pick_wing(chain, cfg, "CE", sc)
    if lp is None or lc is None:
        return None
    if not liquidity(sp, cfg.min_oi, cfg.max_spread_pct, cfg.min_premium).ok:
        return None
    if not liquidity(sc, cfg.min_oi, cfg.max_spread_pct, cfg.min_premium).ok:
        return None

    put_width = abs(sp.strike - lp.strike)
    call_width = abs(lc.strike - sc.strike)
    if put_width <= 0 or call_width <= 0:
        return None
    credit = (entry_price(sp, "SELL") + entry_price(sc, "SELL")
              - entry_price(lp, "BUY") - entry_price(lc, "BUY"))
    if credit <= 0:
        return None
    width = min(put_width, call_width)
    min_condor_credit = max(0.04, cfg.min_credit_pct_of_width * 0.6 * math.sqrt(max(chain.dte, 1) / 45.0))
    if credit / width < min_condor_credit:
        return None

    risk_per_lot = credit_spread_risk_per_lot(width, credit, chain.lot_size)
    lots = lots_for_risk(risk_per_lot, risk_budget(account), account.max_lots_per_trade)
    lots = max(min(lots, account.max_lots_per_trade), 1)

    legs = [
        make_leg(sp, "SELL", lots, chain.lot_size, entry_price(sp, "SELL")),
        make_leg(lp, "BUY", lots, chain.lot_size, entry_price(lp, "BUY")),
        make_leg(sc, "SELL", lots, chain.lot_size, entry_price(sc, "SELL")),
        make_leg(lc, "BUY", lots, chain.lot_size, entry_price(lc, "BUY")),
    ]
    reasons = [
        f"IV rank {iv_ctx.iv_rank:.0f} (percentile {iv_ctx.iv_percentile:.0f}) -- rich vol favours selling",
        f"trend is {trend.direction}, so both wings are sold for premium",
        f"short strikes {sp.strike:,.0f}P / {sc.strike:,.0f}C at ~{cfg.short_delta:.2f} delta each",
        f"credit {credit:.2f} vs {width:,.0f}-point max loss on the tighter side",
    ]
    plan = build_plan(
        "Iron Condor", legs, chain, atm_iv, iv_ctx.iv_rank, iv_ctx.iv_percentile, lots,
        reasons=reasons,
        exit_rules=_credit_exit_rules(cfg, credit, risk_per_lot),
        score=score_credit_setup(
            plan_pop_guess(chain, sp, atm_iv, r, q), iv_ctx.iv_rank, credit / width,
            0.75, min(liquidity_quality(sp, cfg.min_oi, cfg.max_spread_pct),
                      liquidity_quality(sc, cfg.min_oi, cfg.max_spread_pct))),
        margin_estimate=margin_estimate(legs, chain.spot, atm_iv, chain.dte,
                                        chain.lot_size, lots),
        r=r, q=q,
    )
    return plan


def _debit_spread(chain: OptionChain, cfg: NiftySpreadCfg, account: AccountCfg,
                  iv_ctx: IVContext, trend: Trend, atm_iv: float, direction: str,
                  r: float, q: float) -> Optional[TradePlan]:
    long_type = "CE" if direction == "bullish" else "PE"
    long_q = select_strike_by_delta(chain, long_type, 0.62, above_spot=False,
                                    min_premium=cfg.min_premium,
                                    max_spread_pct=cfg.max_spread_pct)
    short_q = select_strike_by_delta(chain, long_type, 0.30, above_spot=True,
                                     min_premium=cfg.min_premium,
                                     max_spread_pct=cfg.max_spread_pct)
    if long_q is None or short_q is None or long_q.strike == short_q.strike:
        return None
    width = abs(short_q.strike - long_q.strike)
    if width <= 0:
        return None
    cost = entry_price(long_q, "BUY") - entry_price(short_q, "SELL")
    if cost <= 0 or cost / width > 0.65:
        return None

    risk_per_lot = cost * chain.lot_size
    lots = lots_for_risk(risk_per_lot, risk_budget(account), account.max_lots_per_trade)
    lots = max(min(lots, account.max_lots_per_trade), 1)

    legs = [
        make_leg(long_q, "BUY", lots, chain.lot_size, entry_price(long_q, "BUY")),
        make_leg(short_q, "SELL", lots, chain.lot_size, entry_price(short_q, "SELL")),
    ]
    strategy = "Bull Call Debit Spread" if direction == "bullish" else "Bear Put Debit Spread"
    reasons = [
        f"IV rank {iv_ctx.iv_rank:.0f} -- vol is cheap, so buying premium is not a tax",
        f"trend is {trend.direction}: {trend.reasons[0] if trend.reasons else ''}",
        f"long {long_type} {long_q.strike:,.0f} at {abs(long_q.delta):.2f} delta, "
        f"financed by selling {short_q.strike:,.0f}",
        f"cost {cost:.2f} = {cost / width:.0%} of the {width:,.0f}-point width",
    ]
    return build_plan(
        strategy, legs, chain, atm_iv, iv_ctx.iv_rank, iv_ctx.iv_percentile, lots,
        reasons=reasons,
        exit_rules=[
            ExitRule("take_profit", f"Close at 50-75 % of max value ({(width - cost) * 0.5:.2f}/unit profit)"),
            ExitRule("stop_loss", f"Close if the spread falls to {cost * 0.5:.2f}/unit (50 % loss)"),
            ExitRule("time", f"Exit by {max(chain.dte - 7, 1)} DTE -- long options lose theta fastest at the end"),
        ],
        score=score_credit_setup(0.0, 100.0 - iv_ctx.iv_rank, 1.0 - cost / width,
                                 trend_alignment(trend, direction),
                                 liquidity_quality(long_q, cfg.min_oi, cfg.max_spread_pct)) * 0.9,
        margin_estimate=margin_estimate(legs, chain.spot, atm_iv, chain.dte,
                                        chain.lot_size, lots),
        r=r, q=q,
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _pick_wing(chain: OptionChain, cfg: NiftySpreadCfg, option_type: str,
               short_q: OptionQuote) -> Optional[OptionQuote]:
    """Wing strike: the wider of (delta-based) and (fixed point width)."""
    by_delta = select_strike_by_delta(chain, option_type, cfg.wing_delta,
                                      above_spot=True, min_premium=0.05,
                                      max_spread_pct=cfg.max_spread_pct * 2)
    by_points = select_strike_offset(chain, option_type, cfg.wing_width_points,
                                     round_up=option_type == "CE")
    wing = None
    if by_points is not None:
        wing = by_points.side(option_type)
    if by_delta is not None:
        # choose whichever puts the wing further away -> more protection per rupee of credit lost
        if wing is None or abs(by_delta.strike - short_q.strike) > abs(wing.strike - short_q.strike):
            wing = by_delta
    if wing is None or wing.strike == short_q.strike:
        return None
    if option_type == "CE" and wing.strike <= short_q.strike:
        return None
    if option_type == "PE" and wing.strike >= short_q.strike:
        return None
    return wing


def plan_pop_guess(chain: OptionChain, short_q: OptionQuote, iv: float,
                   r: float, q: float) -> float:
    from ..quant import prob_above, years_to_expiry

    t = years_to_expiry(max(chain.dte, 0))
    if short_q.option_type.upper().startswith("C"):
        return 1.0 - prob_above(chain.spot, short_q.strike, t, iv or 1e-6, r, q)
    return prob_above(chain.spot, short_q.strike, t, iv or 1e-6, r, q)


def _credit_exit_rules(cfg: NiftySpreadCfg, credit: float, risk_per_lot: float) -> list[ExitRule]:
    return [
        ExitRule("take_profit",
                 f"Buy the spread back at 50 % of max profit (keep {credit * cfg.take_profit_pct:.2f}/unit)",
                 priority=1),
        ExitRule("stop_loss",
                 f"Stop if the spread is worth {credit * cfg.stop_loss_multiple:.2f}/unit "
                 f"({cfg.stop_loss_multiple}x credit received)",
                 priority=1),
        ExitRule("time",
                 f"Close or roll at {cfg.roll_dte} DTE -- gamma explodes in the last days",
                 priority=2),
        ExitRule("delta",
                 "Roll the tested side when the short strike reaches ~30 delta",
                 priority=3),
    ]


def _to_signal(plan: TradePlan, iv_ctx: IVContext, trend: Trend, expected_move_pts: float,
               ts, underlying: str, key: str) -> Signal:
    legs_txt = " / ".join(f"{l.side[0]} {l.option_type} {l.strike:,.0f}" for l in plan.legs)
    headline = (f"{plan.strategy} on {underlying} exp {plan.expiry:%d %b} "
                f"({plan.dte} DTE) -- {legs_txt}")
    reasons = list(plan.reasons) + [
        f"expected move to expiry +/- {expected_move_pts:,.0f} points",
        f"risk per lot {plan.max_loss / max(plan.lots, 1):,.0f}; sizing {plan.lots} lot(s)",
    ]
    return Signal(
        id=f"{underlying}-{key}-{plan.expiry.isoformat()}",
        ts=ts,
        kind="NIFTY_SPREAD",
        direction={"Bull Put Credit Spread": "bullish", "Bear Call Credit Spread": "bearish",
                   "Iron Condor": "neutral"}.get(plan.strategy,
                                                 "bullish" if "Bull" in plan.strategy else "bearish"),
        underlying=underlying,
        headline=headline,
        plan=plan,
        iv_context=iv_ctx.to_dict(),
        score=plan.score,
        reasons=reasons,
        data={"trend": {"direction": trend.direction, "rsi": round(trend.rsi, 1),
                        "adx": round(trend.adx, 1), "momentum_pct": round(trend.momentum_pct, 2)},
              "expected_move_points": round(expected_move_pts, 1)},
    )
