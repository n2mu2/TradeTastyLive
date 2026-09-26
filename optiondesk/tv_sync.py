"""TradingView Watchlist Synchronizer for 3NiftyV2.

Generates the exact TradingView watchlist format with:
- Section header: ###TASTYLIVE (0.16Δ NW & CM • 10K MAX RISK)
- Divider: ###🔶🔶🔶🔶🔶🔶🔶🔶🔶🔶🔶🔶🔶🔶🔶
- Strategy summaries: ###NW • 🟢 Bullish • P ₹1.4k • L ₹8.6k • SL25081 • TG25100
- Leg descriptions & NSE symbols:
  ###Sell Nifty PE 25100 29 Sep 2026 • ₹25
  NSE:NIFTY260929P25100
  ###Buy Nifty PE 24900 29 Sep 2026 • ₹6
  NSE:NIFTY260929P24900

Pushes to TradingView API for Watchlist '3NiftyV2' on Account 1.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional
import requests


def build_tradingview_symbols(data_path: str | Path = "docs/data.json") -> List[str]:
    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found at {path}")

    d = json.loads(path.read_text())
    results = d.get("results", {})

    lines: List[str] = [
        "###TASTYLIVE (0.16Δ NW & CM • 10K MAX RISK)"
    ]
    divider = "###" + "🔶" * 15

    for exp_key, exp_label in [("next_week", "NW"), ("monthly", "CM")]:
        period_data = results.get(exp_key, {})
        if not period_data:
            continue

        # Bullish
        bull = period_data.get("bull")
        if bull:
            lines.append(divider)
            m = bull.get("metrics", {})
            p_val = m.get("max_profit", 0)
            l_val = m.get("max_loss", 0)
            p = f"₹{p_val/1000:.1f}k" if p_val >= 1000 else f"₹{int(p_val)}"
            l = f"₹{l_val/1000:.1f}k" if l_val >= 1000 else f"₹{int(l_val)}"
            sl = int(bull.get("stop_loss_spot", 0))
            tg = int(bull.get("sell_strike", 0))
            lines.append(f"###{exp_label} • 🟢 Bullish • P {p} • L {l} • SL{sl} • TG{tg}")
            lines.append(f"###Sell Nifty PE {bull.get('sell_strike')} • ₹{bull.get('sell_price')}")
            lines.append(f"NSE:{bull.get('sell_tv_symbol')}")
            lines.append(f"###Buy Nifty PE {bull.get('buy_strike')} • ₹{bull.get('buy_price')}")
            lines.append(f"NSE:{bull.get('buy_tv_symbol')}")

        # Bearish
        bear = period_data.get("bear")
        if bear:
            lines.append(divider)
            m = bear.get("metrics", {})
            p_val = m.get("max_profit", 0)
            l_val = m.get("max_loss", 0)
            p = f"₹{p_val/1000:.1f}k" if p_val >= 1000 else f"₹{int(p_val)}"
            l = f"₹{l_val/1000:.1f}k" if l_val >= 1000 else f"₹{int(l_val)}"
            sl = int(bear.get("stop_loss_spot", 0))
            tg = int(bear.get("sell_strike", 0))
            lines.append(f"###{exp_label} • 🔴 Bearish • P {p} • L {l} • SL{sl} • TG{tg}")
            lines.append(f"###Sell Nifty CE {bear.get('sell_strike')} • ₹{bear.get('sell_price')}")
            lines.append(f"NSE:{bear.get('sell_tv_symbol')}")
            lines.append(f"###Buy Nifty CE {bear.get('buy_strike')} • ₹{bear.get('buy_price')}")
            lines.append(f"NSE:{bear.get('buy_tv_symbol')}")

        # Sideways (Iron Condor)
        side = period_data.get("sideways")
        if side:
            lines.append(divider)
            m = side.get("metrics", {})
            p_val = m.get("max_profit", 0)
            l_val = m.get("max_loss", 0)
            p = f"₹{p_val/1000:.1f}k" if p_val >= 1000 else f"₹{int(p_val)}"
            l = f"₹{l_val/1000:.1f}k" if l_val >= 1000 else f"₹{int(l_val)}"
            sl_low = int(side.get("stop_loss_spot_low", 0))
            sl_high = int(side.get("stop_loss_spot_high", 0))
            lines.append(f"###{exp_label} • 🟡 Sideways • P {p} • L {l} • SL{sl_low}/{sl_high}")
            lines.append(f"###Sell Nifty PE {side.get('sell_put_strike')} • ₹{side.get('sell_put_price')}")
            lines.append(f"NSE:{side.get('sell_put_tv')}")
            lines.append(f"###Sell Nifty CE {side.get('sell_call_strike')} • ₹{side.get('sell_call_price')}")
            lines.append(f"NSE:{side.get('sell_call_tv')}")
            lines.append(f"###Buy Nifty PE {side.get('buy_put_strike')} • ₹{side.get('buy_put_price')}")
            lines.append(f"NSE:{side.get('buy_put_tv')}")
            lines.append(f"###Buy Nifty CE {side.get('buy_call_strike')} • ₹{side.get('buy_call_price')}")
            lines.append(f"NSE:{side.get('buy_call_tv')}")

    return lines


def push_to_tradingview(
    session_id: str,
    session_sign: str = "",
    watchlist_name: str = "3NiftyV2",
    symbols: Optional[List[str]] = None,
    data_path: str | Path = "docs/data.json",
    domain: str = "in.tradingview.com"
) -> bool:
    if not session_id or not session_id.strip():
        print("⚠️ No TradingView session ID provided. Skipping push.")
        return False

    session_id = session_id.strip()
    session_sign = session_sign.strip()

    cookie = f"sessionid={session_id}"
    if session_sign:
        cookie += f"; sessionid_sign={session_sign}"

    headers = {
        "Cookie": cookie,
        "Origin": f"https://{domain}",
        "Referer": f"https://{domain}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json"
    }

    if symbols is None:
        symbols = build_tradingview_symbols(data_path)

    # 1. Fetch custom watchlists
    url = f"https://{domain}/api/v1/symbols_list/custom/"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"❌ TradingView auth failed (HTTP {resp.status_code}): {resp.text}")
        return False

    watchlists = resp.json()
    wl_id = None
    if isinstance(watchlists, list):
        for w in watchlists:
            if w.get("name") == watchlist_name:
                wl_id = str(w.get("id"))
                break

    # 2. Create watchlist if not present
    if not wl_id:
        create_url = f"https://{domain}/api/v1/symbols_list/custom/?unsafe=true"
        c_res = requests.post(create_url, headers=headers, json={"name": watchlist_name}, timeout=15)
        if c_res.status_code not in (200, 201):
            print(f"❌ Failed to create watchlist '{watchlist_name}' (HTTP {c_res.status_code}): {c_res.text}")
            return False
        wl_id = str(c_res.json().get("id") or c_res.json())

    # 3. Replace symbols
    replace_url = f"https://{domain}/api/v1/symbols_list/custom/{wl_id}/replace/?unsafe=true"
    r_res = requests.post(replace_url, headers=headers, json=symbols, timeout=15)
    if r_res.status_code == 200:
        print(f"✅ Successfully updated TradingView watchlist '{watchlist_name}' (ID: {wl_id}) with {len(symbols)} items!")
        return True
    else:
        print(f"❌ Failed to replace symbols in '{watchlist_name}' (HTTP {r_res.status_code}): {r_res.text}")
        return False
