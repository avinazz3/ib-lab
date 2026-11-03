"""Dated, auditable Nasdaq Trader listing discovery (not historical membership)."""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from ib_lab.models import Instrument
from ib_lab.store import sha256, write_json

SOURCES = {
    "nasdaqlisted": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "otherlisted": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
EXCHANGES = {"N": "NYSE", "A": "AMEX", "P": "ARCA", "Z": "BATS", "V": "IEX"}
COMMON = re.compile(r"\b(common (stock|shares?)|ordinary shares?)\b", re.I)
EXCLUDED = re.compile(
    r"\b(preferred|preference|depositary|depository|warrants?|rights?|units?|"
    r"notes?|bonds?|debentures?|fund|trust units?|ETF|ETN)\b",
    re.I,
)


def parse_directory(text: str, source: str) -> tuple[list[Instrument], list[dict]]:
    lines = text.splitlines()
    if not lines or not lines[-1].startswith("File Creation Time:"):
        raise ValueError(f"Truncated/missing footer in {source} directory")
    reader = csv.DictReader(lines[:-1], delimiter="|")
    symbol_field = "Symbol" if source == "nasdaqlisted" else "ACT Symbol"
    required = {symbol_field, "Security Name", "Test Issue", "ETF"}
    required.add("Financial Status" if source == "nasdaqlisted" else "Exchange")
    if not required.issubset(reader.fieldnames or []):
        raise ValueError(f"Unexpected {source} directory schema")
    accepted, exclusions = [], []
    seen = set()
    for row in reader:
        if None in row or any(row.get(field) is None for field in required):
            raise ValueError(f"Malformed row in {source}")
        symbol, name = row[symbol_field].strip(), row["Security Name"].strip()
        if symbol in seen:
            raise ValueError(f"Duplicate directory symbol: {symbol}")
        seen.add(symbol)
        reason = None
        if row["Test Issue"] != "N":
            reason = "test_or_unknown_status"
        elif row["ETF"] != "N":
            reason = "etf_or_unknown_flag"
        elif source == "nasdaqlisted" and row["Financial Status"] != "N":
            reason = "non_normal_financial_status"
        elif not COMMON.search(name) or EXCLUDED.search(name):
            reason = "not_explicit_common_or_ordinary_stock"
        # Reject special symbol syntax instead of silently guessing IBKR mappings.
        elif not re.fullmatch(r"[A-Z]{1,6}", symbol):
            reason = "unsupported_symbol_syntax"
        exchange = "NASDAQ" if source == "nasdaqlisted" else EXCHANGES.get(row["Exchange"])
        if not exchange:
            reason = "unsupported_exchange"
        if reason:
            exclusions.append({"symbol": symbol, "name": name, "source": source, "reason": reason})
        else:
            accepted.append(Instrument(symbol, exchange))
    if not seen:
        raise ValueError("Empty directory")
    return accepted, exclusions


def discover(path: Path, *, timeout: float = 30) -> tuple[list[Instrument], dict]:
    path.mkdir(parents=True, exist_ok=False)
    all_items, exclusions, sources = [], [], []
    for source, url in SOURCES.items():
        with urlopen(
            Request(url, headers={"User-Agent": "ib-lab/0.2"}), timeout=timeout
        ) as response:
            payload = response.read(10_000_001)
        if len(payload) > 10_000_000:
            raise ValueError("Listing directory exceeds size limit")
        text = payload.decode("utf-8-sig")
        items, rejected = parse_directory(text, source)
        raw = path / f"{source}.txt"
        raw.write_bytes(payload)
        sources.append({"url": url, "sha256": sha256(raw), "footer": text.splitlines()[-1]})
        all_items.extend(items)
        exclusions.extend(rejected)
    counts = Counter(i.symbol for i in all_items)
    duplicates = {symbol for symbol, count in counts.items() if count > 1}
    items = sorted((i for i in all_items if i.symbol not in duplicates), key=lambda i: i.key)
    exclusions.extend({"symbol": s, "reason": "ambiguous_multiple_listings"} for s in duplicates)
    if not items:
        raise ValueError("No eligible directory listings")
    metadata = {
        "source": "nasdaq_trader_current_directories",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "sources": sources,
        "eligible_count": len(items),
        "exclusions": exclusions,
        "classification": "conservative_name_heuristic_common_or_ordinary_stock",
        "limitations": "Current listings; excludes ADRs, special symbols, "
        "ambiguous names and non-normal Nasdaq listings. Not a certified security master.",
    }
    with (path / "universe.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "primary_exchange", "currency"])
        writer.writeheader()
        writer.writerows(asdict(i) for i in items)
    write_json(path / "discovery.json", metadata)
    return items, metadata
