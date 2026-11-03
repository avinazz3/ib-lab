import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ib_lab.broker import ConnectionSettings
from ib_lab.cache import HistoryCache
from ib_lab.calendar import Sessions
from ib_lab.cli import main
from ib_lab.daily import collect, combine_history, seed_cache
from ib_lab.demo import DEMO_DECISION, demo_data, write_demo
from ib_lab.universe import parse_directory

NASDAQ = "Symbol|Security Name|Test Issue|Financial Status|ETF\n"
OTHER = "ACT Symbol|Security Name|Test Issue|ETF|Exchange\n"
FOOTER = "File Creation Time: 1102202621:00\n"


def test_directory_filters_and_schema():
    text = (
        NASDAQ
        + (
            "GOOD|Good Corp Common Stock|N|N|N\n"
            "ETF|Equity Common Stock ETF|N|N|Y\n"
            "BAD|Preferred Common Stock|N|N|N\n"
            "TEST|Test Common Stock|Y|N|N\n"
            "ADR|American Depositary Common Shares|N|N|N\n"
            "DEF|Deficient Common Stock|N|D|N\n"
            "UNIT|Units each including common shares|N|N|N\n"
        )
        + FOOTER
    )
    accepted, rejected = parse_directory(text, "nasdaqlisted")
    assert [i.symbol for i in accepted] == ["GOOD"]
    assert len(rejected) == 6
    with pytest.raises(ValueError, match="footer"):
        parse_directory(text.replace(FOOTER, ""), "nasdaqlisted")
    with pytest.raises(ValueError, match="schema"):
        parse_directory("broken\n" + FOOTER, "otherlisted")
    other = OTHER + "GOOD|Good Ordinary Shares|N|N|N\nBRK.B|Common Stock|N|N|N\n" + FOOTER
    items, rejected = parse_directory(other, "otherlisted")
    assert items[0].primary_exchange == "NYSE"
    assert rejected[0]["reason"] == "unsupported_symbol_syntax"


@pytest.fixture
def history():
    bars, instruments = demo_data()
    instrument = instruments[-1]
    return instrument, [b for b in bars if b.instrument_key == instrument.key]


def broker_type(instrument, rows, *, failure=False):
    class Broker:
        calls = []
        connected = 0

        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            self.__class__.connected += 1
            return self

        async def __aexit__(self, *_exc):
            pass

        async def resolve(self, item):
            assert item == instrument
            return SimpleNamespace(conId=123)

        async def daily(self, item, _contract, _end, *, duration):
            self.calls.append(duration)
            if failure:
                raise ValueError("subscription unavailable")
            return rows if duration == "2 Y" else rows[-10:]

    return Broker


def test_bootstrap_checkpoint_then_reuse_without_any_connection(tmp_path, history):
    instrument, bars = history
    cache = HistoryCache(tmp_path / "cache.sqlite")
    broker = broker_type(instrument, bars)
    try:
        first = asyncio.run(
            collect([instrument], cache, ConnectionSettings(), DEMO_DECISION, broker_factory=broker)
        )
        assert first[3][0]["action"] == "full_refresh"
        assert len(first[0]) == 146
        again = asyncio.run(
            collect([instrument], cache, ConnectionSettings(), DEMO_DECISION, broker_factory=broker)
        )
        assert again[3][0]["action"] == "reused"
        assert broker.connected == 1
        assert broker.calls == ["2 Y"]
    finally:
        cache.close()
    reopened = HistoryCache(tmp_path / "cache.sqlite")
    try:
        assert len(reopened.get(instrument)[2]) == 160
    finally:
        reopened.close()


def test_incremental_overlap_and_changed_prices_trigger_full_refresh(tmp_path, history):
    instrument, bars = history
    cache = HistoryCache(tmp_path / "cache.sqlite")
    try:
        cache.put(instrument, 123, bars[-3].session, bars[:-1])
        broker = broker_type(instrument, bars)
        result = asyncio.run(
            collect([instrument], cache, ConnectionSettings(), DEMO_DECISION, broker_factory=broker)
        )
        assert result[3][0]["action"] == "incremental"
        assert broker.calls == ["10 D"]
        assert len(cache.get(instrument)[2]) == 160
        # Reverse-split-style change in the overlapping prices must refresh old history.
        cache.put(instrument, 123, bars[-3].session, bars[:-1])
        changed = [
            replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2)
            for b in bars
        ]
        broker = broker_type(instrument, changed)
        result = asyncio.run(
            collect([instrument], cache, ConnectionSettings(), DEMO_DECISION, broker_factory=broker)
        )
        assert result[3][0]["action"] == "full_refresh"
        assert broker.calls == ["10 D", "2 Y"]
        assert cache.get(instrument)[2][0].close == changed[0].close
    finally:
        cache.close()


def test_failed_update_preserves_previous_checkpoint_but_excludes_stale_bars(tmp_path, history):
    instrument, bars = history
    cache = HistoryCache(tmp_path / "cache.sqlite")
    try:
        cache.put(instrument, 123, bars[-3].session, bars[:-1])
        before = cache.get(instrument)
        result = asyncio.run(
            collect(
                [instrument],
                cache,
                ConnectionSettings(),
                DEMO_DECISION,
                broker_factory=broker_type(instrument, bars, failure=True),
            )
        )
        assert result[0] == []
        assert len(result[2]) == 1
        assert cache.get(instrument) == before
    finally:
        cache.close()


def test_missing_overlap_cannot_be_blindly_appended(history):
    _, bars = history
    assert combine_history(bars[:-10], bars[-5:]) is None
    revised_volume = [replace(b, volume=b.volume + 1) for b in bars[-5:]]
    assert combine_history(bars, revised_volume)[-1].volume == revised_volume[-1].volume


def test_single_command_seeded_cache_end_to_end(tmp_path, history, monkeypatch):
    instrument, _ = history
    seed = tmp_path / "seed"
    write_demo(seed)
    manifest_path = seed / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        source="ibkr",
        synthetic=False,
        duration="2 Y",
        price_basis="IBKR_NATIVE_TRADES",
        created_at="2026-10-30T22:00:00+00:00",
    )
    for item in manifest["instruments"]:
        item["con_id"] = 123
    manifest_path.write_text(json.dumps(manifest))
    universe = tmp_path / "universe.csv"
    universe.write_text(f"symbol,primary_exchange\n{instrument.symbol},SYNTHETIC\n")

    # Freeze only workflow clock; no real broker session may be opened in this test.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 30, 22, tzinfo=UTC)

    monkeypatch.setattr("ib_lab.daily.datetime", Clock)
    out = tmp_path / "run"
    assert (
        main(
            [
                "daily",
                "--universe",
                str(universe),
                "--seed-snapshot",
                str(seed),
                "--cache",
                str(tmp_path / "cache.sqlite"),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads((out / "scan/report.json").read_text())
    assert report["universe_count"] == 1
    assert report["data_error_count"] == 0
    assert report["last_completed_session"] == "2026-10-30"


def test_seed_drops_bar_that_was_incomplete_when_downloaded(tmp_path):
    seed = tmp_path / "seed"
    write_demo(seed)
    path = seed / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(
        source="ibkr",
        synthetic=False,
        duration="2 Y",
        price_basis="IBKR_NATIVE_TRADES",
        created_at="2026-10-30T17:00:00+00:00",
    )
    for item in manifest["instruments"]:
        item["con_id"] = 123
    path.write_text(json.dumps(manifest))
    cache = HistoryCache(tmp_path / "cache.sqlite")
    try:
        calendar = Sessions(DEMO_DECISION.date() - timedelta(days=1000), DEMO_DECISION.date())
        seed_cache(cache, seed, calendar, DEMO_DECISION.date())
        assert cache.get(demo_data()[1][0])[2][-1].session.isoformat() == "2026-10-29"
    finally:
        cache.close()


def test_periodic_full_refresh_and_download_budget(tmp_path, history):
    instrument, bars = history
    cache = HistoryCache(tmp_path / "cache.sqlite")
    try:
        cache.put(instrument, 123, bars[-1].session - timedelta(days=31), bars)
        broker = broker_type(instrument, bars)
        result = asyncio.run(
            collect([instrument], cache, ConnectionSettings(), DEMO_DECISION, broker_factory=broker)
        )
        assert result[3][0]["action"] == "full_refresh"
        assert broker.calls == ["2 Y"]
        missing = replace(instrument, symbol="MISSING")
        # Reuse needs no budget. An exhausted budget must remain an explicit failure.
        result = asyncio.run(
            collect(
                [instrument, missing],
                cache,
                ConnectionSettings(),
                DEMO_DECISION,
                broker_factory=broker,
                max_downloads=0,
            )
        )
        assert result[3][0]["action"] == "reused"
        assert "budget" in result[2][0]["error"]
    finally:
        cache.close()


def test_interruption_keeps_completed_stock_checkpoint(tmp_path, history):
    instrument, bars = history
    second = replace(instrument, symbol="SECOND")
    cache = HistoryCache(tmp_path / "cache.sqlite")

    class Broker(broker_type(instrument, bars)):
        async def resolve(self, item):
            if item == second:
                raise asyncio.CancelledError()
            return await super().resolve(item)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                collect(
                    [instrument, second],
                    cache,
                    ConnectionSettings(),
                    DEMO_DECISION,
                    broker_factory=Broker,
                )
            )
        assert len(cache.get(instrument)[2]) == 160
        assert cache.get(second) is None
    finally:
        cache.close()


def test_concurrent_runs_cannot_share_writer_lock(tmp_path):
    from filelock import FileLock, Timeout

    cache = tmp_path / "cache.sqlite"
    out = tmp_path / "blocked"
    with FileLock(str(cache) + ".lock", timeout=0):
        with pytest.raises(Timeout):
            from argparse import Namespace

            from ib_lab.daily import run_daily

            asyncio.run(
                run_daily(
                    Namespace(
                        cache=cache,
                        out=out,
                        host="127.0.0.1",
                        port=7496,
                        client_id=31,
                        timeout=20,
                        request_spacing=1,
                        completion_lag_minutes=20,
                    )
                )
            )
    assert not out.exists()
