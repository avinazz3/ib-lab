from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ib_lab import __version__
from ib_lab.broker import (
    BrokerUnavailableError,
    ConnectionSettings,
    ReadOnlyBroker,
    failure_details,
)
from ib_lab.demo import DEMO_DECISION, write_demo
from ib_lab.models import Instrument, aware_timestamp, read_universe
from ib_lab.screen import LOOKBACKS, BreakoutConfig, screen
from ib_lab.store import new_directory, read_snapshot, sha256, write_json, write_snapshot


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Read-only IBKR data and momentum research")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Create a deterministic synthetic daily snapshot")
    demo.add_argument("--out", type=Path, required=True)
    scan = commands.add_parser("scan", help="Screen an existing daily snapshot offline")
    scan.add_argument("--snapshot", type=Path, required=True)
    scan.add_argument("--as-of", type=aware_timestamp, required=True)
    scan.add_argument("--config", type=Path)
    scan.add_argument("--out", type=Path, required=True)
    for command in ("probe", "download", "daily"):
        sub = commands.add_parser(command, help="Read-only IBKR " + command)
        sub.add_argument("--host", default="127.0.0.1")
        sub.add_argument("--port", type=int, default=7497)
        sub.add_argument("--client-id", type=int, default=31)
        sub.add_argument("--timeout", type=float, default=20)
        sub.add_argument("--request-spacing", type=float, default=1)
        sub.add_argument("--max-attempts", type=int, default=3)
        sub.add_argument("--recovery-timeout", type=float, default=60)
        sub.add_argument("--out", type=Path, required=command != "daily")
        if command == "probe":
            sub.add_argument("--symbol", default="AAPL")
            sub.add_argument("--primary-exchange", default="NASDAQ")
            sub.add_argument("--data-type", type=int, choices=(1, 2, 3, 4), default=1)
            sub.add_argument(
                "--require-live",
                action="store_true",
                help="Return exit 1 if the live quote diagnostic does not pass",
            )
        elif command == "download":
            sub.add_argument("--universe", type=Path, required=True)
        else:
            sub.add_argument("--universe", type=Path, help="Override automatic listing discovery")
            sub.add_argument("--cache", type=Path, default=Path("data/history-cache.sqlite"))
            sub.add_argument("--seed-snapshot", type=Path)
            sub.add_argument(
                "--refresh-candidates",
                type=Path,
                help="Force full history refresh for names in a candidates/universe CSV",
            )
            sub.add_argument("--config", type=Path)
            sub.add_argument("--max-symbols", type=int)
            sub.add_argument("--max-downloads", type=int)
            sub.add_argument("--completion-lag-minutes", type=float, default=20)
    return root


def run_scan(args) -> int:
    config = (
        BreakoutConfig(**json.loads(args.config.read_text())) if args.config else BreakoutConfig()
    )
    bars, instruments, manifest = read_snapshot(args.snapshot)
    report = screen(bars, instruments, args.as_of, config)
    failures = {item["instrument_key"]: item for item in manifest.get("failures", [])}
    for row in report["results"]:
        if row["instrument_key"] in failures:
            row["download_failure"] = failures[row["instrument_key"]]
            if "data_error" in row:
                row["data_error_kind"] = "download_failure"
    report["download_failure_count"] = len(failures)
    report["data_error_counts"] = dict(
        Counter(row["data_error_kind"] for row in report["results"] if "data_error" in row)
    )
    if manifest["status"] != "complete":
        report["warnings"].append(
            "Snapshot is partial; leadership rankings depend on successful data coverage."
        )
    report.update(
        {
            "source": manifest["source"],
            "synthetic": manifest.get("synthetic", False),
            "snapshot_bars_sha256": manifest["bars_sha256"],
            "snapshot_manifest_sha256": sha256(args.snapshot / "manifest.json"),
            "snapshot_created_at": manifest["created_at"],
            "snapshot_status": manifest["status"],
            "price_basis": manifest["price_basis"],
            "historical_availability_verified": False,
        }
    )
    report["warnings"].append(
        "Historical as-of replay uses this snapshot's later data vintage and declared universe; "
        "it does not establish point-in-time historical availability."
    )
    new_directory(args.out)
    write_json(args.out / "report.json", report)
    accepted = [row for row in report["results"] if row["accepted"]]
    feature_columns = [
        "close",
        "base_ceiling",
        "atr14",
        "contraction",
        "volume20",
        "dollar_volume20",
        *[f"return_{n}" for n in LOOKBACKS],
        *[f"percentile_{n}" for n in LOOKBACKS],
        "prior_impulse",
        "base_depth",
        "support_change",
        "sma20",
        "sma20_change5",
        "sma_distance_atr",
    ]
    check_columns = [
        "price",
        "liquidity",
        "leadership",
        "impulse",
        "base_depth",
        "contraction",
        "rising_support",
        "sma_proximity",
        "rising_sma",
    ]
    with (args.out / "candidates.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "instrument_key",
                "symbol",
                "primary_exchange",
                "currency",
                "last_completed_session",
                *feature_columns,
                *[f"return_{n}_start" for n in LOOKBACKS],
                *[f"check_{key}" for key in check_columns],
                "rising_support_required",
                "event_window_start",
                "event_window_end",
                "relative_volume_session_count",
            ],
        )
        writer.writeheader()
        for row in accepted:
            writer.writerow(
                {
                    "instrument_key": row["instrument_key"],
                    **row["instrument"],
                    "last_completed_session": report["last_completed_session"],
                    **{key: row["features"][key] for key in feature_columns},
                    **{
                        f"return_{n}_start": row["research"]["return_start_dates"][str(n)]
                        for n in LOOKBACKS
                    },
                    **{f"check_{key}": row["checks"][key] for key in check_columns},
                    "rising_support_required": config.require_rising_support,
                    **{
                        key: row["research"][key]
                        for key in (
                            "event_window_start",
                            "event_window_end",
                            "relative_volume_session_count",
                        )
                    },
                }
            )
    with (args.out / "candidate-events.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "instrument_key",
                "date",
                "price_change",
                "opening_gap",
                "volume",
                "relative_volume",
                "selected_for",
            ],
        )
        writer.writeheader()
        for row in accepted:
            for event in row["research"]["events"]:
                writer.writerow(
                    {
                        "instrument_key": row["instrument_key"],
                        **event,
                        "selected_for": ";".join(event["selected_for"]),
                    }
                )
    print(
        json.dumps(
            {
                "candidates": report["candidate_count"],
                "universe": report["universe_count"],
                "rank_eligible": report["rank_eligible_count"],
                "data_errors": report["data_error_count"],
                "data_error_counts": report["data_error_counts"],
                "download_failures": report["download_failure_count"],
                "last_completed_session": report["last_completed_session"],
                "synthetic": report["synthetic"],
                "report": str(args.out / "report.json"),
                "candidates_csv": str(args.out / "candidates.csv"),
                "events_csv": str(args.out / "candidate-events.csv"),
                "warnings": report["warnings"],
            },
            indent=2,
        )
    )
    return 1 if report["data_error_count"] or manifest["status"] != "complete" else 0


async def run_broker(args) -> int:
    settings = ConnectionSettings(
        args.host,
        args.port,
        args.client_id,
        args.timeout,
        args.request_spacing,
        max_attempts=args.max_attempts,
        recovery_timeout=args.recovery_timeout,
    )
    if args.command == "probe" and args.require_live and args.data_type != 1:
        raise ValueError("--require-live requires --data-type 1")
    instruments = (
        read_universe(args.universe)
        if args.command == "download"
        else [Instrument(args.symbol.upper(), args.primary_exchange.upper())]
    )
    new_directory(args.out)
    decision_at = datetime.now(UTC)
    broker = ReadOnlyBroker(settings)
    try:
        async with broker:
            if args.command == "probe":
                report = await broker.probe(instruments[0], args.data_type)
                write_json(args.out / "probe.json", report)
                print(json.dumps(report, indent=2))
                return (
                    1 if args.require_live and not report["quote"]["live_quote_check_passed"] else 0
                )
            bars, resolved, failures = [], [], []
            # Deliberately serial: local pacing, per-symbol diagnostics, no silent omissions.
            for item in instruments:
                try:
                    contract = await broker.resolve(item)
                    downloaded = await broker.daily(item, contract, decision_at)
                    bars.extend(downloaded)
                    resolved.append({"instrument": asdict(item), "con_id": contract.conId})
                    print(f"{item.key}: {len(downloaded)} bars", file=sys.stderr)
                except Exception as error:
                    failures.append({"instrument_key": item.key, **failure_details(error)})
                    resolved.append({"instrument": asdict(item), "con_id": None})
                    print(f"{item.key}: failed: {error}", file=sys.stderr)
                    if isinstance(error, BrokerUnavailableError):
                        write_json(args.out / "failures.json", failures)
                        raise
            write_snapshot(
                args.out,
                bars,
                {
                    "source": "ibkr",
                    "synthetic": False,
                    "status": "partial" if failures else "complete",
                    "created_at": datetime.now(UTC).isoformat(),
                    "decision_at": decision_at.isoformat(),
                    "price_basis": "IBKR_NATIVE_TRADES",
                    "adjustment_note": "Native broker history; no local adjustment/PIT guarantee.",
                    "duration": "2 Y",
                    "bar_size": "1 day",
                    "use_rth": True,
                    "universe_scope": "user_supplied_list",
                    "universe_sha256": sha256(args.universe),
                    "instruments": resolved,
                    "failures": failures,
                    "broker_messages": broker.errors,
                    "retries": getattr(broker, "retries", []),
                },
            )
            print(json.dumps({"snapshot": str(args.out), "failures": len(failures)}))
            return 1 if failures else 0
    except Exception as error:
        write_json(
            args.out / "error.json",
            {
                "command": args.command,
                **failure_details(error),
                "readonly_requested": True,
                "broker_messages": getattr(broker, "errors", []),
                "retries": getattr(broker, "retries", []),
            },
        )
        raise


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        if args.command == "demo":
            write_demo(args.out)
            print(f"Synthetic snapshot: {args.out}; scan --as-of {DEMO_DECISION.isoformat()}")
            return 0
        if args.command == "scan":
            return run_scan(args)
        if args.command == "daily":
            import math

            from ib_lab.daily import run_daily

            for limit in (args.max_symbols, args.max_downloads):
                if limit is not None and limit < 1:
                    raise ValueError("Symbol/download limits must be positive")
            if not math.isfinite(args.completion_lag_minutes) or args.completion_lag_minutes < 0:
                raise ValueError("Completion lag must be finite and nonnegative")
            return asyncio.run(run_daily(args))
        return asyncio.run(run_broker(args))
    except Exception as error:
        # CLI boundary: SDK RequestError is not an OSError; report it consistently too.
        print(f"ib-lab: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ib-lab: interrupted", file=sys.stderr)
        return 130
