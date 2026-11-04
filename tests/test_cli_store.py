import csv
import json
from pathlib import Path

import pytest

from ib_lab.cli import main
from ib_lab.models import read_universe
from ib_lab.store import read_snapshot


def test_cli_offline_workflow_and_snapshot_integrity(tmp_path):
    snapshot, output = tmp_path / "snapshot", tmp_path / "scan"
    assert main(["demo", "--out", str(snapshot)]) == 0
    assert (
        main(
            [
                "scan",
                "--snapshot",
                str(snapshot),
                "--as-of",
                "2026-10-30T22:00:00Z",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads((output / "report.json").read_text())
    assert report["synthetic"] is True
    assert report["candidate_count"] == 1
    assert report["historical_availability_verified"] is False
    assert "DEMO099" in (output / "candidates.csv").read_text()
    with (output / "candidates.csv").open() as handle:
        candidate = next(csv.DictReader(handle))
    assert candidate["symbol"] == "DEMO099"
    assert candidate["last_completed_session"] == "2026-10-30"
    assert candidate["check_rising_support"] == "True"
    assert candidate["relative_volume_session_count"] == "126"
    with (output / "candidate-events.csv").open() as handle:
        events = list(csv.DictReader(handle))
    assert 1 <= len(events) <= 6
    assert all(e["instrument_key"] == candidate["instrument_key"] for e in events)
    assert {e["date"] for e in events} == {
        e["date"] for e in report["results"][0]["research"]["events"]
    }
    # Existing snapshots cannot be overwritten even by a second demo command.
    assert main(["demo", "--out", str(snapshot)]) == 2
    with (snapshot / "bars.parquet").open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        read_snapshot(snapshot)


def test_review_config_and_empty_shortlist_have_valid_csv_headers(tmp_path):
    snapshot = tmp_path / "snapshot"
    assert main(["demo", "--out", str(snapshot)]) == 0
    config = json.loads((Path(__file__).parents[1] / "configs/breakout-review.json").read_text())
    config["min_price"] = 1_000_000
    path = tmp_path / "review.json"
    path.write_text(json.dumps(config))
    out = tmp_path / "empty"
    assert (
        main(
            [
                "scan",
                "--snapshot",
                str(snapshot),
                "--as-of",
                "2026-10-30T22:00:00Z",
                "--config",
                str(path),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads((out / "report.json").read_text())
    assert report["candidate_count"] == 0
    assert report["informational_checks"] == ["rising_support"]
    for name in ("candidates.csv", "candidate-events.csv"):
        with (out / name).open() as handle:
            reader = csv.DictReader(handle)
            assert "instrument_key" in reader.fieldnames
            assert list(reader) == []


def test_universe_requires_identity_and_unique_listings(tmp_path):
    path = tmp_path / "universe.csv"
    path.write_text("symbol,primary_exchange\nAAPL,NASDAQ\nAAPL,NASDAQ\n")
    with pytest.raises(ValueError, match="Duplicate"):
        read_universe(path)
    path.write_text("symbol\nAAPL\n")
    with pytest.raises(ValueError, match="headers"):
        read_universe(path)
    path.write_text("symbol,primary_exchange\n")
    assert read_universe(path, allow_empty=True) == []
    with pytest.raises(ValueError, match="empty"):
        read_universe(path)


def test_no_trading_commands_are_exposed():
    from ib_lab.cli import parser

    with pytest.raises(SystemExit):
        parser().parse_args(["trade"])


def test_sdk_request_errors_have_consistent_cli_exit_code(monkeypatch, capsys):
    from ib_async.wrapper import RequestError

    async def fail(_args):
        raise RequestError(1, 354, "No market data subscription")

    monkeypatch.setattr("ib_lab.cli.run_broker", fail)
    assert main(["probe", "--out", "unused"]) == 2
    assert "RequestError" in capsys.readouterr().err


def test_partial_download_keeps_successes_and_missing_instruments_visible(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ib_lab.demo import demo_data

    bars, instruments = demo_data()
    selected = instruments[:2]

    class FakeBroker:
        errors = []

        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def resolve(self, instrument):
            if instrument == selected[1]:
                raise ValueError("Missing subscription")
            return SimpleNamespace(conId=123)

        async def daily(self, instrument, _contract, _end):
            return [bar for bar in bars if bar.instrument_key == instrument.key]

    monkeypatch.setattr("ib_lab.cli.ReadOnlyBroker", FakeBroker)
    universe = tmp_path / "universe.csv"
    universe.write_text(
        "symbol,primary_exchange\n"
        + "".join(f"{item.symbol},{item.primary_exchange}\n" for item in selected)
    )
    snapshot = tmp_path / "partial"
    assert main(["download", "--universe", str(universe), "--out", str(snapshot)]) == 1
    rows, read_instruments, manifest = read_snapshot(snapshot)
    assert len(rows) == 160
    assert read_instruments == selected
    assert manifest["status"] == "partial"
    assert manifest["failures"][0]["instrument_key"] == selected[1].key
    output = tmp_path / "scan"
    assert (
        main(
            [
                "scan",
                "--snapshot",
                str(snapshot),
                "--as-of",
                "2026-10-30T22:00:00Z",
                "--out",
                str(output),
            ]
        )
        == 1
    )
    report = json.loads((output / "report.json").read_text())
    assert report["universe_count"] == 2
    assert report["data_error_count"] == 1
    assert report["download_failure_count"] == 1
    assert report["data_error_counts"] == {"download_failure": 1}
    failed = next(r for r in report["results"] if "download_failure" in r)
    assert failed["download_failure"]["error_type"] == "ValueError"


@pytest.mark.parametrize("passed,expected_code", [(False, 1), (True, 0)])
def test_require_live_keeps_diagnostic_report_and_returns_gate_result(
    tmp_path,
    monkeypatch,
    passed,
    expected_code,
):
    class Broker:
        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def probe(self, _instrument, data_type):
            assert data_type == 1
            return {"quote": {"live_quote_check_passed": passed, "execution_ready": False}}

    monkeypatch.setattr("ib_lab.cli.ReadOnlyBroker", Broker)
    out = tmp_path / "probe"
    assert main(["probe", "--require-live", "--out", str(out)]) == expected_code
    assert json.loads((out / "probe.json").read_text())["quote"]["execution_ready"] is False
    assert main(["probe", "--require-live", "--data-type", "3", "--out", str(out)]) == 2
