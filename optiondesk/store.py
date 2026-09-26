"""SQLite persistence for signals (history + alert de-duplication)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .model import ScanResult, Signal

DEFAULT_DB = Path(os.environ.get("OPTIONDESK_DB",
                                 str(Path(__file__).resolve().parent.parent / "signals.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          TEXT NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    underlying  TEXT NOT NULL,
    direction   TEXT,
    score       REAL,
    headline    TEXT,
    alerted     INTEGER DEFAULT 0,
    payload     TEXT,
    PRIMARY KEY (id, ts)
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_underlying ON signals(underlying, ts DESC);

CREATE TABLE IF NOT EXISTS scans (
    ts          TEXT PRIMARY KEY,
    provider    TEXT,
    scanned     INTEGER,
    n_signals   INTEGER,
    duration_s  REAL,
    errors      TEXT
);
"""


def _sanitize_floats(obj):
    if isinstance(obj, float):
        import math
        return None if (math.isinf(obj) or math.isnan(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    return obj


class SignalStore:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as cx:
            cx.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        cx = sqlite3.connect(self.path, timeout=10)
        cx.row_factory = sqlite3.Row
        return cx

    # -- writes ----------------------------------------------------------- #
    def save_scan(self, result: ScanResult) -> int:
        with self._lock, self._connect() as cx:
            cx.execute(
                "INSERT OR REPLACE INTO scans (ts, provider, scanned, n_signals, duration_s, errors)"
                " VALUES (?,?,?,?,?,?)",
                (result.ts.isoformat(timespec="seconds"), result.provider, result.scanned,
                 len(result.signals), result.duration_s, json.dumps(result.errors)))
            for sig in result.signals:
                cx.execute(
                    "INSERT OR REPLACE INTO signals (id, ts, kind, underlying, direction, score,"
                    " headline, alerted, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sig.id, sig.ts.isoformat(timespec="seconds"), sig.kind, sig.underlying,
                     sig.direction, sig.score, sig.headline, 0, json.dumps(sig.to_dict())))
        return len(result.signals)

    def mark_alerted(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock, self._connect() as cx:
            cx.executemany("UPDATE signals SET alerted = 1 WHERE id = ? AND alerted = 0",
                           [(i,) for i in ids])

    # -- reads ------------------------------------------------------------ #
    def recent(self, limit: int = 40, kind: Optional[str] = None,
               underlying: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM signals"
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if underlying:
            clauses.append("underlying = ?")
            params.append(underlying.upper())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC, score DESC LIMIT ?"
        params.append(limit)
        with self._connect() as cx:
            rows = cx.execute(sql, params).fetchall()
        out = []
        for row in rows:
            try:
                out.append(_sanitize_floats(json.loads(row["payload"])))
            except (json.JSONDecodeError, TypeError):
                out.append({"id": row["id"], "headline": row["headline"], "score": row["score"]})
        return out

    def has_recent(self, signal_id: str, minutes: int) -> bool:
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with self._connect() as cx:
            row = cx.execute("SELECT 1 FROM signals WHERE id = ? AND ts >= ? AND alerted = 1 LIMIT 1",
                             (signal_id, cutoff)).fetchone()
        return row is not None

    def last_scan(self) -> Optional[dict]:
        with self._connect() as cx:
            row = cx.execute("SELECT * FROM scans ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        with self._connect() as cx:
            total = cx.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
            alerted = cx.execute("SELECT COUNT(*) c FROM signals WHERE alerted = 1").fetchone()["c"]
            scans = cx.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"]
            by_kind = {r["kind"]: r["c"] for r in cx.execute(
                "SELECT kind, COUNT(*) c FROM signals GROUP BY kind")}
        return {"signals": total, "alerted": alerted, "scans": scans, "by_kind": by_kind}
