from datetime import UTC, date, datetime

from ib_lab.calendar import Sessions


def test_holiday_early_close_and_dst_are_exchange_calendar_driven():
    sessions = Sessions(date(2026, 10, 1), date(2026, 12, 1))
    assert date(2026, 11, 26) not in sessions.dates(date(2026, 11, 25), date(2026, 11, 27))
    assert sessions.completed(date(2026, 11, 25), datetime(2026, 11, 27, 17, 59, tzinfo=UTC))[
        -1
    ] == date(2026, 11, 25)
    assert sessions.completed(date(2026, 11, 25), datetime(2026, 11, 27, 18, tzinfo=UTC))[
        -1
    ] == date(2026, 11, 27)
    assert sessions.calendar.session_close("2026-10-30").hour == 20
    assert sessions.calendar.session_close("2026-11-02").hour == 21
