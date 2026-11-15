import json
from dataclasses import asdict
from datetime import UTC, date, datetime

import pytest

from ib_lab.cli import main
from ib_lab.demo import DEMO_DECISION, demo_data
from ib_lab.models import DailyBar
from ib_lab.store import write_snapshot


@pytest.fixture
def archived_scan(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 30, 22, 30, tzinfo=UTC)

    monkeypatch.setattr("ib_lab.cli.datetime", Clock)
    all_bars, all_instruments = demo_data()
    instruments = all_instruments[-2:]
    identities = [
        {"instrument": asdict(item), "con_id": index}
        for index, item in enumerate(instruments, start=101)
    ]
    run = tmp_path / "run"
    snapshot = run / "snapshot"
    snapshot.mkdir(parents=True)
    manifest = {
        "source": "synthetic",
        "synthetic": True,
        "status": "complete",
        "price_basis": "synthetic_consistent",
        "bar_size": "1 day",
        "use_rth": True,
        "decision_at": DEMO_DECISION.isoformat(),
        "created_at": "2026-10-30T22:05:00+00:00",
        "universe_scope": {
            "prefilter": {
                "source": "ibkr_scanner",
                "hits": {item.key: [] for item in instruments},
                "scans": [{"status": "complete", "observed_at": "2026-10-30T22:02:00+00:00"}],
            },
        },
        "instruments": identities,
    }
    write_snapshot(
        snapshot,
        [bar for bar in all_bars if bar.instrument_key in {item.key for item in instruments}],
        manifest,
    )
    assert (
        main(
            [
                "scan",
                "--snapshot",
                str(snapshot),
                "--as-of",
                DEMO_DECISION.isoformat(),
                "--out",
                str(run / "scan"),
            ]
        )
        == 0
    )
    return run, instruments, manifest


def test_backtest_uses_next_open_and_reports_incomplete_outcomes(tmp_path, archived_scan):
    run, instruments, manifest = archived_scan
    outcome = tmp_path / "later"
    outcome.mkdir()
    start, end = date(2026, 11, 2), date(2026, 11, 3)
    bars = [
        DailyBar(item.key, day, 20, 25, 19, close, 100_000)
        for item, closes in zip(instruments, ((20, 21), (22, 24)), strict=True)
        for day, close in zip((start, end), closes, strict=True)
    ]
    manifest["decision_at"] = "2026-11-03T22:00:00+00:00"
    manifest["created_at"] = "2026-11-03T23:00:00+00:00"
    write_snapshot(outcome, bars, manifest)
    args = [
        "backtest",
        "--runs",
        str(run),
        "--outcomes",
        str(outcome),
        "--holding-sessions",
        "2",
    ]
    out = tmp_path / "valid"
    assert main([*args, "--out", str(out)]) == 0
    result = json.loads((out / "backtest.json").read_text())
    assert result["synthetic"] is True
    assert result["paired_sessions"] == 1
    assert result["mean_paired_candidate_minus_rejected"] == pytest.approx(3 / 20 * 0.999 / 1.001)
    assert result["cohorts"][0]["entry_session"] == start.isoformat()
    assert result["cohorts"][0]["exit_session"] == end.isoformat()
    # A long download finishing after close does not make a still-open bar usable.
    manifest["decision_at"] = "2026-11-03T17:00:00+00:00"
    write_snapshot(outcome, bars, manifest)
    out = tmp_path / "incomplete"
    assert main([*args, "--out", str(out)]) == 0
    result = json.loads((out / "backtest.json").read_text())
    assert result["paired_sessions"] == 0
    assert result["event_counts"]["candidate"] == {"holding_window_incomplete": 1}


def test_backtest_rejects_report_created_after_next_open(tmp_path, archived_scan, capsys):
    run, _, _ = archived_scan
    path = run / "scan" / "report.json"
    report = json.loads(path.read_text())
    report["report_created_at"] = "2026-11-02T14:30:01+00:00"
    path.write_text(json.dumps(report))
    out = tmp_path / "late"
    assert main(["backtest", "--runs", str(run), "--out", str(out)]) == 2
    assert "signal was not available between its close and next open" in capsys.readouterr().err
    assert not out.exists()
