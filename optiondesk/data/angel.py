"""Angel One SmartAPI client + scrip master loader.

Verified against the live host (2026-09-26):

* ``POST /rest/auth/angelbroking/user/v1/loginByPassword``      -> AB1050 on bad creds
* ``POST /rest/secure/angelbroking/marketData/v1/optionGreek``  -> AG8001 without token
* ``POST /rest/secure/angelbroking/market/v1/quote/``           -> AG8001 without token
* ``POST /rest/secure/angelbroking/historical/v1/getCandleData``-> AG8001 without token
* ``GET  https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json``
  -> 141,925 instruments, public, no auth

The scrip master is cached on disk because it is ~33 MB.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import socket
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

from ..model import Instrument, OptionChain, OptionQuote, ChainRow
from .base import Candle, DataError, enrich_quotes

log = logging.getLogger(__name__)

API_ROOT = "https://apiconnect.angelone.in"
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_DIR = Path(os.environ.get("OPTIONDESK_CACHE", str(Path.home() / ".optiondesk")))
SCRIP_CACHE = CACHE_DIR / "scrip_master.json.gz"

EXPIRY_FMT = "%d%b%Y"
IDX_TOKENS = {"NIFTY": "99926000", "BANKNIFTY": "99926009", "FINNIFTY": "99926037",
              "MIDCPNIFTY": "99926074", "SENSEX": "99919000"}


def _public_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# --------------------------------------------------------------------------- #
# Scrip master
# --------------------------------------------------------------------------- #
class ScripMaster:
    """Cached Angel One instrument master."""

    def __init__(self, path: Path = SCRIP_CACHE, max_age_hours: float = 24.0,
                 offline: bool = False):
        self.path = Path(path)
        self.max_age = max_age_hours * 3600
        self.offline = offline
        self._rows: list[dict] = []
        self._loaded_at: float = 0.0

    # -- loading ---------------------------------------------------------- #
    def load(self, force: bool = False) -> "ScripMaster":
        fresh = self.path.exists() and (time.time() - self.path.stat().st_mtime) < self.max_age
        if fresh and not force:
            self._rows = self._read(self.path)
            self._loaded_at = self.path.stat().st_mtime
            return self
        if self.offline:
            if self.path.exists():
                self._rows = self._read(self.path)
                return self
            raise DataError("Scrip master not cached and offline mode is on")
        self.refresh()
        return self

    def refresh(self) -> "ScripMaster":
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        log.info("downloading scrip master from %s", SCRIP_MASTER_URL)
        resp = requests.get(SCRIP_MASTER_URL, timeout=120)
        resp.raise_for_status()
        rows = resp.json()
        tmp = self.path.with_suffix(".tmp.gz")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(rows, fh)
        tmp.replace(self.path)
        self._rows = rows
        self._loaded_at = time.time()
        log.info("scrip master cached: %d instruments", len(rows))
        return self

    @staticmethod
    def _read(path: Path) -> list[dict]:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
            return json.load(fh)

    # -- queries ---------------------------------------------------------- #
    @property
    def rows(self) -> list[dict]:
        if not self._rows:
            self.load()
        return self._rows

    def index_token(self, underlying: str) -> str:
        key = underlying.upper()
        if key in IDX_TOKENS:
            return IDX_TOKENS[key]
        for r in self.rows:
            if r.get("exch_seg") == "NSE" and r.get("instrumenttype") == "AMXIDX" \
                    and r.get("name", "").upper() == key:
                return r["token"]
        raise DataError(f"no index token found for {underlying}")

    def underlying_exchange(self, underlying: str) -> str:
        key = underlying.upper()
        if key in IDX_TOKENS:
            return "NSE"
        for r in self.rows:
            if r.get("exch_seg") == "NSE" and r.get("symbol", "").upper() == key:
                return "NSE"
        return "NSE"

    def option_rows(self, underlying: str, expiry: date) -> list[dict]:
        key = underlying.upper()
        stamp = expiry.strftime(EXPIRY_FMT).upper()
        return [r for r in self.rows
                if r.get("exch_seg") == "NFO"
                and r.get("instrumenttype") in ("OPTIDX", "OPTSTK")
                and r.get("name", "").upper() == key
                and (r.get("expiry") or "").upper() == stamp]

    def expiries(self, underlying: str, after: Optional[date] = None, limit: int = 6) -> list[date]:
        key = underlying.upper()
        after = after or date.today()
        seen: set[date] = set()
        for r in self.rows:
            if r.get("exch_seg") != "NFO" or r.get("name", "").upper() != key:
                continue
            if r.get("instrumenttype") not in ("OPTIDX", "OPTSTK"):
                continue
            raw = (r.get("expiry") or "").upper()
            if not raw:
                continue
            try:
                d = datetime.strptime(raw, EXPIRY_FMT).date()
            except ValueError:
                continue
            if d >= after:
                seen.add(d)
        return sorted(seen)[:limit]

    def stock_universe(self) -> list[str]:
        return sorted({r["name"] for r in self.rows
                       if r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "OPTSTK"})

    def lot_size(self, underlying: str, expiry: Optional[date] = None) -> int:
        rows = self.option_rows(underlying, expiry) if expiry else []
        if not rows:
            key = underlying.upper()
            rows = [r for r in self.rows if r.get("exch_seg") == "NFO"
                    and r.get("name", "").upper() == key]
        for r in rows:
            try:
                return int(float(r["lotsize"]))
            except (KeyError, ValueError):
                continue
        return 1

    def instruments(self, underlying: str, expiry: date) -> list[Instrument]:
        out = []
        for r in self.option_rows(underlying, expiry):
            out.append(self._to_instrument(r))
        return out

    @staticmethod
    def _to_instrument(r: dict) -> Instrument:
        exp = None
        raw = (r.get("expiry") or "").upper()
        if raw:
            try:
                exp = datetime.strptime(raw, EXPIRY_FMT).date()
            except ValueError:
                exp = None
        return Instrument(
            token=str(r.get("token", "")),
            symbol=r.get("symbol", ""),
            name=r.get("name", ""),
            exchange=r.get("exch_seg", "NFO"),
            instrument_type=r.get("instrumenttype", ""),
            strike=float(r.get("strike", 0) or 0) / 100.0,
            expiry=exp,
            lot_size=int(float(r.get("lotsize", 1) or 1)),
            tick_size=float(r.get("tick_size", 0.05) or 0.05),
            option_type=("CE" if r.get("symbol", "").endswith("CE")
                         else "PE" if r.get("symbol", "").endswith("PE") else ""),
        )


# --------------------------------------------------------------------------- #
# SmartAPI REST client
# --------------------------------------------------------------------------- #
class AngelClient:
    """Thin REST wrapper over SmartAPI. Session lasts a trading day."""

    def __init__(self, api_key: str, client_code: str, pin: str, totp_secret: str,
                 timeout: float = 20.0):
        self.api_key = api_key
        self.client_code = client_code
        self.pin = pin
        self.totp_secret = totp_secret
        self.timeout = timeout
        self.session = requests.Session()
        self.jwt: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        self.logged_in_at: float = 0.0
        self._ip = _public_ip()

    # -- auth ------------------------------------------------------------- #
    def _headers(self, auth: bool = True) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": self._ip,
            "X-ClientPublicIP": self._ip,
            "X-ClientPrivateIP": self._ip,
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self.api_key,
        }
        if auth and self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    @staticmethod
    def current_totp(secret: str) -> str:
        import pyotp

        return pyotp.TOTP(secret.replace(" ", "")).now()

    def login(self) -> dict:
        body = {
            "clientcode": self.client_code,
            "password": self.pin,
            "totp": self.current_totp(self.totp_secret),
        }
        resp = self.session.post(f"{API_ROOT}/rest/auth/angelbroking/user/v1/loginByPassword",
                                 json=body, headers=self._headers(auth=False), timeout=self.timeout)
        data = self._json(resp)
        if not data.get("status"):
            raise DataError(f"SmartAPI login failed: {data.get('errorcode')} {data.get('message')}")
        payload = data["data"]
        self.jwt = payload["jwtToken"]
        self.refresh_token = payload.get("refreshToken")
        self.feed_token = payload.get("feedToken")
        self.logged_in_at = time.time()
        log.info("SmartAPI session established for %s", self.client_code)
        return payload

    def ensure_session(self) -> None:
        # Angel tokens are valid for the trading day; re-login every 10 h to be safe.
        if not self.jwt or (time.time() - self.logged_in_at) > 10 * 3600:
            self.login()

    def _json(self, resp: requests.Response) -> dict:
        try:
            return resp.json()
        except ValueError as exc:  # WAF rejections come back as HTML
            raise DataError(f"SmartAPI returned non-JSON ({resp.status_code}): {resp.text[:160]}") from exc

    def post(self, path: str, payload: dict) -> dict:
        self.ensure_session()
        resp = self.session.post(f"{API_ROOT}{path}", json=payload,
                                 headers=self._headers(), timeout=self.timeout)
        data = self._json(resp)
        if isinstance(data, dict) and data.get("errorCode") in ("AG8001", "AB1004"):
            log.warning("token rejected (%s), re-authenticating", data.get("errorCode"))
            self.login()
            resp = self.session.post(f"{API_ROOT}{path}", json=payload,
                                     headers=self._headers(), timeout=self.timeout)
            data = self._json(resp)
        return data

    # -- market data ------------------------------------------------------ #
    def option_greek(self, name: str, expiry: date) -> list[dict]:
        payload = {"name": name.upper(), "expirydate": expiry.strftime(EXPIRY_FMT).upper()}
        data = self.post("/rest/secure/angelbroking/marketData/v1/optionGreek", payload)
        if not data.get("status"):
            raise DataError(f"optionGreek failed: {data.get('message')}")
        return data.get("data") or []

    def quote(self, exchange_tokens: dict[str, list[str]], mode: str = "FULL") -> list[dict]:
        payload = {"mode": mode, "exchangeTokens": exchange_tokens}
        data = self.post("/rest/secure/angelbroking/market/v1/quote/", payload)
        if not data.get("status"):
            raise DataError(f"quote failed: {data.get('message')}")
        return (data.get("data") or {}).get("fetched") or []

    def candles(self, exchange: str, token: str, interval: str,
                from_date: datetime, to_date: datetime) -> list[Candle]:
        payload = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        data = self.post("/rest/secure/angelbroking/historical/v1/getCandleData", payload)
        if not data.get("status"):
            raise DataError(f"getCandleData failed: {data.get('message')}")
        out: list[Candle] = []
        for row in data.get("data") or []:
            try:
                ts = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S%z")
            except (ValueError, IndexError):
                continue
            out.append(Candle(ts=ts, open=float(row[1]), high=float(row[2]),
                              low=float(row[3]), close=float(row[4]),
                              volume=int(row[5]) if len(row) > 5 else 0))
        return out

    def rms(self) -> dict:
        data = self.post("/rest/secure/angelbroking/user/v1/getRMS", {})
        return data.get("data") or {}

    def position(self) -> list[dict]:
        data = self.post("/rest/secure/angelbroking/order/v1/getBook", {})
        return data.get("data") or []

    def logout(self) -> None:
        if self.jwt:
            try:
                self.post("/rest/secure/angelbroking/user/v1/logout", {})
            except Exception as exc:  # pragma: no cover - best effort
                log.debug("logout failed: %s", exc)
            self.jwt = None

    def healthy(self) -> tuple[bool, str]:
        try:
            self.ensure_session()
            rms = self.rms()
            return True, f"logged in as {self.client_code}; available cash {rms.get('availablecash', '?')}"
        except Exception as exc:
            return False, str(exc)


# --------------------------------------------------------------------------- #
# Provider built on top of the two pieces above
# --------------------------------------------------------------------------- #
class AngelProvider:
    """Live Angel One market data -> :class:`OptionChain` objects."""

    name = "angel"

    def __init__(self, client: AngelClient, scrip: Optional[ScripMaster] = None,
                 r: float = 0.065, q: float = 0.0):
        self.client = client
        self.scrip = scrip or ScripMaster()
        self.r = r
        self.q = q
        self._close_cache: dict[tuple[str, int], tuple[float, list[float]]] = {}

    # -- DataProvider ----------------------------------------------------- #
    def spot(self, underlying: str) -> float:
        token = self.scrip.index_token(underlying.upper())
        rows = self.client.quote({"NSE": [token]}, mode="LTP")
        if not rows:
            raise DataError(f"no quote for {underlying} (token {token})")
        return float(rows[0].get("last_price") or rows[0].get("ltp") or 0.0)

    def expiries(self, underlying: str, limit: int = 4) -> list[date]:
        return self.scrip.expiries(underlying.upper(), limit=limit)

    def instruments(self, underlying: str, expiry: date) -> list[Instrument]:
        return self.scrip.instruments(underlying.upper(), expiry)

    def option_chain(self, underlying: str, expiry: date, around_atm: int = 12) -> OptionChain:
        spot = self.spot(underlying)
        lot_size = self.scrip.lot_size(underlying.upper(), expiry)
        rows_by_strike: dict[float, ChainRow] = {}
        greeks = self.client.option_greek(underlying.upper(), expiry)
        by_symbol = {g.get("symbol"): g for g in greeks}
        instruments = self.scrip.instruments(underlying.upper(), expiry)
        keep = {i.symbol: i for i in instruments}
        tokens = [i.token for i in instruments if i.symbol in by_symbol]
        quotes = {q.get("symbolToken"): q for q in self._chunked_quote(tokens)}

        for inst in instruments:
            g = by_symbol.get(inst.symbol)
            if not g:
                continue
            live = quotes.get(inst.token, {})
            oq = OptionQuote(
                strike=inst.strike,
                option_type=inst.option_type,
                ltp=float(live.get("last_price", 0) or 0),
                bid=self._best_depth(live, "buy"),
                ask=self._best_depth(live, "sell"),
                volume=int(float(live.get("volume", 0) or 0)),
                oi=int(float(live.get("open_interest", g.get("openInterest", 0)) or 0)),
                oi_change=int(float(g.get("changeInOpenInterest", 0) or 0)),
                iv=float(g.get("impliedVolatility", 0) or 0) / 100.0,
                delta=float(g.get("delta", 0) or 0),
                gamma=float(g.get("gamma", 0) or 0),
                theta=float(g.get("theta", 0) or 0),
                vega=float(g.get("vega", 0) or 0),
                token=inst.token,
                symbol=inst.symbol,
            )
            if q := (oq.ltp or oq.mid):
                oq.ltp = oq.ltp or q
            row = rows_by_strike.setdefault(inst.strike, ChainRow(strike=inst.strike))
            if inst.option_type == "CE":
                row.ce = oq
            else:
                row.pe = oq

        rows = sorted(rows_by_strike.values(), key=lambda r: r.strike)
        rows = _window_around_atm(rows, spot, around_atm)
        chain = OptionChain(
            underlying=underlying.upper(),
            underlying_symbol=underlying.upper(),
            spot=spot,
            expiry=expiry,
            dte=(expiry - date.today()).days,
            rows=rows,
            lot_size=lot_size,
            source=self.name,
            closes=self.daily_closes(underlying, 400),
        )
        return enrich_quotes(chain, self.r, self.q)

    def daily_closes(self, underlying: str, days: int = 400) -> list[float]:
        key = (underlying.upper(), days)
        cached = self._close_cache.get(key)
        if cached and (time.time() - cached[0]) < 15 * 60:
            return cached[1]
        token = self.scrip.index_token(underlying.upper())
        exchange = "NSE" if underlying.upper() in IDX_TOKENS else "NSE"
        now = datetime.now()
        candles = self.client.candles(exchange, token, "ONE_DAY",
                                      now - timedelta(days=int(days * 1.5)), now)
        closes = [c.close for c in candles]
        self._close_cache[key] = (time.time(), closes)
        return closes

    def candles(self, underlying: str, interval: str = "ONE_DAY",
                days: int = 60) -> list[Candle]:
        token = self.scrip.index_token(underlying.upper())
        now = datetime.now()
        return self.client.candles("NSE", token, interval,
                                   now - timedelta(days=days), now)

    def universe(self) -> list[str]:
        return self.scrip.stock_universe()

    def healthy(self) -> tuple[bool, str]:
        return self.client.healthy()

    # -- helpers ---------------------------------------------------------- #
    def _chunked_quote(self, tokens: list[str], size: int = 50) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(tokens), size):
            chunk = tokens[i:i + size]
            try:
                out.extend(self.client.quote({"NFO": chunk}, mode="FULL"))
            except DataError as exc:
                log.warning("quote chunk failed: %s", exc)
        return out

    @staticmethod
    def _best_depth(live: dict, side: str) -> float:
        depth = (live.get("depth") or {}).get(side) or []
        if not depth:
            return 0.0
        prices = [float(d.get("price", 0) or 0) for d in depth if float(d.get("price", 0) or 0) > 0]
        if not prices:
            return 0.0
        return min(prices) if side == "buy" else max(prices)


def _window_around_atm(rows: list[ChainRow], spot: float, around_atm: int) -> list[ChainRow]:
    if around_atm <= 0 or len(rows) <= 2 * around_atm + 1:
        return rows
    idx = min(range(len(rows)), key=lambda i: abs(rows[i].strike - spot))
    lo = max(0, idx - around_atm)
    hi = min(len(rows), idx + around_atm + 1)
    return rows[lo:hi]
