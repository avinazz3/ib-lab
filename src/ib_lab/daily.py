"""One daily orchestration path; reuses the existing broker, snapshot and scanner."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filelock import FileLock

from ib_lab.broker import ConnectionSettings, ReadOnlyBroker
from ib_lab.cache import HistoryCache
from ib_lab.calendar import Sessions
from ib_lab.models import Instrument, read_universe
from ib_lab.screen import RESEARCH_SESSIONS
from ib_lab.store import new_directory, read_snapshot, sha256, write_json, write_snapshot
from ib_lab.universe import discover


def seed_cache(cache: HistoryCache, path: Path, calendar: Sessions, target):
    bars, instruments, manifest = read_snapshot(path)
    if (
        manifest.get("source") != "ibkr"
        or manifest.get("synthetic") is not False
        or manifest.get("price_basis") != "IBKR_NATIVE_TRADES"
        or manifest.get("use_rth") is not True
        or manifest.get("duration") != "2 Y"
    ):
        raise ValueError("Only native, nonsynthetic IBKR RTH snapshots can seed this cache")
    created = datetime.fromisoformat(manifest["created_at"])
    if created.tzinfo is None:
        raise ValueError("Seed snapshot needs an aware creation timestamp")
    ids = {Instrument(**x["instrument"]).key: x.get("con_id") for x in manifest["instruments"]}
    for instrument in instruments:
        if cache.get(instrument) is not None or not ids[instrument.key]:
            continue
        selected = sorted(
            (
                b
                for b in bars
                if b.instrument_key == instrument.key
                and b.session <= target
                and calendar.calendar.session_close(b.session).to_pydatetime()
                <= created - timedelta(minutes=20)
            ),
            key=lambda b: b.session,
        )
        if any(not calendar.calendar.is_session(b.session) for b in selected):
            raise ValueError("Seed contains bars on nontrading sessions")
        if selected:
            cache.put(instrument, ids[instrument.key], min(created.date(), target), selected)


def combine_history(old, new):
    """Return None when prices cannot safely share a split-adjustment basis."""
    prior = {bar.session: bar for bar in old}
    overlap = [bar for bar in new if bar.session in prior]
    if not overlap:
        return None
    for bar in overlap:
        if any(
            not math.isclose(
                getattr(bar, field), getattr(prior[bar.session], field), rel_tol=1e-8, abs_tol=1e-6
            )
            for field in ("open", "high", "low", "close")
        ):
            return None
    prior.update({bar.session: bar for bar in new})
    return sorted(prior.values(), key=lambda b: b.session)[-550:]


async def collect(
    instruments,
    cache,
    settings,
    decision,
    *,
    broker_factory=ReadOnlyBroker,
    progress=None,
    max_downloads=None,
):
    calendar = Sessions(decision.date() - timedelta(days=800), decision.date())
    completed = calendar.completed(decision.date() - timedelta(days=550), decision)
    target, required = completed[-1], set(completed[-127:])
    collected, identities, failures, actions = [], [], [], []
    downloads = 0
    async with AsyncExitStack() as stack:
        broker = None
        for index, instrument in enumerate(instruments):
            action, con_id = "failed", None
            connecting = False
            try:
                cached = cache.get(instrument)
                fresh = False
                if cached:
                    con_id, refreshed, old = cached
                    old = [b for b in old if b.session <= target]
                    fresh = (
                        bool(old)
                        and old[-1].session == target
                        and {day for day in required if day >= old[0].session}.issubset(
                            {b.session for b in old}
                        )
                        and 0 <= (target - refreshed).days < 30
                    )
                if fresh:
                    selected, action = old, "reused"
                else:
                    if max_downloads is not None and downloads >= max_downloads:
                        raise ValueError(
                            "Download budget exhausted; rerun to continue checkpointed work"
                        )
                    downloads += 1
                    if broker is None:
                        connecting = True
                        broker = await stack.enter_async_context(broker_factory(settings))
                        connecting = False
                    contract = await broker.resolve(instrument)
                    con_id = contract.conId
                    full = (
                        not cached
                        or cached[0] != con_id
                        or not old
                        or not 0 <= (target - cached[1]).days < 30
                        or len(old) < 127
                        or (target - old[-1].session).days > 350
                        or not {day for day in required if day <= old[-1].session}.issubset(
                            {b.session for b in old}
                        )
                    )
                    duration = (
                        "2 Y" if full else f"{max(10, (target - old[-1].session).days + 7)} D"
                    )
                    end = calendar.calendar.session_close(target).to_pydatetime() + timedelta(
                        seconds=1
                    )
                    new = await broker.daily(instrument, contract, end, duration=duration)
                    new = sorted((b for b in new if b.session <= target), key=lambda b: b.session)
                    for bar in new:
                        bar.validate()
                        if not calendar.calendar.is_session(bar.session):
                            raise ValueError("Broker returned nontrading-session history")
                    if len({b.session for b in new}) != len(new):
                        raise ValueError("Broker returned duplicate daily bars")
                    selected = new if full else combine_history(old, new)
                    if selected is None:
                        full = True
                        new = await broker.daily(instrument, contract, end, duration="2 Y")
                        selected = sorted(
                            (b for b in new if b.session <= target), key=lambda b: b.session
                        )
                    # Short history is retained for future IPO warm-up, but screen rejects it.
                    if not selected or selected[-1].session != target:
                        raise ValueError("History has not reached the target completed session")
                    if any(not calendar.calendar.is_session(b.session) for b in selected):
                        raise ValueError("Invalid refreshed session")
                    cache.put(instrument, con_id, target if full else cached[1], selected)
                    action = "full_refresh" if full else "incremental"
                collected.extend(selected[-RESEARCH_SESSIONS:])
            except Exception as error:
                if connecting:
                    raise ConnectionError(
                        "Could not connect to IBKR; cache checkpoints retained"
                    ) from error
                failures.append({"instrument_key": instrument.key, "error": str(error)})
            identities.append({"instrument": asdict(instrument), "con_id": con_id})
            actions.append({"instrument_key": instrument.key, "action": action})
            if progress:
                progress(index + 1, len(instruments), instrument.key, action)
    return collected, identities, failures, actions


async def run_daily(args):
    # Import here to reuse the exact offline report path without a module import cycle.
    from ib_lab.cli import run_scan

    now = datetime.now(UTC)
    decision = now - timedelta(minutes=args.completion_lag_minutes)
    out = args.out or Path("runs") / now.strftime("daily-%Y%m%dT%H%M%S-%fZ")
    settings = ConnectionSettings(
        args.host, args.port, args.client_id, args.timeout, args.request_spacing
    )
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    # A second process fails fast instead of racing per-stock read/merge/write cycles.
    with FileLock(str(args.cache) + ".lock", timeout=0):
        new_directory(out)
        cache = HistoryCache(args.cache)
        status = {
            "status": "running",
            "started_at": now.isoformat(),
            "decision_at": decision.isoformat(),
            "out": str(out),
        }
        write_json(out / "status.json", status)
        try:
            if args.universe:
                instruments = read_universe(args.universe)
                universe = {"source": "user_supplied_list", "sha256": sha256(args.universe)}
            else:
                instruments, universe = await asyncio.to_thread(discover, out / "universe")
            total = len(instruments)
            if args.max_symbols:
                instruments = instruments[: args.max_symbols]
            universe["discovered_count"] = total
            universe["selected_count"] = len(instruments)
            universe["limited"] = len(instruments) < total
            write_json(out / "universe.json", universe)
            if args.seed_snapshot:
                calendar = Sessions(decision.date() - timedelta(days=1000), decision.date())
                target = calendar.completed(decision.date() - timedelta(days=550), decision)[-1]
                seed_cache(cache, args.seed_snapshot, calendar, target)

            def progress(index, count, key, action):
                print(f"[{index}/{count}] {key}: {action}", file=sys.stderr, flush=True)
                status.update({"processed": index, "selected": count, "last_action": action})
                write_json(out / "status.json", status)

            bars, identities, failures, actions = await collect(
                instruments,
                cache,
                settings,
                decision,
                progress=progress,
                max_downloads=args.max_downloads,
            )
            snapshot = out / "snapshot"
            new_directory(snapshot)
            write_snapshot(
                snapshot,
                bars,
                {
                    "source": "ibkr",
                    "synthetic": False,
                    "status": "partial" if failures else "complete",
                    "created_at": datetime.now(UTC).isoformat(),
                    "decision_at": decision.isoformat(),
                    "price_basis": "IBKR_NATIVE_TRADES",
                    "bar_size": "1 day",
                    "use_rth": True,
                    "instruments": identities,
                    "failures": failures,
                    "cache_actions": actions,
                    "universe_scope": universe,
                    "completion_lag_minutes": args.completion_lag_minutes,
                    "cache_policy": "7-day overlap; changed OHLC or 30-day age -> full refresh",
                },
            )
            from argparse import Namespace

            code = run_scan(
                Namespace(snapshot=snapshot, as_of=decision, config=args.config, out=out / "scan")
            )
            report = json.loads((out / "scan" / "report.json").read_text())
            report["universe_scope"] = universe
            if universe["limited"]:
                report["warnings"].append("Explicit symbol limit: this is a subset scan.")
            write_json(out / "scan" / "report.json", report)
            status.update(
                {
                    "status": "partial" if code else "complete",
                    "exit_code": code,
                    "candidate_count": report["candidate_count"],
                    "failures": failures,
                    "report": str(out / "scan" / "report.json"),
                }
            )
            print(json.dumps(status, indent=2))
            return code
        except BaseException as error:
            status.update(
                {
                    "status": "interrupted_or_failed",
                    "error": str(error),
                    "error_type": type(error).__name__,
                }
            )
            raise
        finally:
            cache.close()
            write_json(out / "status.json", status)
