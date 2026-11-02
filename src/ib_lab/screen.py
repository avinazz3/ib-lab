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

STRATEGY_VERSION = "breakout-v0.1"
LOOKBACKS = (21, 63, 126)


@dataclass(frozen=True)
class BreakoutConfig:
    min_price: float = 5.0
    min_dollar_volume: float = 10_000_000.0
    leadership_percentile: float = 98.0
    min_impulse: float = 0.30
    max_base_depth: float = 0.20
    max_contraction: float = 0.70
    max_sma_distance_atr: float = 1.0

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) and v >= 0 for v in asdict(self).values()):
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
        try:
            if len({b.session for b in rows}) != len(rows):
                raise ValueError("Duplicate session")
            if any(not calendar.calendar.is_session(b.session) for b in rows):
                raise ValueError("Bar on a nontrading session")
            if not expected_set.issubset({b.session for b in rows}):
                raise ValueError("Missing recent sessions or insufficient history")
            for bar in rows:
                bar.validate()
            # Fixed warm-up length makes features independent of extra old history.
            f = features(rows[-127:])
            row["features"] = f
            checks = {
                "price": f["close"] >= config.min_price,
                "liquidity": f["dollar_volume20"] >= config.min_dollar_volume,
            }
            row["checks"] = checks
            if all(checks.values()):
                eligible.append(row)
            else:
                row["reasons"] = [key for key, passed in checks.items() if not passed]
        except ValueError as error:
            row["reasons"] = ["data_quality"]
            row["data_error"] = str(error)
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
        row["reasons"] = [key for key, passed in checks.items() if not passed]
        row["accepted"] = all(checks.values())
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
        "universe_count": len(instruments),
        "rank_eligible_count": len(eligible),
        "candidate_count": sum(row["accepted"] for row in results),
        "data_error_count": sum("data_error" in row for row in results),
        "warnings": warnings,
        "results": sorted(results, key=lambda row: (not row["accepted"], row["instrument_key"])),
    }
