from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ib_lab.models import DailyBar, Instrument


def new_directory(path: Path) -> None:
    """A snapshot/run never overwrites an earlier observation."""
    path.mkdir(parents=True, exist_ok=False)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False, default=str) + "\n")
    temporary.replace(path)


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_snapshot(path: Path, bars: list[DailyBar], manifest: dict) -> None:
    schema = pa.schema(
        [
            ("instrument_key", pa.string()),
            ("session", pa.date32()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.float64()),
        ]
    )
    temporary = path / "bars.parquet.tmp"
    table = pa.Table.from_pylist([asdict(bar) for bar in bars], schema=schema)
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(path / "bars.parquet")
    manifest = {**manifest, "schema_version": 1, "bars_sha256": sha256(path / "bars.parquet")}
    write_json(path / "manifest.json", manifest)


def read_snapshot(path: Path) -> tuple[list[DailyBar], list[Instrument], dict]:
    manifest = json.loads((path / "manifest.json").read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported snapshot schema")
    if sha256(path / "bars.parquet") != manifest["bars_sha256"]:
        raise ValueError("Snapshot bars checksum differs from manifest")
    instruments = [Instrument(**item["instrument"]) for item in manifest["instruments"]]
    keys = [item.key for item in instruments]
    if len(set(keys)) != len(keys) or not keys:
        raise ValueError("Snapshot has duplicate or empty universe")
    bars = [DailyBar(**row) for row in pq.read_table(path / "bars.parquet").to_pylist()]
    if any(bar.instrument_key not in keys for bar in bars):
        raise ValueError("Snapshot bars contain an instrument outside the declared universe")
    return bars, instruments, manifest
