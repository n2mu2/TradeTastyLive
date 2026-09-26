"""Data model shared by the data providers, strategy builders and the UI."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Optional


@dataclass
class Instrument:
    """One row of the Angel One scrip master."""

    token: str
    symbol: str
    name: str
    exchange: str
    instrument_type: str
    strike: float = 0.0
    expiry: Optional[date] = None
    lot_size: int = 1
    tick_size: float = 0.05
    option_type: str = ""  # CE / PE for options

    @property
    def is_option(self) -> bool:
        return self.instrument_type in ("OPTIDX", "OPTSTK")


@dataclass
class OptionQuote:
    strike: float
    option_type: str
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    oi: int = 0
    oi_change: int = 0
    iv: float = 0.0            # decimal, 0.14 = 14 %
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    token: str = ""
    symbol: str = ""

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0)

    @property
    def spread_pct(self) -> float:
        return (self.spread / self.mid * 100.0) if self.mid > 0 else 100.0

    @property
    def tradable(self) -> bool:
        return self.mid > 0


@dataclass
class ChainRow:
    strike: float
    ce: Optional[OptionQuote] = None
    pe: Optional[OptionQuote] = None

    def side(self, option_type: str) -> Optional[OptionQuote]:
        return self.ce if option_type.upper().startswith("C") else self.pe


@dataclass
class OptionChain:
    underlying: str
    spot: float
    expiry: date
    dte: int
    rows: list[ChainRow] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    lot_size: int = 1
    underlying_symbol: str = ""
    source: str = "replay"
    iv_history: list[float] = field(default_factory=list)  # ATM IV, oldest -> newest
    closes: list[float] = field(default_factory=list)      # daily closes for the underlying

    def strikes(self) -> list[float]:
        return [r.strike for r in self.rows]

    def row(self, strike: float) -> Optional[ChainRow]:
        for r in self.rows:
            if abs(r.strike - strike) < 1e-6:
                return r
        return None

    def nearest_row(self, strike: float) -> Optional[ChainRow]:
        if not self.rows:
            return None
        return min(self.rows, key=lambda r: abs(r.strike - strike))

    def quotes(self, option_type: str) -> list[OptionQuote]:
        out = []
        for r in self.rows:
            q = r.side(option_type)
            if q is not None and q.tradable:
                out.append(q)
        return out

    def strike_step(self) -> float:
        s = self.strikes()
        if len(s) < 2:
            return 0.0
        return min(abs(s[i + 1] - s[i]) for i in range(len(s) - 1))


@dataclass
class Leg:
    side: str              # BUY / SELL
    option_type: str       # CE / PE
    strike: float
    premium: float         # per unit of underlying
    lots: int = 1
    lot_size: int = 1
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    token: str = ""
    symbol: str = ""

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def signed_qty(self) -> int:
        return self.quantity if self.side.upper() == "BUY" else -self.quantity

    @property
    def cash(self) -> float:
        """Cash flow at entry: positive = money received."""
        sign = 1.0 if self.side.upper() == "SELL" else -1.0
        return sign * self.premium * self.quantity


@dataclass
class ExitRule:
    name: str
    description: str
    priority: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradePlan:
    strategy: str
    underlying: str
    expiry: date
    dte: int
    spot: float
    legs: list[Leg] = field(default_factory=list)
    lot_size: int = 1
    lots: int = 1
    net_credit: float = 0.0          # per unit; positive when you receive money
    max_profit: float = 0.0          # total, all lots, in currency
    max_loss: float = 0.0            # total, all lots, positive number
    breakevens: list[float] = field(default_factory=list)
    prob_short_otm: float = 0.0      # P(short legs expire worthless), 0-1
    prob_max_profit: float = 0.0     # P(expiry beyond breakeven), 0-1
    prob_touch_short: float = 0.0
    margin_estimate: float = 0.0
    capital_used: float = 0.0
    iv_rank: float = 0.0
    iv_percentile: float = 0.0
    iv: float = 0.0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    exit_rules: list[ExitRule] = field(default_factory=list)
    payoff: dict[str, list[float]] = field(default_factory=dict)
    greeks: dict[str, float] = field(default_factory=dict)

    @property
    def risk_reward(self) -> float:
        return (self.max_profit / self.max_loss) if self.max_loss > 0 else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        rr = self.risk_reward
        d["risk_reward"] = None if (math.isinf(rr) or math.isnan(rr)) else round(rr, 3)
        if math.isinf(self.max_profit) or math.isnan(self.max_profit):
            d["max_profit"] = None
        if math.isinf(self.max_loss) or math.isnan(self.max_loss):
            d["max_loss"] = None
        return d


@dataclass
class Signal:
    id: str
    ts: datetime
    kind: str                 # NIFTY_SPREAD / STOCK_LONG_OPTION
    direction: str            # bullish / bearish / neutral
    underlying: str
    headline: str
    plan: Optional[TradePlan] = None
    iv_context: dict = field(default_factory=dict)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat(timespec="seconds"),
            "kind": self.kind,
            "direction": self.direction,
            "underlying": self.underlying,
            "headline": self.headline,
            "score": round(self.score, 2),
            "reasons": self.reasons,
            "iv_context": self.iv_context,
            "data": self.data,
            "plan": self.plan.to_dict() if self.plan else None,
        }


@dataclass
class ScanResult:
    ts: datetime
    provider: str
    signals: list[Signal] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    scanned: int = 0
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(timespec="seconds"),
            "provider": self.provider,
            "scanned": self.scanned,
            "signals": [s.to_dict() for s in self.signals],
            "rejected": self.rejected,
            "errors": self.errors,
            "duration_s": round(self.duration_s, 2),
        }
