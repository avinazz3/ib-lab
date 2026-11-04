from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import fmean

import numpy as np
import pandas as pd
import talib

from ib_lab.calendar import Sessions
from ib_lab.models import DailyBar, Instrument

STRATEGY_VERSION = "breakout-v0.2"
LOOKBACKS = (21, 63, 126)
RESEARCH_SESSIONS = 146  # 126 event sessions plus 20 preceding volume observations.


@dataclass(frozen=True)
class BreakoutConfig:
    min_price: float = 5.0
    min_dollar_volume: float = 10_000_000.0
    leadership_percentile: float = 98.0
    min_impulse: float = 0.30
    max_base_depth: float = 0.20
    max_contraction: float = 0.70
    max_sma_distance_atr: float = 1.0
    require_rising_support: bool = True

    def __post_init__(self) -> None:
        if type(self.require_rising_support) is not bool:
            raise ValueError("require_rising_support must be a boolean")
        numeric = {k: v for k, v in asdict(self).items() if k != "require_rising_support"}
        if not all(math.isfinite(v) and v >= 0 for v in numeric.values()):
            raise ValueError("Screen parameters must be finite, nonnegative numbers")
        if not 0 <= self.leadership_percentile <= 100:
            raise ValueError("leadership_percentile must be between 0 and 100")


def volatility(bars: list[DailyBar], period: int = 14) -> tuple[np.ndarray, float]:
    """TA-Lib supplies the gap-aware true range and Wilder ATR implementation."""
    if len(bars) < period + 1:
        raise ValueError("Insufficient bars for ATR")
    high, low, close = (
        np.array([getattr(b, field) for b in bars], dtype=float)
        for field in ("high", "low", "close")
    )
    return talib.TRANGE(high, low, close)[1:], float(talib.ATR(high, low, close, period)[-1])


def features(bars: list[DailyBar]) -> dict[str, float]:
    close = bars[-1].close
    base = bars[-15:]
    prior = bars[-58:-15]  # Base-preceding close and its preceding 42 sessions.
    tr, atr = volatility(bars)
    earlier_range = fmean(tr[-15:-5])
    if atr <= 0 or earlier_range <= 0:
        raise ValueError("Zero volatility prevents normalized feature calculation")
    sma20 = fmean(b.close for b in bars[-20:])
    return {
        "close": close,
        "volume20": fmean(b.volume for b in bars[-20:]),
        "dollar_volume20": fmean(b.close * b.volume for b in bars[-20:]),
        **{f"return_{n}": close / bars[-n - 1].close - 1 for n in LOOKBACKS},
        "prior_impulse": prior[-1].close / min(b.close for b in prior) - 1,
        "base_ceiling": max(b.high for b in base),
        "base_depth": (max(b.high for b in base) - min(b.low for b in base)) / close,
        "contraction": fmean(tr[-5:]) / earlier_range,
        "support_change": min(b.low for b in base[-5:]) - min(b.low for b in base[:5]),
        "atr14": atr,
        "sma20": sma20,
        "sma20_change5": sma20 - fmean(b.close for b in bars[-25:-5]),
        "sma_distance_atr": abs(close - sma20) / atr,
    }


def research_summary(bars: list[DailyBar], sessions: list) -> dict:
    """Dated observations only; missing volume context never becomes a partial mean."""
    frame = pd.DataFrame([asdict(b) for b in bars]).set_index("session").reindex(sessions)
    previous_close = frame["close"].shift(1)
    frame["price_change"] = frame["close"] / previous_close - 1
    frame["opening_gap"] = frame["open"] / previous_close - 1
    volume_mean = frame["volume"].shift(1).rolling(20, min_periods=20).mean()
    frame["relative_volume"] = frame["volume"] / volume_mean.replace(0, np.nan)
    window = frame.iloc[-126:]
    price_dates = window["price_change"].abs().dropna().nlargest(3).index
    volume_dates = window["relative_volume"].dropna().nlargest(3).index
    events = []
    for day in sorted(set(price_dates) | set(volume_dates)):
        row = window.loc[day]
        events.append(
            {
                "date": day.isoformat(),
                "price_change": float(row["price_change"]),
                "opening_gap": float(row["opening_gap"]),
                "volume": float(row["volume"]),
                "relative_volume": (
                    float(row["relative_volume"]) if pd.notna(row["relative_volume"]) else None
                ),
                "selected_for": [
                    name
                    for name, dates in (
                        ("price_change", price_dates),
                        ("relative_volume", volume_dates),
                    )
                    if day in dates
                ],
            }
        )
    return {
        "return_start_dates": {str(n): sessions[-n - 1].isoformat() for n in LOOKBACKS},
        "event_window_start": window.index[0].isoformat(),
        "event_window_end": window.index[-1].isoformat(),
        "relative_volume_session_count": int(window["relative_volume"].notna().sum()),
        "events": events,
    }


def screen(
    bars: list[DailyBar],
    instruments: list[Instrument],
    decision_at: datetime,
    config: BreakoutConfig,
) -> dict:
    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueError("decision_at must be timezone aware")
    # Bound calendar and all calculations by decision time, not a future row's date.
    start = decision_at.date() - timedelta(days=550)
    calendar = Sessions(start, decision_at.date())
    completed = calendar.completed(start, decision_at)
    expected = completed[-127:]
    expected_set = set(expected)
    if len(expected) < 127:
        raise ValueError("Calendar does not cover 127 completed sessions")
    grouped: dict[str, list[DailyBar]] = defaultdict(list)
    for bar in bars:
        if start <= bar.session <= expected[-1]:
            grouped[bar.instrument_key].append(bar)
    results = []
    eligible = []
    histories = {}
    informational = [] if config.require_rising_support else ["rising_support"]
    for instrument in instruments:
        row = {
            "instrument": asdict(instrument),
            "instrument_key": instrument.key,
            "accepted": False,
            "reasons": [],
            "features": {},
            "checks": {},
        }
        rows = sorted(grouped[instrument.key], key=lambda b: b.session)
        missing = sorted(expected_set - {b.session for b in rows})
        row["history"] = {
            "required_sessions": 127,
            "available_required_sessions": 127 - len(missing),
            "first_available_session": rows[0].session.isoformat() if rows else None,
            "latest_available_session": rows[-1].session.isoformat() if rows else None,
            "missing_session_count": len(missing),
            "missing_session_sample": [day.isoformat() for day in missing[:5]],
        }
        error_kind = "duplicate_sessions"
        try:
            if len({b.session for b in rows}) != len(rows):
                raise ValueError("Duplicate session")
            error_kind = "nontrading_sessions"
            if any(not calendar.calendar.is_session(b.session) for b in rows):
                raise ValueError("Bar on a nontrading session")
            error_kind = "no_history" if not rows else "missing_sessions"
            if (
                rows
                and rows[-1].session == expected[-1]
                and all(day < rows[0].session for day in missing)
            ):
                error_kind = "insufficient_history"
            if missing:
                raise ValueError("Missing recent sessions or insufficient history")
            error_kind = "invalid_bars"
            for bar in rows:
                bar.validate()
            # Fixed warm-up length makes features independent of extra old history.
            error_kind = "feature_error"
            f = features(rows[-127:])
            row["features"] = f
            checks = {
                "price": f["close"] >= config.min_price,
                "liquidity": f["dollar_volume20"] >= config.min_dollar_volume,
            }
            row["checks"] = checks
            if all(checks.values()):
                eligible.append(row)
                histories[instrument.key] = rows[-RESEARCH_SESSIONS:]
            else:
                row["reasons"] = [key for key, passed in checks.items() if not passed]
        except ValueError as error:
            row["reasons"] = ["data_quality"]
            row["data_error"] = str(error)
            row["data_error_kind"] = error_kind
        results.append(row)
    percentiles = (
        {
            n: (
                pd.Series([row["features"][f"return_{n}"] for row in eligible], dtype=float).rank(
                    method="average"
                )
                - 0.5
            )
            * 100
            / len(eligible)
            for n in LOOKBACKS
        }
        if eligible
        else {}
    )
    for index, row in enumerate(eligible):
        f = row["features"]
        for n in LOOKBACKS:
            f[f"percentile_{n}"] = float(percentiles[n].iloc[index])
        checks = {
            **row["checks"],
            "leadership": max(f[f"percentile_{n}"] for n in LOOKBACKS)
            >= config.leadership_percentile,
            "impulse": f["prior_impulse"] >= config.min_impulse,
            "base_depth": f["base_depth"] <= config.max_base_depth,
            "contraction": f["contraction"] <= config.max_contraction,
            "rising_support": f["support_change"] > 0,
            "sma_proximity": f["sma_distance_atr"] <= config.max_sma_distance_atr,
            "rising_sma": f["sma20_change5"] > 0,
        }
        row["checks"] = checks
        row["reasons"] = [
            key for key, passed in checks.items() if not passed and key not in informational
        ]
        row["accepted"] = not row["reasons"]
        if row["accepted"]:
            row["research"] = research_summary(
                histories[row["instrument_key"]], completed[-RESEARCH_SESSIONS:]
            )
    payload = json.dumps(asdict(config), sort_keys=True, allow_nan=False).encode()
    warnings = ["Rankings are relative to the supplied, data-complete, liquid universe."]
    if len(eligible) < 50:
        warnings.append("Fewer than 50 rank-eligible instruments; leadership is under-resolved.")
    return {
        "strategy_version": STRATEGY_VERSION,
        "decision_at": decision_at.isoformat(),
        "last_completed_session": str(expected[-1]),
        "config": asdict(config),
        "config_sha256": hashlib.sha256(payload).hexdigest(),
        "informational_checks": informational,
        "universe_count": len(instruments),
        "rank_eligible_count": len(eligible),
        "candidate_count": sum(row["accepted"] for row in results),
        "data_error_count": sum("data_error" in row for row in results),
        "warnings": warnings,
        "results": sorted(results, key=lambda row: (not row["accepted"], row["instrument_key"])),
    }
