"""Evaluate archived daily signals against later prices in a fixed-horizon event study."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import date, timedelta
from statistics import fmean

from ib_lab.calendar import Sessions
from ib_lab.models import Instrument, aware_timestamp
from ib_lab.store import new_directory, read_snapshot, sha256, write_json


def contract_ids(manifest):
    return {
        Instrument(**row["instrument"]).key: row.get("con_id") for row in manifest["instruments"]
    }


def validate_capture(run, report_path, report, signal_day, market):
    manifest_path = run / "snapshot" / "manifest.json"
    if not report.get("report_created_at"):
        raise ValueError(f"{run}: archived report predates recorded signal timestamps")
    if sha256(manifest_path) != report["snapshot_manifest_sha256"]:
        raise ValueError(f"{run}: report and snapshot manifest hashes differ")
    _, instruments, manifest = read_snapshot(run / "snapshot")
    if report["snapshot_bars_sha256"] != manifest["bars_sha256"]:
        raise ValueError(f"{run}: report and snapshot bars hashes differ")

    entry_day = market.calendar.next_session(signal_day).date()
    decision = aware_timestamp(report["decision_at"])
    captured = aware_timestamp(manifest["created_at"])
    ready = aware_timestamp(report["report_created_at"])
    if (
        manifest.get("decision_at") != report["decision_at"]
        or not market.calendar.session_close(signal_day) <= decision <= captured <= ready
        or ready >= market.calendar.session_open(entry_day)
    ):
        raise ValueError(f"{run}: signal was not available between its close and next open")
    scope = manifest["universe_scope"]
    if isinstance(scope, dict):
        discovery_times = [scope.get("retrieved_at")]
        discovery_times.extend(
            scan.get("observed_at") for scan in scope.get("prefilter", {}).get("scans", [])
        )
        if any(ts and aware_timestamp(ts) > captured for ts in discovery_times):
            raise ValueError(f"{run}: discovery timestamp follows the snapshot capture")
    keys = [row["instrument_key"] for row in report["results"]]
    if len(keys) != len(instruments) or set(keys) != {item.key for item in instruments}:
        raise ValueError(f"{run}: report rows do not cover the snapshot universe")

    return signal_day, report, manifest, sha256(report_path)


def run_backtest(args) -> int:
    holding = args.holding_sessions
    if not 1 <= holding <= 252:
        raise ValueError("Holding sessions must be between 1 and 252")
    if not math.isfinite(args.cost_bps) or not 0 <= args.cost_bps < 10_000:
        raise ValueError("Cost must be finite and below 10,000 basis points per side")
    cost = args.cost_bps / 10_000
    reports = []
    for run in args.runs:
        path = run / "scan/report.json"
        report = json.loads(path.read_text())
        reports.append((run, path, report, date.fromisoformat(report["last_completed_session"])))
    days = [day for _, _, _, day in reports]
    if len(set(days)) != len(days):
        raise ValueError("Supply exactly one archived scan per signal session")
    market = Sessions(min(days), max(days) + timedelta(days=holding * 3 + 14))
    captures = [
        validate_capture(*item, market) for item in sorted(reports, key=lambda item: item[-1])
    ]
    experiment = {
        (
            report["strategy_version"],
            report["config_sha256"],
            report["ranking_scope"],
            manifest["source"],
            manifest["price_basis"],
            manifest.get("synthetic", False),
        )
        for _, report, manifest, _ in captures
    }
    if len(experiment) != 1:
        raise ValueError("Use the same strategy, configuration, ranking scope and data basis")
    version, config_sha, scope, source, price_basis, synthetic = experiment.pop()

    outcomes, outcome_ids = {}, {}
    outcome_manifest_hash = None
    prices_as_of = None
    if args.outcomes:
        bars, _, manifest = read_snapshot(args.outcomes)
        if (
            manifest["source"] != source
            or manifest["price_basis"] != price_basis
            or manifest.get("synthetic", False) != synthetic
            or manifest.get("bar_size") != "1 day"
            or manifest.get("use_rth") is not True
        ):
            raise ValueError("Outcome snapshot must use the same source and regular-session basis")
        # Use request cutoff, not the time the last request finished in a long download.
        prices_as_of = aware_timestamp(manifest["decision_at"])
        if prices_as_of > aware_timestamp(manifest["created_at"]):
            raise ValueError("Outcome request cutoff follows outcome snapshot creation")
        outcome_ids = contract_ids(manifest)
        outcome_manifest_hash = sha256(args.outcomes / "manifest.json")
        for bar in bars:
            key = (bar.instrument_key, bar.session)
            if key in outcomes:
                raise ValueError(f"Duplicate outcome bar: {key}")
            bar.validate()
            outcomes[key] = bar

    cohorts = []
    universe = {}
    for day, report, manifest, report_hash in captures:
        sessions = market.calendar.sessions_window(day, holding + 1)[1:]
        held_days = [session.date() for session in sessions]
        entry, exit_day = held_days[0], held_days[-1]
        matured = (
            prices_as_of is not None and market.calendar.session_close(exit_day) <= prices_as_of
        )
        ids = contract_ids(manifest)
        events = []
        for row in report["results"]:
            if not (row["checks"].get("price") and row["checks"].get("liquidity")):
                continue
            key = row["instrument_key"]
            universe[key] = row["instrument"]
            event = {
                "instrument_key": key,
                "group": "candidate" if row["accepted"] else "eligible_rejected",
                "reasons": row["reasons"],
                "net_return": None,
            }
            window = [outcomes.get((key, held_day)) for held_day in held_days]
            if prices_as_of is None:
                event["status"] = "outcomes_not_supplied"
            elif not matured:
                event["status"] = "holding_window_incomplete"
            elif not ids[key] or outcome_ids.get(key) != ids[key]:
                event["status"] = "missing_or_mismatched_contract"
            elif any(bar is None or bar.volume <= 0 for bar in window):
                event["status"] = "missing_or_zero_volume_bar"
            else:
                buy, sell = window[0].open, window[-1].close
                event.update(
                    status="observed",
                    entry_open=buy,
                    exit_close=sell,
                    net_return=sell * (1 - cost) / (buy * (1 + cost)) - 1,
                )
            events.append(event)
        groups = {
            group: [event for event in events if event["group"] == group]
            for group in ("candidate", "eligible_rejected")
        }
        means = {
            group: fmean(event["net_return"] for event in rows)
            if rows and all(event["status"] == "observed" for event in rows)
            else None
            for group, rows in groups.items()
        }
        cohorts.append(
            {
                "signal_session": day,
                "signal_ready_at": report["report_created_at"],
                "report_sha256": report_hash,
                "snapshot_status": report["snapshot_status"],
                "universe_count": report["universe_count"],
                "data_error_count": report["data_error_count"],
                "download_failure_count": report["download_failure_count"],
                "entry_session": entry,
                "exit_session": exit_day,
                "events": events,
                "group_mean_net_returns": means,
                "candidate_minus_rejected": means["candidate"] - means["eligible_rejected"]
                if all(mean is not None for mean in means.values())
                else None,
            }
        )

    paired = [c for c in cohorts if c["candidate_minus_rejected"] is not None]
    result = {
        "study": "archived_daily_signal_event_study",
        "strategy_version": version,
        "config_sha256": config_sha,
        "ranking_scope": scope,
        "synthetic": synthetic,
        "holding_sessions": holding,
        "cost_bps_per_side": args.cost_bps,
        "outcome_manifest_sha256": outcome_manifest_hash,
        "outcome_request_cutoff": prices_as_of,
        "missing_signal_sessions": sorted(set(market.dates(min(days), max(days))) - set(days)),
        "event_counts": {
            group: dict(
                Counter(
                    event["status"]
                    for c in cohorts
                    for event in c["events"]
                    if event["group"] == group
                )
            )
            for group in ("candidate", "eligible_rejected")
        },
        "paired_sessions": len(paired),
        "mean_paired_group_net_returns": {
            group: fmean(c["group_mean_net_returns"][group] for c in paired) if paired else None
            for group in ("candidate", "eligible_rejected")
        },
        "mean_paired_candidate_minus_rejected": (
            fmean(c["candidate_minus_rejected"] for c in paired) if paired else None
        ),
        "cohorts": cohorts,
    }
    new_directory(args.out)
    write_json(args.out / "backtest.json", result)
    with (args.out / "outcome-universe.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "primary_exchange", "currency"])
        writer.writeheader()
        writer.writerows(universe[key] for key in sorted(universe))
    print(json.dumps({k: v for k, v in result.items() if k != "cohorts"}, default=str, indent=2))
    return 0
