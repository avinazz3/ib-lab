from collections import Counter

from AlgorithmImports import (
    DataNormalizationMode,
    QCAlgorithm,
    Resolution,
    Slice,
    TimeZones,
    TradeBar,
)


class BreakoutDataAudit(QCAlgorithm):
    """Check historical data access and timing without placing orders."""

    def initialize(self) -> None:
        self.set_start_date(2018, 7, 9)
        self.set_end_date(2018, 7, 13)
        self.set_time_zone(TimeZones.NEW_YORK)
        self.settings.daily_precise_end_time = True
        self._counts = Counter()
        self._universe_dates = set()
        self._symbols = [
            self.add_equity(
                ticker,
                Resolution.MINUTE,
                fill_forward=False,
                extended_market_hours=False,
                data_normalization_mode=DataNormalizationMode.RAW,
            ).symbol
            for ticker in ("AAPL", "TWTR")
        ]
        self.add_universe(self._audit_universe)
        for symbol in self._symbols:
            history = list(self.history[TradeBar](symbol, 127, Resolution.DAILY))
            first = history[0].end_time if history else None
            last = history[-1].end_time if history else None
            self.log(
                f"AUDIT history {symbol.value} {symbol.id} cutoff={self.time} "
                f"bars={len(history)} first={first} last={last}"
            )

    def _audit_universe(self, rows):
        day = self.time.date()
        self._universe_dates.add(day)
        members = {row.symbol for row in rows if row.has_fundamental_data}
        present = ",".join(s.value for s in self._symbols if s in members)
        self.log(f"AUDIT universe {self.time} fundamentals={len(members)} sample={present}")
        return []

    def on_data(self, data: Slice) -> None:
        for symbol in self._symbols:
            if symbol in data.bars:
                bar = data.bars[symbol]
                day = bar.time.date()
                self._counts[day, symbol, "trade"] += 1
                if bar.time.hour == 9 and 30 <= bar.time.minute < 35:
                    self._counts[day, symbol, "opening_range"] += 1
            if symbol in data.quote_bars:
                quote = data.quote_bars[symbol]
                if quote.bid and quote.ask and 0 < quote.bid.close <= quote.ask.close:
                    self._counts[quote.time.date(), symbol, "two_sided"] += 1

    def on_end_of_algorithm(self) -> None:
        for day in sorted(self._universe_dates | {day for day, _, _ in self._counts}):
            for symbol in self._symbols:
                trade = self._counts[day, symbol, "trade"]
                opening = self._counts[day, symbol, "opening_range"]
                quotes = self._counts[day, symbol, "two_sided"]
                self.log(
                    f"AUDIT intraday {day} {symbol.value} trade_minutes={trade} "
                    f"opening_range_minutes={opening} two_sided_quote_minutes={quotes}"
                )
