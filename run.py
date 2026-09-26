#!/usr/bin/env python3
"""OptionDesk CLI runner.

Commands:
  serve       Start the live FastAPI dashboard and scanning daemon
  scan        Run a one-shot scan and print top trade setups
  check       Verify Angel One API connection / session / credentials
  rules       Print the mechanical trading rules enforced by the engine
  test-alert  Send a sample Telegram alert to verify configuration
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Add optiondesk root to sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from optiondesk.alerts import AlertDispatcher, format_signal
from optiondesk.config import Config
from optiondesk.signals.engine import Engine, build_provider
from optiondesk.store import SignalStore

TZ = ZoneInfo("Asia/Kolkata")


def cmd_serve(args):
    import uvicorn
    from optiondesk.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = create_app(config_path=args.config, autostart=not args.no_daemon)
    print(f"Starting OptionDesk on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def cmd_scan(args):
    cfg = Config.load(args.config)
    if args.provider:
        cfg.market.provider = args.provider
    provider = build_provider(cfg)
    engine = Engine(provider, cfg)
    store = SignalStore()
    dispatcher = AlertDispatcher(cfg.alerts, store, dry_run=args.no_alert)

    print(f"Running OptionDesk scan (provider={provider.name})...")
    result = engine.scan()
    store.save_scan(result)

    print(f"\nScanned {result.scanned} instruments in {result.duration_s:.2f}s")
    print(f"Found {len(result.signals)} trade setup(s):\n")

    if not result.signals:
        print("No setups passed all filters. Filters are working as intended.")
        if result.rejected:
            print("\nSample rejected items:")
            for r in result.rejected[:5]:
                print(f"  • {r.get('underlying')}: {r.get('why')}")
        return

    for idx, sig in enumerate(result.signals, 1):
        print(f"--- [#{idx}] Score: {sig.score:.0f}/100 ---")
        print(format_signal(sig))
        print()

    if not args.no_alert and dispatcher.configured:
        sent_ids = dispatcher.dispatch(result.signals)
        print(f"Dispatched {len(sent_ids)} alert(s) to Telegram.")


def cmd_check(args):
    cfg = Config.load(args.config)
    print("=== Configuration Check ===")
    print(f"Configured provider: {cfg.market.provider}")
    print(f"Capital: ₹{cfg.account.capital:,.0f} | Max risk per trade: {cfg.account.max_risk_per_trade_pct}%")
    print(f"Angel Client Code: {cfg.market.angel_client_code or 'NOT SET'}")
    print(f"Angel API Key: {'SET' if cfg.market.angel_api_key else 'NOT SET'}")
    print(f"Angel TOTP Secret: {'SET' if cfg.market.angel_totp_secret else 'NOT SET'}")
    print(f"Telegram Bot Token: {'SET' if cfg.alerts.telegram_bot_token else 'NOT SET'}")
    print(f"Telegram Chat ID: {cfg.alerts.telegram_chat_id or 'NOT SET'}")

    try:
        provider = build_provider(cfg)
        ok, detail = provider.healthy()
        status_sym = "✅" if ok else "❌"
        print(f"\nProvider connection {status_sym}: {detail}")
    except Exception as exc:
        print(f"\nProvider initialization error: {exc}")


def cmd_rules(args):
    cfg = Config.load(args.config)
    n = cfg.nifty_spreads
    s = cfg.stock_options

    print("=================================================================")
    print("           OPTIONDESK MECHANICAL TRADING RULES                   ")
    print("=================================================================")
    print("\n1. NIFTY HEDGED SPREADS (Selling Rich Volatility):")
    print(f"  • Sell premium ONLY when IV Rank >= {n.min_iv_rank}")
    print(f"  • Short strike selection: ~{n.short_delta} Delta (approx 84% OTM probability)")
    print(f"  • Wing protection: ~{n.wing_delta} Delta or {n.wing_width_points:.0f} points (whichever is wider)")
    print(f"  • Expiry window: {n.dte_min} to {n.dte_max} DTE")
    print(f"  • Min Probability of Profit (POP): >= {n.min_pop:.0%}")
    print(f"  • Min Credit / Width: >= {n.min_credit_pct_of_width:.0%}")
    print(f"  • Exit at {n.take_profit_pct:.0%} max profit (tastylive core rule)")
    print(f"  • Stop loss at {n.stop_loss_multiple}x credit received")
    print(f"  • Roll or close at {n.roll_dte} DTE to avoid explosive expiration gamma")

    print("\n2. NEXT-MONTH STOCK OPTION BUYING (Buying Cheap Volatility):")
    print(f"  • Buy premium ONLY when IV Rank <= {s.max_iv_rank}")
    print(f"  • Expiry: Next-month contract ({s.dte_min} to {s.dte_max} DTE)")
    print(f"  • Moneyness: {s.delta_min} to {s.delta_max} Delta (ITM/near-ATM, not high-theta lotteries)")
    print(f"  • Technical trend filter: Price > SMA20 > SMA50 (Calls) or Price < SMA20 < SMA50 (Puts)")
    print(f"  • Momentum confirmation: ADX >= {s.adx_min} or Momentum >= {s.momentum_min_pct}%")
    print(f"  • Auto-switch to Debit Spread when IV Rank >= {s.prefer_debit_spread_above_iv_rank}")
    print(f"  • Exit at {s.take_profit_pct:.0%} profit, stop at {s.stop_loss_pct:.0%} loss, exit with 7-10 DTE left")

    print("\n3. CAPITAL & RISK MANAGEMENT:")
    print(f"  • Account Capital: ₹{cfg.account.capital:,.0f}")
    print(f"  • Risk per trade: {cfg.account.max_risk_per_trade_pct}% (₹{cfg.account.capital * cfg.account.max_risk_per_trade_pct / 100:,.0f})")
    print(f"  • Max open positions: {cfg.account.max_open_trades}")
    print("=================================================================\n")


def cmd_export_aptrade(args):
    cfg = Config.load(args.config)
    from optiondesk.export_aptrade import export_to_file

    out_file = export_to_file(args.out)
    print(f"✅ APTrade options dataset exported successfully to {out_file}")


def cmd_test_alert(args):
    cfg = Config.load(args.config)
    from optiondesk.alerts import send_telegram

    if not cfg.alerts.telegram_bot_token or not cfg.alerts.telegram_chat_id:
        print("Telegram bot token and chat ID must be set in config.yaml or env vars.")
        sys.exit(1)

    text = f"✅ OptionDesk Alert Test\nTime: {datetime.now(TZ):%Y-%m-%d %H:%M:%S} IST\nTelegram notifications are functioning!"
    try:
        send_telegram(cfg.alerts.telegram_bot_token, cfg.alerts.telegram_chat_id, text)
        print("Test alert sent successfully to Telegram!")
    except Exception as exc:
        print(f"Failed to send alert: {exc}")


def main():
    parser = argparse.ArgumentParser(description="OptionDesk Options Trading System")
    parser.add_argument("--config", "-c", default=None, help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="cmd")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start web dashboard & background scanner")
    p_serve.add_argument("--host", default="0.0.0.0", help="Host to bind (default 0.0.0.0)")
    p_serve.add_argument("--port", "-p", type=int, default=8080, help="Port to listen (default 8080)")
    p_serve.add_argument("--no-daemon", action="store_true", help="Disable background scan loop")

    # scan
    p_scan = subparsers.add_parser("scan", help="Run a manual one-shot scan")
    p_scan.add_argument("--provider", choices=["replay", "angel"], help="Override provider")
    p_scan.add_argument("--no-alert", action="store_true", help="Do not dispatch Telegram alerts")

    # check
    subparsers.add_parser("check", help="Check broker & API settings")

    # rules
    subparsers.add_parser("rules", help="Display mechanical strategy rules")

    # export-aptrade
    p_exp = subparsers.add_parser("export-aptrade", help="Export options dataset for APTrade Android app")
    p_exp.add_argument("--out", default="docs/data.json", help="Path to write data.json (default docs/data.json)")

    # test-alert
    subparsers.add_parser("test-alert", help="Send test alert to Telegram")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "serve": cmd_serve,
        "scan": cmd_scan,
        "check": cmd_check,
        "rules": cmd_rules,
        "export-aptrade": cmd_export_aptrade,
        "test-alert": cmd_test_alert,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
