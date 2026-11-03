import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ib_lab.demo import DEMO_DECISION, demo_data
from ib_lab.models import DailyBar
from ib_lab.screen import BreakoutConfig, research_summary, screen, volatility


@pytest.fixture(scope="module")
def data():
    return demo_data()


def test_end_to_end_known_candidate_and_rejection_reasons(data):
    bars, instruments = data
    result = screen(bars, instruments, DEMO_DECISION, BreakoutConfig())
    assert result["candidate_count"] == 1
    candidate = next(row for row in result["results"] if row["accepted"])
    assert candidate["instrument"]["symbol"] == "DEMO099"
    assert candidate["reasons"] == []
    assert result["rank_eligible_count"] == 100
    assert all(row["reasons"] for row in result["results"] if not row["accepted"])


def test_future_and_incomplete_daily_bars_cannot_change_past_scan(data):
    bars, instruments = data
    expected = screen(bars, instruments, DEMO_DECISION, BreakoutConfig())
    future = [replace(bar, session=date(2026, 11, 2), close=float("nan")) for bar in bars[-5:]]
    assert screen(bars + future, instruments, DEMO_DECISION, BreakoutConfig()) == expected


def test_incomplete_session_excluded_even_when_daily_bar_is_present(data):
    bars, instruments = data
    during_session = datetime(2026, 10, 30, 15, tzinfo=UTC)
    baseline = screen(bars, instruments, during_session, BreakoutConfig())
    changed = [replace(b, close=1_000_000) if b.session == date(2026, 10, 30) else b for b in bars]
    assert screen(changed, instruments, during_session, BreakoutConfig()) == baseline
    assert baseline["last_completed_session"] == "2026-10-29"


def test_missing_session_is_reported_and_excluded_from_ranking(data):
    bars, instruments = data
    key = instruments[-1].key
    damaged = [b for b in bars if not (b.instrument_key == key and b.session == date(2026, 10, 29))]
    report = screen(damaged, instruments, DEMO_DECISION, BreakoutConfig())
    row = next(r for r in report["results"] if r["instrument_key"] == key)
    assert row["reasons"] == ["data_quality"]
    assert not row["accepted"]
    assert report["rank_eligible_count"] == 99
    assert report["data_error_count"] == 1


@pytest.mark.parametrize(
    "damage",
    [
        {"volume": -1},
        {"close": float("nan")},
        {"high": 0},
        {"low": 10_000},
    ],
)
def test_invalid_recent_ohlcv_never_becomes_a_candidate(data, damage):
    bars, instruments = data
    damaged = [*bars[:-1], replace(bars[-1], **damage)]
    result = screen(damaged, instruments, DEMO_DECISION, BreakoutConfig())
    assert result["data_error_count"] == 1
    assert result["candidate_count"] == 0


def test_duplicate_session_does_not_silently_double_volume(data):
    bars, instruments = data
    report = screen([*bars, bars[-1]], instruments, DEMO_DECISION, BreakoutConfig())
    assert report["data_error_count"] == 1
    assert report["candidate_count"] == 0


def test_ties_and_tiny_universes_do_not_all_become_top_percentile(data):
    bars, instruments = data
    template = [b for b in bars if b.instrument_key == instruments[0].key]
    identical = [replace(b, instrument_key=i.key) for i in instruments for b in template]
    for universe in (instruments[:1], instruments):
        report = screen(identical, universe, DEMO_DECISION, BreakoutConfig())
        assert report["candidate_count"] == 0
        assert all(r["features"]["percentile_21"] == 50 for r in report["results"])


def test_liquidity_exclusions_are_not_in_ranking_denominator(data):
    bars, instruments = data
    liquid_key = instruments[-1].key
    changed = [b if b.instrument_key == liquid_key else replace(b, volume=0) for b in bars]
    report = screen(changed, instruments, DEMO_DECISION, BreakoutConfig())
    assert report["rank_eligible_count"] == 1
    assert report["candidate_count"] == 0


def test_wilder_atr_uses_gaps_and_recursive_smoothing():
    bars = [
        DailyBar("x", date(2026, 1, 1) + timedelta(days=i), 10, 11, 9, 10, 1) for i in range(15)
    ]
    assert volatility(bars)[1] == 2
    bars.append(DailyBar("x", date(2026, 1, 16), 14, 15, 13, 14, 1))
    assert volatility(bars)[1] == pytest.approx((13 * 2 + 5) / 14)


def test_naive_timestamps_and_invalid_config_rejected(data):
    with pytest.raises(ValueError, match="timezone"):
        screen(*data, datetime(2026, 10, 30), BreakoutConfig())
    with pytest.raises(ValueError):
        BreakoutConfig(leadership_percentile=101)
    with pytest.raises(ValueError):
        BreakoutConfig(max_contraction=float("nan"))
    with pytest.raises(ValueError, match="boolean"):
        BreakoutConfig(require_rising_support="false")


@pytest.mark.parametrize(
    "changes,strict_pass,review_pass",
    [
        ({"base_depth": 0.20}, True, True),
        ({"base_depth": 0.25}, False, True),
        ({"base_depth": 0.250001}, False, False),
        ({"contraction": 0.70}, True, True),
        ({"contraction": 1.0}, False, True),
        ({"contraction": 1.000001}, False, False),
        ({"sma_distance_atr": 1.0}, True, True),
        ({"sma_distance_atr": 2.0}, False, True),
        ({"sma_distance_atr": 2.000001}, False, False),
        ({"support_change": 0}, False, True),
        ({"support_change": -1}, False, True),
        ({"prior_impulse": 0.299999}, False, False),
        ({"sma20_change5": 0}, False, False),
        ({"close": 4.99}, False, False),
        ({"dollar_volume20": 9_999_999}, False, False),
    ],
)
def test_review_relaxes_only_approved_checks(data, monkeypatch, changes, strict_pass, review_pass):
    from ib_lab.screen import features

    bars, instruments = data
    key = instruments[-1].key

    def changed_features(rows):
        result = features(rows)
        return {**result, **changes} if rows[-1].instrument_key == key else result

    monkeypatch.setattr("ib_lab.screen.features", changed_features)
    config_dir = Path(__file__).parents[1] / "configs"
    for name, expected in (("breakout.json", strict_pass), ("breakout-review.json", review_pass)):
        config = BreakoutConfig(**json.loads((config_dir / name).read_text()))
        report = screen(bars, instruments, DEMO_DECISION, config)
        row = next(r for r in report["results"] if r["instrument_key"] == key)
        assert row["accepted"] is expected
        if name == "breakout-review.json":
            assert report["informational_checks"] == ["rising_support"]
            assert "rising_support" not in row["reasons"]
            if "support_change" in changes:
                assert row["checks"]["rising_support"] is False


def test_event_dates_and_volume_baseline_exclude_current_and_missing_sessions(data):
    bars, instruments = data
    template = [b for b in bars if b.instrument_key == instruments[-1].key][-146:]
    rows = []
    for i, bar in enumerate(template):
        price = {30: 20, 90: 30}.get(i, 10)
        volume = {30: 1000, 100: 2000, 145: 500}.get(i, 100)
        rows.append(replace(bar, open=price, high=price, low=price, close=price, volume=volume))
    sessions = [b.session for b in rows]
    result = research_summary(rows, sessions)
    events = {e["date"]: e for e in result["events"]}
    assert list(events) == [sessions[i].isoformat() for i in (30, 90, 91, 100, 145)]
    assert events[sessions[30].isoformat()]["selected_for"] == ["price_change", "relative_volume"]
    assert events[sessions[30].isoformat()]["price_change"] == 1
    assert events[sessions[91].isoformat()]["opening_gap"] == pytest.approx(-2 / 3)
    assert events[sessions[100].isoformat()]["relative_volume"] == 20
    assert events[sessions[145].isoformat()]["relative_volume"] == 5
    assert result["relative_volume_session_count"] == 126
    assert result["return_start_dates"]["126"] == sessions[19].isoformat()
    # Old daily snapshots have 127 bars: retain price events, disclose shorter volume context.
    assert research_summary(rows[-127:], sessions)["relative_volume_session_count"] == 107
    missing = [b for i, b in enumerate(rows) if i != 10]
    assert research_summary(missing, sessions)["relative_volume_session_count"] == 115
    zero = [replace(b, volume=0) for b in rows]
    result = research_summary(zero, sessions)
    assert result["relative_volume_session_count"] == 0
    assert all(e["relative_volume"] is None for e in result["events"])
