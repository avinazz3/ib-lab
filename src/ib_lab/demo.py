from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

from ib_lab.calendar import Sessions
from ib_lab.models import DailyBar, Instrument
from ib_lab.store import new_directory, write_snapshot

DEMO_DECISION = datetime(2026, 10, 30, 22, tzinfo=UTC)


def demo_data() -> tuple[list[DailyBar], list[Instrument]]:
    days = Sessions(date(2026, 1, 1), DEMO_DECISION.date()).dates(
        date(2026, 1, 1), DEMO_DECISION.date()
    )[-160:]
    instruments = [Instrument(f"DEMO{i:03d}", "SYNTHETIC") for i in range(100)]
    rows = []
    for number, instrument in enumerate(instruments):
        for index, day in enumerate(days):
            if number == 99:
                # Strong prior impulse, followed by a base with shrinking daily ranges.
                close = 20 * (1.009 ** min(index, 144))
                if index >= 145:
                    close *= 1 + 0.0005 * (index - 144)
                half_range = 1.2 if index < 155 else 0.35
            else:
                close = 20 * (1 + 0.00002 * number) ** index
                half_range = 0.5 + (index % 3) * 0.1
            rows.append(
                DailyBar(
                    instrument.key,
                    day,
                    close,
                    close + half_range,
                    close - half_range,
                    close,
                    2_000_000,
                )
            )
    return rows, instruments


def write_demo(path: Path) -> None:
    rows, instruments = demo_data()
    new_directory(path)
    write_snapshot(
        path,
        rows,
        {
            "source": "synthetic",
            "synthetic": True,
            "status": "complete",
            "created_at": DEMO_DECISION.isoformat(),
            "decision_at": DEMO_DECISION.isoformat(),
            "price_basis": "synthetic_consistent",
            "use_rth": True,
            "universe_scope": "100 fabricated instruments; no real prices or performance evidence",
            "instruments": [{"instrument": asdict(item)} for item in instruments],
            "failures": [],
        },
    )
