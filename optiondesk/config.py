"""Configuration: YAML on disk, sane defaults in code, env overrides for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class MarketCfg:
    provider: str = "replay"                 # replay | angel
    risk_free_rate: float = 0.065
    index_dividend_yield: float = 0.012
    stock_dividend_yield: float = 0.008
    index_underlying: str = "NIFTY"
    stock_universe: list[str] = field(default_factory=list)
    angel_api_key: str = ""
    angel_client_code: str = ""
    angel_pin: str = ""
    angel_totp_secret: str = ""
    scrip_cache_path: str = ""


@dataclass
class AccountCfg:
    capital: float = 500_000.0
    max_risk_per_trade_pct: float = 2.0
    max_open_trades: int = 5
    max_lots_per_trade: int = 10


@dataclass
class NiftySpreadCfg:
    enabled: bool = True
    dte_min: int = 3
    dte_max: int = 45
    expiry_index: int = 0                    # 0 = front (weekly for NIFTY)
    min_iv_rank: float = 25.0
    short_delta: float = 0.16
    wing_delta: float = 0.05
    wing_width_points: float = 250.0
    min_pop: float = 0.68
    min_credit_pct_of_width: float = 0.30
    max_spread_pct: float = 8.0
    min_oi: int = 500
    min_premium: float = 0.5
    take_profit_pct: float = 0.50
    stop_loss_multiple: float = 2.0
    roll_dte: int = 2
    allow_debit_spreads: bool = True
    debit_iv_rank_max: float = 30.0
    debit_min_pop: float = 0.45
    strategy_bias: str = "auto"              # auto | iron_condor | credit_spread | debit_spread


@dataclass
class StockOptionCfg:
    enabled: bool = True
    expiry_index: int = 1                    # 0 = current month, 1 = next month
    dte_min: int = 20
    dte_max: int = 55
    max_iv_rank: float = 32.0
    delta_min: float = 0.55
    delta_max: float = 0.78
    trend_fast: int = 20
    trend_slow: int = 50
    rsi_window: int = 14
    rsi_long_min: float = 50.0
    rsi_long_max: float = 74.0
    rsi_short_min: float = 26.0
    rsi_short_max: float = 50.0
    adx_min: float = 18.0
    momentum_min_pct: float = 3.0
    max_premium_pct_of_spot: float = 9.0
    max_spread_pct: float = 6.0
    min_oi: int = 1_000
    take_profit_pct: float = 0.50
    stop_loss_pct: float = 0.40
    prefer_debit_spread_above_iv_rank: float = 22.0
    max_universe: int = 25


@dataclass
class AlertsCfg:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    min_score: float = 55.0
    only_market_hours: bool = True
    max_alerts_per_scan: int = 6
    dedupe_minutes: int = 45


@dataclass
class SchedulerCfg:
    interval_seconds: int = 120
    market_open: str = "09:15"
    market_close: str = "15:30"
    timezone: str = "Asia/Kolkata"
    scan_outside_hours: bool = True


@dataclass
class Config:
    market: MarketCfg = field(default_factory=MarketCfg)
    account: AccountCfg = field(default_factory=AccountCfg)
    nifty_spreads: NiftySpreadCfg = field(default_factory=NiftySpreadCfg)
    stock_options: StockOptionCfg = field(default_factory=StockOptionCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    scheduler: SchedulerCfg = field(default_factory=SchedulerCfg)

    # -- io --------------------------------------------------------------- #
    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Config":
        path = Path(path) if path else DEFAULT_PATH
        raw: dict[str, Any] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
        cfg = cls(
            market=MarketCfg(**raw.get("market", {})),
            account=AccountCfg(**raw.get("account", {})),
            nifty_spreads=NiftySpreadCfg(**raw.get("nifty_spreads", {})),
            stock_options=StockOptionCfg(**raw.get("stock_options", {})),
            alerts=AlertsCfg(**raw.get("alerts", {})),
            scheduler=SchedulerCfg(**raw.get("scheduler", {})),
        )
        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        """Secrets come from the environment, never from a committed file."""
        env = {
            "market.angel_api_key": "ANGEL_API_KEY",
            "market.angel_client_code": "ANGEL_CLIENT_CODE",
            "market.angel_pin": "ANGEL_PIN",
            "market.angel_totp_secret": "ANGEL_TOTP_SECRET",
            "market.provider": "OPTIONDESK_PROVIDER",
            "alerts.telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "alerts.telegram_chat_id": "TELEGRAM_CHAT_ID",
            "account.capital": "OPTIONDESK_CAPITAL",
        }
        for dotted, var in env.items():
            value = os.environ.get(var)
            if value in (None, ""):
                continue
            section, key = dotted.split(".")
            obj = getattr(self, section)
            current = getattr(obj, key)
            setattr(obj, key, type(current)(value) if isinstance(current, (int, float)) else value)
        # auto-switch to live data when credentials are present
        if self.market.provider == "replay" and all(
                [self.market.angel_api_key, self.market.angel_client_code,
                 self.market.angel_pin, self.market.angel_totp_secret]):
            self.market.provider = "angel"

    def save(self, path: Optional[str | Path] = None) -> Path:
        path = Path(path) if path else DEFAULT_PATH
        path.write_text(yaml.safe_dump(asdict(self), sort_keys=False, default_flow_style=False))
        return path

    def public_dict(self) -> dict:
        """Config without secrets -- safe to expose through the dashboard."""
        d = asdict(self)
        m = d.get("market", {})
        for k in ("angel_api_key", "angel_pin", "angel_totp_secret"):
            if m.get(k):
                m[k] = "***set***"
        m["angel_client_code"] = (m.get("angel_client_code") or "")[:3] + "***"
        a = d.get("alerts", {})
        if a.get("telegram_bot_token"):
            a["telegram_bot_token"] = "***set***"
        return d


def sample_yaml() -> str:
    return yaml.safe_dump(asdict(Config()), sort_keys=False, default_flow_style=False)
