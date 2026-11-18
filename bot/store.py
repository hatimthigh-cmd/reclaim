"""SQLite persistence for setups/trades and sent-alert de-duplication."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass, fields
from typing import Optional

# Setup lifecycle:
#   pending  -> limit order is live at the 1h EMA200 (paused flag = stop currently too wide)
#   open     -> filled, waiting for SL / TP / time exit
#   closed   -> exit_reason in TP / SL / TIME
#   expired  -> no fill within ORDER_VALID_H
#   skipped  -> price touched the level while the stop was too wide (backtest drops these)


@dataclass
class Setup:
    id: Optional[int] = None
    symbol: str = ""
    status: str = "pending"
    trigger_t: int = 0          # open time of the trigger candle (ms)
    li_t: int = 0               # open time of the flush candle
    high: float = 0.0
    flush_low: float = 0.0
    pump: float = 0.0
    dd: float = 0.0
    flush_x: float = 0.0
    level: float = 0.0          # current limit price (EMA200 of the last closed candle)
    level_atr: float = 0.0      # ATR of the last closed candle (sets the SL at fill)
    paused: int = 0
    expires_t: int = 0          # pending until this candle open time is closed
    fill_t: int = 0             # open time of the candle the fill happened in
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    risk: float = 0.0
    exit_t: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    r_net: float = 0.0
    created: int = 0
    updated: int = 0


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        cols = ", ".join(
            f"{f.name} {'INTEGER PRIMARY KEY AUTOINCREMENT' if f.name == 'id' else 'TEXT' if f.type in ('str', str) else 'REAL'}"
            for f in fields(Setup)
        )
        self.db.execute(f"CREATE TABLE IF NOT EXISTS setups ({cols})")
        self.db.execute("CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, t INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    # ----- setups -----
    def _row(self, r) -> Setup:
        d = dict(r)
        s = Setup()
        for f in fields(Setup):
            v = d.get(f.name)
            if v is None:
                continue
            if f.name in ("id", "trigger_t", "li_t", "paused", "expires_t", "fill_t", "exit_t", "created", "updated"):
                v = int(v)
            elif f.name in ("symbol", "status", "exit_reason"):
                v = str(v)
            else:
                v = float(v)
            setattr(s, f.name, v)
        return s

    def save(self, s: Setup) -> Setup:
        now = int(time.time())
        s.updated = now
        if not s.created:
            s.created = now
        d = asdict(s)
        if s.id is None:
            d.pop("id")
            keys = ", ".join(d)
            q = ", ".join("?" for _ in d)
            cur = self.db.execute(f"INSERT INTO setups ({keys}) VALUES ({q})", list(d.values()))
            s.id = cur.lastrowid
        else:
            sid = d.pop("id")
            sets = ", ".join(f"{k}=?" for k in d)
            self.db.execute(f"UPDATE setups SET {sets} WHERE id=?", list(d.values()) + [sid])
        self.db.commit()
        return s

    def by_status(self, *statuses: str) -> list[Setup]:
        q = ",".join("?" for _ in statuses)
        return [self._row(r) for r in self.db.execute(f"SELECT * FROM setups WHERE status IN ({q}) ORDER BY trigger_t", statuses)]

    def active_for(self, symbol: str) -> Optional[Setup]:
        r = self.db.execute("SELECT * FROM setups WHERE symbol=? AND status IN ('pending','open') ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        return self._row(r) if r else None

    def closed(self, since_ms: int = 0) -> list[Setup]:
        return [self._row(r) for r in self.db.execute(
            "SELECT * FROM setups WHERE status='closed' AND exit_t>=? ORDER BY exit_t", (since_ms,))]

    # ----- alert de-duplication -----
    def once(self, key: str) -> bool:
        """True the first time a key is seen (so the alert should be sent)."""
        try:
            self.db.execute("INSERT INTO alerts (key, t) VALUES (?, ?)", (key, int(time.time())))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def prune_alerts(self, older_than_days: int = 30) -> None:
        self.db.execute("DELETE FROM alerts WHERE t < ?", (int(time.time()) - older_than_days * 86400,))
        self.db.commit()

    def get_kv(self, k: str, default: str = "") -> str:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_kv(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, v))
        self.db.commit()
