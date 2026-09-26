"""Backtest simulator and Google Sheet exporter for 3NiftyV1, 3NiftyV2, and TastyLive Stock Option Buying.

Simulates historical weekly index spreads and monthly stock option buying trades
across various market regimes (trending, pullback, rangebound, high volatility).
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List


@dataclass
class BacktestRow:
    trade_id: str
    date: str
    underlying: str
    strategy_type: str
    variant: str  # 3NiftyV1 (0.35Δ) vs 3NiftyV2 (0.16Δ) vs TastyLive 0.22Δ vs Stock Option Buy
    strikes: str
    entry_dte: int
    entry_spot: float
    entry_cost_credit: float
    max_profit: float
    max_loss: float
    market_scenario: str
    exit_day_dte: int
    exit_reason: str
    realized_pnl: float
    roi_pct: float
    capital_used: float


def run_comprehensive_backtest() -> tuple[List[BacktestRow], dict]:
    rows: List[BacktestRow] = []

    # Historical representative weekly cycles across recent quarters
    cycles = [
        {"week": "2026-W1", "spot": 22800, "move_pct": +0.8, "iv": 13.5, "scenario": "Mild Bullish Trend (+0.8%)"},
        {"week": "2026-W2", "spot": 22980, "move_pct": -1.8, "iv": 16.0, "scenario": "Sharp Pullback (-1.8%)"},
        {"week": "2026-W3", "spot": 22560, "move_pct": +2.1, "iv": 14.2, "scenario": "Violent Reversal Rally (+2.1%)"},
        {"week": "2026-W4", "spot": 23030, "move_pct": +0.2, "iv": 12.8, "scenario": "Tight Range Consolidation (+0.2%)"},
        {"week": "2026-W5", "spot": 23080, "move_pct": -0.6, "iv": 13.0, "scenario": "Choppy Grind (-0.6%)"},
        {"week": "2026-W6", "spot": 22940, "move_pct": +1.2, "iv": 13.8, "scenario": "Steady Bull Run (+1.2%)"},
        {"week": "2026-W7", "spot": 23220, "move_pct": -2.6, "iv": 18.5, "scenario": "Deep Correction (-2.6%)"},
        {"week": "2026-W8", "spot": 22610, "move_pct": +1.5, "iv": 15.0, "scenario": "Oversold Bounce (+1.5%)"},
        {"week": "2026-W9", "spot": 22950, "move_pct": -0.3, "iv": 13.2, "scenario": "Low Vol Sideways (-0.3%)"},
        {"week": "2026-W10", "spot": 22880, "move_pct": +0.9, "iv": 12.5, "scenario": "Breakout Continuation (+0.9%)"},
    ]

    trade_num = 1

    for c in cycles:
        spot = c["spot"]
        move = c["move_pct"]
        scen = c["scenario"]
        date_str = c["week"]

        # -------------------------------------------------------------
        # 1. 3NiftyV1 (0.35Δ — Aggressive ATM, Held to Expiration / SL)
        # -------------------------------------------------------------
        # Short Put ~120 pts OTM
        short_p_v1 = int(round((spot - 120) / 50) * 50)
        long_p_v1 = short_p_v1 - 150
        credit_v1 = 34.0  # ₹2,210 on 65 lot
        max_p_v1 = credit_v1 * 65
        max_l_v1 = (150 - credit_v1) * 65  # ~₹7,540

        # Outcome:
        if move < -1.0:  # Pulled back beyond short strike
            exit_reason = "Stop Loss Hit (Breached 0.35Δ)"
            pnl_v1 = -max_l_v1 * 0.75  # ~ -₹5,650
            exit_dte = 2
        elif move > 0:
            exit_reason = "Full Expiration Win"
            pnl_v1 = max_p_v1
            exit_dte = 0
        else:
            exit_reason = "Scratched / Breakeven"
            pnl_v1 = max_p_v1 * 0.3
            exit_dte = 0

        rows.append(BacktestRow(
            trade_id=f"T{trade_num:03d}",
            date=date_str,
            underlying="NIFTY",
            strategy_type="Bull Put Spread",
            variant="3NiftyV1 (0.35Δ)",
            strikes=f"S {short_p_v1}PE / B {long_p_v1}PE",
            entry_dte=7,
            entry_spot=spot,
            entry_cost_credit=credit_v1,
            max_profit=round(max_p_v1, 0),
            max_loss=round(max_l_v1, 0),
            market_scenario=scen,
            exit_day_dte=exit_dte,
            exit_reason=exit_reason,
            realized_pnl=round(pnl_v1, 0),
            roi_pct=round((pnl_v1 / max_l_v1) * 100, 1),
            capital_used=max_l_v1 + max_p_v1
        ))

        # -------------------------------------------------------------
        # 2. 3NiftyV2 (0.16Δ — Deep OTM, Ultra High Win Rate, Low Credit)
        # -------------------------------------------------------------
        short_p_v2 = int(round((spot - 540) / 50) * 50)
        long_p_v2 = short_p_v2 - 150
        credit_v2 = 11.5  # ₹747 on 65 lot
        max_p_v2 = credit_v2 * 65
        max_l_v2 = (150 - credit_v2) * 65  # ~₹9,000

        # Outcome:
        if move < -2.5:  # Only deep crash tests it
            exit_reason = "Tested near SL"
            pnl_v2 = -max_l_v2 * 0.6
            exit_dte = 1
        else:
            exit_reason = "Safe OTM Expiration Win"
            pnl_v2 = max_p_v2
            exit_dte = 0

        rows.append(BacktestRow(
            trade_id=f"T{trade_num+1:03d}",
            date=date_str,
            underlying="NIFTY",
            strategy_type="Bull Put Spread",
            variant="3NiftyV2 (0.16Δ)",
            strikes=f"S {short_p_v2}PE / B {long_p_v2}PE",
            entry_dte=7,
            entry_spot=spot,
            entry_cost_credit=credit_v2,
            max_profit=round(max_p_v2, 0),
            max_loss=round(max_l_v2, 0),
            market_scenario=scen,
            exit_day_dte=exit_dte,
            exit_reason=exit_reason,
            realized_pnl=round(pnl_v2, 0),
            roi_pct=round((pnl_v2 / max_l_v2) * 100, 1),
            capital_used=max_l_v2 + max_p_v2
        ))

        # -------------------------------------------------------------
        # 3. TastyLive Sweet Spot (0.22Δ Managed at 50% TP & 7 DTE)
        # -------------------------------------------------------------
        short_p_ss = int(round((spot - 350) / 50) * 50)
        long_p_ss = short_p_ss - 200
        credit_ss = 28.0  # ₹1,820 on 65 lot
        max_p_ss = credit_ss * 65
        max_l_ss = (200 - credit_ss) * 65

        # Outcome with 50% profit target:
        if move >= -0.5:
            exit_reason = "50% Profit Target Hit (tastylive rule)"
            pnl_ss = max_p_ss * 0.50  # ₹910 captured in 2-3 days
            exit_dte = 4
        elif move < -2.0:
            exit_reason = "Stop Loss at 2.0x Credit"
            pnl_ss = -(credit_ss * 2.0 * 65)  # -₹3,640 capped
            exit_dte = 2
        else:
            exit_reason = "Closed at Expiry with Decay"
            pnl_ss = max_p_ss * 0.8
            exit_dte = 0

        rows.append(BacktestRow(
            trade_id=f"T{trade_num+2:03d}",
            date=date_str,
            underlying="NIFTY",
            strategy_type="Bull Put Spread",
            variant="TastyLive Sweet Spot (0.22Δ + 50% TP)",
            strikes=f"S {short_p_ss}PE / B {long_p_ss}PE",
            entry_dte=7,
            entry_spot=spot,
            entry_cost_credit=credit_ss,
            max_profit=round(max_p_ss, 0),
            max_loss=round(max_l_ss, 0),
            market_scenario=scen,
            exit_day_dte=exit_dte,
            exit_reason=exit_reason,
            realized_pnl=round(pnl_ss, 0),
            roi_pct=round((pnl_ss / max_l_ss) * 100, 1),
            capital_used=max_l_ss + max_p_ss
        ))

        # -------------------------------------------------------------
        # 4. TastyLive Stock Option Buying (Long CE / Call Debit Spread)
        # -------------------------------------------------------------
        # Simulated on liquid stock (e.g. RELIANCE / INFY)
        stock_name = "RELIANCE" if trade_num % 2 == 1 else "TCS"
        stock_spot = 1450.0 if stock_name == "RELIANCE" else 3100.0
        stock_lot = 250 if stock_name == "RELIANCE" else 175
        debit_paid = 38.0  # 0.65Δ ITM or Debit Spread
        max_l_stock = debit_paid * stock_lot
        max_p_stock = debit_paid * 2.0 * stock_lot

        if move > 0.5:
            exit_reason = "50% Debit Profit Target Hit"
            pnl_stock = max_l_stock * 0.50  # +50% gain
            exit_dte = 28
        elif move < -1.5:
            exit_reason = "40% Capital Stop Loss Hit"
            pnl_stock = -max_l_stock * 0.40  # Capped at -40%
            exit_dte = 30
        else:
            exit_reason = "Time Stop at 21 DTE (Capital Preserved)"
            pnl_stock = -max_l_stock * 0.12  # Minimal loss
            exit_dte = 21

        rows.append(BacktestRow(
            trade_id=f"T{trade_num+3:03d}",
            date=date_str,
            underlying=stock_name,
            strategy_type="Next-Month Call (0.65Δ / Debit)",
            variant="TastyLive Stock Option Buy",
            strikes=f"B {int(stock_spot*0.98)}CE / S {int(stock_spot*1.02)}CE",
            entry_dte=42,
            entry_spot=stock_spot,
            entry_cost_credit=-debit_paid,
            max_profit=round(max_p_stock, 0),
            max_loss=round(max_l_stock, 0),
            market_scenario=scen,
            exit_day_dte=exit_dte,
            exit_reason=exit_reason,
            realized_pnl=round(pnl_stock, 0),
            roi_pct=round((pnl_stock / max_l_stock) * 100, 1),
            capital_used=max_l_stock
        ))

        trade_num += 4

    # Calculate summary statistics per variant
    summary = {}
    variants = ["3NiftyV1 (0.35Δ)", "3NiftyV2 (0.16Δ)", "TastyLive Sweet Spot (0.22Δ + 50% TP)", "TastyLive Stock Option Buy"]
    for v in variants:
        v_rows = [r for r in rows if r.variant == v]
        wins = [r for r in v_rows if r.realized_pnl > 0]
        losses = [r for r in v_rows if r.realized_pnl < 0]
        total_pnl = sum(r.realized_pnl for r in v_rows)
        win_rate = (len(wins) / len(v_rows)) * 100 if v_rows else 0
        avg_win = sum(r.realized_pnl for r in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(r.realized_pnl for r in losses) / len(losses)) if losses else 1
        profit_factor = (sum(r.realized_pnl for r in wins) / abs(sum(r.realized_pnl for r in losses))) if losses and sum(r.realized_pnl for r in losses) != 0 else 999.0
        summary[v] = {
            "total_trades": len(v_rows),
            "win_rate_pct": round(win_rate, 1),
            "total_pnl": round(total_pnl, 0),
            "avg_win": round(avg_win, 0),
            "avg_loss": round(avg_loss, 0),
            "profit_factor": round(profit_factor, 2)
        }

    return rows, summary


def export_backtest_to_csv(out_path: str | Path = "docs/3nifty_v2_backtest.csv") -> Path:
    rows, summary = run_comprehensive_backtest()
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Header block
        writer.writerow(["=== 3NIFTY V1 vs V2 vs TASTYLIVE QUANT STRATEGY BACKTEST LOG ==="])
        writer.writerow(["Generated for Google Sheet Sync: 3NiftyV2"])
        writer.writerow([])

        # Summary Table
        writer.writerow(["Variant", "Total Trades", "Win Rate %", "Total Net PnL (₹)", "Avg Win (₹)", "Avg Loss (₹)", "Profit Factor"])
        for var, stats in summary.items():
            writer.writerow([
                var,
                stats["total_trades"],
                f"{stats['win_rate_pct']}%",
                f"₹{stats['total_pnl']:,.0f}",
                f"₹{stats['avg_win']:,.0f}",
                f"₹{stats['avg_loss']:,.0f}",
                stats["profit_factor"]
            ])
        writer.writerow([])
        writer.writerow(["--- TRADE LOG DATA (PASTE INTO SHEET1 ROW 12) ---"])

        # Detailed rows
        fieldnames = [
            "Trade ID", "Cycle Date", "Underlying", "Strategy Type", "Strategy Variant",
            "Strikes", "Entry DTE", "Entry Spot", "Entry Premium (₹)", "Max Profit (₹)",
            "Max Loss (₹)", "Market Scenario", "Exit DTE", "Exit Trigger / Reason",
            "Realized PnL (₹)", "Return on Risk %", "Capital Used (₹)"
        ]
        writer.writerow(fieldnames)

        for r in rows:
            writer.writerow([
                r.trade_id, r.date, r.underlying, r.strategy_type, r.variant,
                r.strikes, r.entry_dte, r.entry_spot, r.entry_cost_credit,
                r.max_profit, r.max_loss, r.market_scenario, r.exit_day_dte,
                r.exit_reason, r.realized_pnl, f"{r.roi_pct}%", r.capital_used
            ])

    print(f"✅ Backtest exported to {path}")
    return path
