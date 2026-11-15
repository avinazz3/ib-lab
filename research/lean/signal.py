from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from AlgorithmImports import (
    DataNormalizationMode,
    QCAlgorithm,
    Resolution,
    Split,
    SplitType,
    Symbol,
    TimeZones,
    TradeBar,
)
from System import Array

from ib_lab.historical import adjust_known_splits
from ib_lab.models import DailyBar, Instrument
from ib_lab.screen import RESEARCH_SESSIONS, BreakoutConfig, screen_completed


@dataclass(frozen=True, kw_only=True)
class LeanInstrument(Instrument):
    security_id: str

    @property
    def key(self) -> str:
        return self.security_id


class LeanSignalReplay(QCAlgorithm):
    """Run one pre-open snapshot through the shared full-universe screen."""

    config: BreakoutConfig

    def initialize(self):
        self.set_start_date(2018, 7, 9)
        self.set_end_date(2018, 7, 9)
        self.set_time_zone(TimeZones.NEW_YORK)
        self.settings.daily_precise_end_time = True
        self.universe_settings.asynchronous = False
        self._clock = self.add_equity(
            "SPY", Resolution.MINUTE, fill_forward=False, extended_market_hours=True
        )
        self._members = {}
        self._membership_at = None
        self._replayed = False
        self.add_universe(self._capture_universe)

    def _capture_universe(self, rows):
        self._members = {str(row.symbol.id): row.symbol for row in rows if row.has_fundamental_data}
        self._membership_at = self.time
        return []  # Read bulk history by Symbol without subscribing to the whole universe.

    def on_data(self, data):
        cutoff = self.time
        if (
            self._replayed
            or self._membership_at is None
            or self._membership_at.date() != cutoff.date()
            or not time(9) <= cutoff.time() < time(9, 30)
            or self._clock.symbol not in data.bars
        ):
            return
        self._replay(cutoff)
        self._replayed = True

    def _replay(self, cutoff):
        day = cutoff
        completed = []
        for _ in range(RESEARCH_SESSIONS):
            day = self._clock.exchange.hours.get_previous_trading_day(day)
            completed.append(day.date())
        completed.reverse()

        instruments = []
        for key, symbol in sorted(self._members.items()):
            try:
                instruments.append(LeanInstrument(symbol.value, "USA", security_id=key))
            except ValueError:
                continue
        if not instruments:
            raise RuntimeError("No eligible universe identifiers for the pre-open replay")

        raw = defaultdict(list)
        splits = defaultdict(list)
        symbols = [self._members[instrument.key] for instrument in instruments]
        for offset in range(0, len(symbols), 128):
            batch = Array[Symbol](symbols[offset : offset + 128])
            # No fill-forward, no extended hours, default mapping and raw prices.
            options = (day, cutoff, Resolution.DAILY, False, False, None, DataNormalizationMode.RAW)
            for frame in self.history[TradeBar](batch, *options):
                for bar in frame.values():
                    if bar.end_time <= cutoff and bar.time.date() < cutoff.date():
                        key = str(bar.symbol.id)
                        raw[key].append(
                            DailyBar(
                                key,
                                bar.time.date(),
                                float(bar.open),
                                float(bar.high),
                                float(bar.low),
                                float(bar.close),
                                float(bar.volume),
                            )
                        )
            for frame in self.history[Split](batch, *options):
                for split in frame.values():
                    if split.type == SplitType.SPLIT_OCCURRED:
                        splits[str(split.symbol.id)].append((split.time, float(split.split_factor)))

        bars = [
            bar
            for key, rows in raw.items()
            for bar in adjust_known_splits(rows, splits[key], cutoff)
        ]
        report = screen_completed(
            bars,
            instruments,
            cutoff.replace(tzinfo=ZoneInfo("America/New_York")),
            self.config,
            completed,
            completed[0],
            raw_history=raw,
        )
        self._log_summary(report)

    def _log_summary(self, report):
        rows = report["results"]
        errors = Counter(row["data_error_kind"] for row in rows if "data_error_kind" in row)
        self.log(
            f"REPLAY decision={report['decision_at']} universe_at={self._membership_at} "
            f"last_completed={report['last_completed_session']} "
            f"members={len(self._members)} screened={report['universe_count']} "
            f"rank_eligible={report['rank_eligible_count']} candidates={report['candidate_count']} "
            f"config_sha256={report['config_sha256']}"
        )
        self.log(f"REPLAY data_errors={dict(sorted(errors.items()))}")
        for row in rows:
            if row["accepted"]:
                self.log(
                    f"REPLAY candidate={row['instrument']['symbol']} id={row['instrument_key']}"
                )

    def on_end_of_algorithm(self):
        if not self._replayed:
            self.error("REPLAY failed to observe current-day universe and pre-open clock data")
