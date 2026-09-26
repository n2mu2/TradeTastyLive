"""Test suite for OptionDesk quant, strategies, indicators and engine."""
import math
import pytest
from datetime import date, datetime

from optiondesk.quant import (
    bs_price, bs_greeks, implied_volatility, prob_above, prob_below,
    prob_touch, expected_move, years_to_expiry, breakevens_from_payoff
)
from optiondesk.volatility import iv_context, historical_volatility, term_structure
from optiondesk.indicators import sma, ema, rsi, adx, momentum
from optiondesk.model import OptionChain, OptionQuote, ChainRow, Leg
from optiondesk.strategies import analyze_payoff, pnl_at, select_strike_by_delta, build_plan
from optiondesk.data.replay import ReplayProvider
from optiondesk.config import Config
from optiondesk.signals.engine import Engine
from optiondesk.signals.rules import trend_of, liquidity, lots_for_risk
from optiondesk.store import SignalStore


def test_black_scholes_call_put_parity():
    spot = 25000.0
    strike = 25000.0
    t = 30.0 / 365.0
    vol = 0.15
    r = 0.065
    q = 0.012

    call = bs_price(spot, strike, t, vol, "CE", r=r, q=q)
    put = bs_price(spot, strike, t, vol, "PE", r=r, q=q)

    # Put-Call parity with dividends: C - P = S*exp(-q*t) - K*exp(-r*t)
    parity_diff = (call - put) - (spot * math.exp(-q * t) - strike * math.exp(-r * t))
    assert abs(parity_diff) < 1e-4


def test_implied_volatility_roundtrip():
    spot = 25000.0
    strike = 25200.0
    t = 14.0 / 365.0
    true_vol = 0.18
    r = 0.065
    q = 0.012

    price_ce = bs_price(spot, strike, t, true_vol, "CE", r, q)
    solved_iv_ce = implied_volatility(price_ce, spot, strike, t, "CE", r, q)
    assert abs(solved_iv_ce - true_vol) < 1e-4

    price_pe = bs_price(spot, strike, t, true_vol, "PE", r, q)
    solved_iv_pe = implied_volatility(price_pe, spot, strike, t, "PE", r, q)
    assert abs(solved_iv_pe - true_vol) < 1e-4


def test_greeks():
    spot = 25000.0
    strike = 25000.0
    t = 30.0 / 365.0
    vol = 0.15

    g_ce = bs_greeks(spot, strike, t, vol, "CE")
    assert 0.45 < g_ce["delta"] < 0.55
    assert g_ce["gamma"] > 0
    assert g_ce["theta"] < 0  # theta decays
    assert g_ce["vega"] > 0   # long option benefits from vol rise

    g_pe = bs_greeks(spot, strike, t, vol, "PE")
    assert -0.55 < g_pe["delta"] < -0.45
    assert g_pe["gamma"] > 0
    assert g_pe["theta"] < 0


def test_probabilities_and_expected_move():
    spot = 25000.0
    t = 30.0 / 365.0
    vol = 0.15

    # ATM strike: P(S > K) ~ 50%
    p_up = prob_above(spot, spot, t, vol)
    assert 0.45 < p_up < 0.55

    # High strike: P(S > K) < 50%
    p_far = prob_above(spot, 26000.0, t, vol)
    assert p_far < 0.20

    em = expected_move(spot, vol, 30)
    assert em > 500  # expected move on Nifty ~1000 pts over a month


def test_payoff_bull_put_credit_spread():
    # Bull put spread: Short 25000 PE @ 100, Long 24800 PE @ 40
    # Net credit = 60, Width = 200, Lot size = 65
    legs = [
        Leg(side="SELL", option_type="PE", strike=25000, premium=100, lots=1, lot_size=65),
        Leg(side="BUY", option_type="PE", strike=24800, premium=40, lots=1, lot_size=65),
    ]
    net_credit_total = sum(l.cash for l in legs)  # (100 - 40) * 65 = 3900
    assert net_credit_total == 3900.0

    res = analyze_payoff(legs, net_credit_total, 25000.0)
    assert res["max_profit"] == 3900.0
    assert res["max_loss"] == (200 - 60) * 65  # 140 * 65 = 9100.0
    assert len(res["breakevens"]) == 1
    assert abs(res["breakevens"][0] - 24940.0) < 1.0


def test_payoff_iron_condor():
    # Iron Condor: 24800/25000 Put Spread + 25600/25800 Call Spread
    legs = [
        Leg(side="BUY", option_type="PE", strike=24800, premium=40, lots=1, lot_size=65),
        Leg(side="SELL", option_type="PE", strike=25000, premium=100, lots=1, lot_size=65),
        Leg(side="SELL", option_type="CE", strike=25600, premium=100, lots=1, lot_size=65),
        Leg(side="BUY", option_type="CE", strike=25800, premium=40, lots=1, lot_size=65),
    ]
    net_credit_total = sum(l.cash for l in legs)  # (60 + 60) * 65 = 7800
    assert net_credit_total == 7800.0

    res = analyze_payoff(legs, net_credit_total, 25300.0)
    assert res["max_profit"] == 7800.0
    # max loss on 200 pt wing = (200 - 120) * 65 = 5200.0
    assert res["max_loss"] == 5200.0
    assert len(res["breakevens"]) == 2
    assert abs(res["breakevens"][0] - (25000 - 120)) < 1.0
    assert abs(res["breakevens"][1] - (25600 + 120)) < 1.0


def test_iv_rank_and_percentile():
    history = [12.0, 14.0, 15.0, 18.0, 20.0, 22.0, 25.0]
    ctx = iv_context(20.0, history)
    assert ctx.iv_low == 12.0
    assert ctx.iv_high == 25.0
    assert round(ctx.iv_rank, 1) == round((20.0 - 12.0) / (25.0 - 12.0) * 100, 1)
    # 4 out of 7 are below 20.0
    assert round(ctx.iv_percentile, 1) == round(4.0 / 7.0 * 100, 1)


def test_indicators():
    closes = [100.0 + i for i in range(60)]
    s20 = sma(closes, 20)
    assert round(s20[-1], 2) == round(sum(closes[-20:]) / 20.0, 2)

    r = rsi(closes, 14)
    assert r[-1] == 100.0  # purely upward moves

    tr = trend_of(closes, 20, 50, 14)
    assert tr.direction == "bullish"


def test_replay_provider_and_engine():
    cfg = Config()
    p = ReplayProvider()
    eng = Engine(p, cfg)
    res = eng.scan()
    assert res.scanned > 0
    assert res.provider == "replay"
    assert isinstance(res.signals, list)


def test_store_sqlite(tmp_path):
    db_file = tmp_path / "test.db"
    store = SignalStore(db_file)
    cfg = Config()
    p = ReplayProvider()
    eng = Engine(p, cfg)
    res = eng.scan()
    count = store.save_scan(res)
    assert count == len(res.signals)
    stats = store.stats()
    assert stats["scans"] == 1
    assert stats["signals"] == len(res.signals)
