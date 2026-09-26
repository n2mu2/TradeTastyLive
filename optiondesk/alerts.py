"""Alert formatting + Telegram delivery.

Telegram is the default channel because it works from a phone with zero setup:
create a bot with @BotFather, grab the token, get your chat id, put both in the
config or in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Without a token configured
the dispatcher prints to the console instead, so nothing is ever silently dropped.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

import requests

from .config import AlertsCfg
from .model import Signal, TradePlan
from .store import SignalStore

log = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org"
MAX_LEN = 4000


def _money(value: float) -> str:
    if value == float("inf"):
        return "unlimited"
    return f"{value:,.0f}"


def format_plan(plan: TradePlan) -> str:
    legs = "\n".join(
        f"  {'SELL' if l.side == 'SELL' else 'BUY '} {l.option_type} {l.strike:>9,.0f}"
        f"  @ {l.premium:>8.2f}  x{l.lots} lot ({l.quantity:,} qty)"
        for l in plan.legs)
    lines = [
        legs,
        f"  credit/net cost: {plan.net_credit:+.2f} per unit",
        f"  max profit {_money(plan.max_profit)} | max loss {_money(plan.max_loss)}"
        f" | R:R {plan.risk_reward:.2f}",
        f"  P(short expires OTM) {plan.prob_short_otm:.0%}"
        f" | P(max profit) {plan.prob_max_profit:.0%}"
        f" | P(touch short) {plan.prob_touch_short:.0%}",
    ]
    if plan.breakevens:
        lines.append("  breakevens: " + " / ".join(f"{b:,.1f}" for b in plan.breakevens))
    if plan.margin_estimate:
        lines.append(f"  margin est. {plan.margin_estimate:,.0f} (verify with broker RMS)")
    return "\n".join(lines)


def format_signal(sig: Signal, include_reasons: bool = True) -> str:
    icon = {"NIFTY_SPREAD": "🧭", "STOCK_LONG_OPTION": "📈"}.get(sig.kind, "•")
    dirn = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(sig.direction, "")
    head = [f"{icon} {dirn} {sig.headline}", f"Score {sig.score:.0f}/100"]
    if sig.plan:
        head.append(format_plan(sig.plan))
    if sig.plan and sig.plan.exit_rules:
        head.append("Exits:\n" + "\n".join(f"  • {r.description}" for r in sig.plan.exit_rules))
    if include_reasons and sig.reasons:
        head.append("Why:\n" + "\n".join(f"  - {r}" for r in sig.reasons[:6]))
    if sig.plan and sig.plan.warnings:
        head.append("⚠ " + " | ".join(sig.plan.warnings))
    text = "\n".join(head)
    return text[:MAX_LEN]


def format_digest(signals: Sequence[Signal], ts: Optional[datetime] = None) -> str:
    ts = ts or datetime.now()
    if not signals:
        return f"OptionDesk scan {ts:%H:%M} — no setup met the filters."
    body = "\n\n".join(format_signal(s) for s in signals)
    return f"🗓 OptionDesk {ts:%d %b %H:%M}\n{len(signals)} setup(s)\n\n{body}"[:MAX_LEN]


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 15.0) -> dict:
    if not token or not chat_id:
        raise ValueError("telegram token/chat_id not configured")
    resp = requests.post(f"{TELEGRAM_API}/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                         timeout=timeout)
    data = resp.json() if resp.content else {"ok": False, "description": resp.text}
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data.get('description')}")
    return data


class AlertDispatcher:
    """Score filter + de-duplication + delivery."""

    def __init__(self, cfg: AlertsCfg, store: Optional[SignalStore] = None,
                 dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.dry_run = dry_run

    @property
    def configured(self) -> bool:
        return bool(self.cfg.telegram_bot_token and self.cfg.telegram_chat_id)

    def select(self, signals: Sequence[Signal]) -> list[Signal]:
        fresh: list[Signal] = []
        for sig in signals:
            if sig.score < self.cfg.min_score:
                continue
            if self.store and self.cfg.dedupe_minutes > 0 \
                    and self.store.has_recent(sig.id, self.cfg.dedupe_minutes):
                continue
            fresh.append(sig)
            if len(fresh) >= self.cfg.max_alerts_per_scan:
                break
        return fresh

    def dispatch(self, signals: Sequence[Signal]) -> list[str]:
        """Return the ids actually alerted."""
        chosen = self.select(signals)
        if not chosen:
            return []
        text = format_digest(chosen)
        ids = [s.id for s in chosen]
        if self.dry_run or not self.configured:
            log.info("telegram not configured -- printing %d alert(s) to console", len(chosen))
            print("\n" + "=" * 72)
            print(text)
            print("=" * 72 + "\n")
        else:
            try:
                send_telegram(self.cfg.telegram_bot_token, self.cfg.telegram_chat_id, text)
                log.info("telegram alert sent for %s", ids)
            except Exception as exc:
                log.error("telegram send failed: %s", exc)
                print("\n[telegram failed: %s]\n%s\n" % (exc, text))
        if self.store:
            self.store.mark_alerted(ids)
        return ids
