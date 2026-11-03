"""Transactional per-instrument history checkpoints using standard SQLite."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path

from ib_lab.models import DailyBar, Instrument


class HistoryCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS history (
            instrument_key TEXT PRIMARY KEY, con_id INTEGER NOT NULL,
            full_refresh TEXT NOT NULL, payload TEXT NOT NULL)""")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, instrument: Instrument):
        row = self.db.execute(
            "SELECT con_id, full_refresh, payload FROM history WHERE instrument_key=?",
            (instrument.key,),
        ).fetchone()
        if row is None:
            return None
        bars = [
            DailyBar(**{**b, "session": date.fromisoformat(b["session"])})
            for b in json.loads(row[2])
        ]
        return row[0], date.fromisoformat(row[1]), bars

    def put(self, instrument: Instrument, con_id: int, refreshed: date, bars: list[DailyBar]):
        if con_id <= 0 or not bars:
            raise ValueError("Cannot checkpoint unresolved or empty history")
        if len({b.session for b in bars}) != len(bars):
            raise ValueError("Duplicate cache sessions")
        for bar in bars:
            bar.validate()
            if bar.instrument_key != instrument.key:
                raise ValueError("Cache instrument mismatch")
        payload = json.dumps([asdict(b) for b in bars], default=str, allow_nan=False)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO history VALUES (?, ?, ?, ?)",
                (instrument.key, con_id, refreshed.isoformat(), payload),
            )
