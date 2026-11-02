from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class Instrument:
    symbol: str
    primary_exchange: str
    currency: str = "USD"

    def __post_init__(self) -> None:
        for value in (self.symbol, self.primary_exchange, self.currency):
            if not re.fullmatch(r"[A-Z0-9.\-]+", value):
                raise ValueError(f"Invalid instrument identifier: {value!r}")
        if self.currency != "USD":
            raise ValueError("The first scanner supports USD stocks only")

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.currency}:{self.primary_exchange}"


@dataclass(frozen=True)
class DailyBar:
    instrument_key: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def validate(self) -> None:
        prices = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(p) and p > 0 for p in prices):
            raise ValueError(f"Nonpositive/nonfinite OHLC at {self.session}")
        if not math.isfinite(self.volume) or self.volume < 0:
            raise ValueError(f"Invalid volume at {self.session}")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError(f"Inconsistent OHLC at {self.session}")
        if self.low > self.high:
            raise ValueError(f"Low exceeds high at {self.session}")


def aware_timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset, e.g. 2026-10-30T22:00:00Z")
    return result


def read_universe(path: Path) -> list[Instrument]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"symbol", "primary_exchange"}.issubset(reader.fieldnames or []):
            raise ValueError("Universe CSV requires symbol,primary_exchange headers")
        instruments = [
            Instrument(row["symbol"].strip().upper(), row["primary_exchange"].strip().upper())
            for row in reader
        ]
    if not instruments:
        raise ValueError("Universe is empty")
    keys = [item.key for item in instruments]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate universe instruments")
    if len({item.symbol for item in instruments}) != len(instruments):
        raise ValueError("Use one listing per symbol in this initial USD universe")
    return instruments
