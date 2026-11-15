from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

ENTRY_TIMEOUT = timedelta(minutes=2)


@dataclass(frozen=True)
class EntryLevels:
    trigger: Decimal
    limit: Decimal
    stop: Decimal


def entry_levels(base, opening_high, session_low, tick) -> EntryLevels:
    base, opening_high, session_low, tick = map(
        lambda value: Decimal(str(value)), (base, opening_high, session_low, tick)
    )
    trigger = (max(base, opening_high) // tick + 1) * tick
    limit = (trigger * Decimal("1.001") // tick) * tick
    stop = ((session_low / tick).to_integral_value(rounding=ROUND_CEILING) - 1) * tick
    return EntryLevels(trigger, limit, stop)


def modeled_buy_fill(
    submitted: datetime, observed: datetime, bid, ask, volume, limit, tick, remaining: int
) -> tuple[Decimal, int] | None:
    """Use only a later completed minute with trades and current two-sided quotes."""
    if not timedelta(0) < observed - submitted <= ENTRY_TIMEOUT or not 0 < bid <= ask:
        return None
    tick = Decimal(str(tick))
    slipped = Decimal(str(ask)) * Decimal("1.0005")
    price = (slipped / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    quantity = min(remaining, int(volume // 100))
    if price > Decimal(str(limit)) or quantity <= 0:
        return None
    return price, quantity
