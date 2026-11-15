from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from ib_lab.models import DailyBar


def adjust_known_splits(
    raw: list[DailyBar], splits: list[tuple[datetime, float]], cutoff: datetime
) -> list[DailyBar]:
    """Put prior bars on the share basis at cutoff, using only effective splits."""
    actions = sorted((at, factor) for at, factor in splits if at <= cutoff)
    if not actions:
        return raw
    if len({at for at, _ in actions}) != len(actions) or any(
        factor <= 0 or not math.isfinite(factor) for _, factor in actions
    ):
        raise ValueError("Invalid or duplicate effective split")
    adjusted = []
    for bar in raw:
        scale = math.prod(factor for at, factor in actions if bar.session < at.date())
        adjusted.append(
            replace(
                bar,
                open=bar.open * scale,
                high=bar.high * scale,
                low=bar.low * scale,
                close=bar.close * scale,
                volume=bar.volume / scale,
            )
        )
    return adjusted
