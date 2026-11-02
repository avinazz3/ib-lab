from __future__ import annotations

from datetime import date, datetime, timedelta

import exchange_calendars as xcals


class Sessions:
    """NYSE calendar proxy for the initial regular-session US stock universe."""

    def __init__(self, first: date, last: date):
        self.calendar = xcals.get_calendar(
            "XNYS", start=first - timedelta(days=10), end=last + timedelta(days=10)
        )

    def dates(self, first: date, last: date) -> list[date]:
        return [stamp.date() for stamp in self.calendar.sessions_in_range(first, last)]

    def completed(self, first: date, decision_at: datetime) -> list[date]:
        return [
            day
            for day in self.dates(first, decision_at.date())
            if self.calendar.session_close(day).to_pydatetime() <= decision_at
        ]
