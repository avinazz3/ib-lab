from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from ib_lab.entry import entry_levels, modeled_buy_fill

SIGNAL = datetime(2018, 7, 9, 14)


def test_entry_levels_snap_to_tick_from_observed_range():
    levels = entry_levels(100, 100.025, 98.995, 0.01)
    assert levels.trigger == Decimal("100.03")
    assert levels.limit == Decimal("100.13")
    assert levels.stop == Decimal("98.99")


@pytest.mark.parametrize("minutes", [-1, 0, 3])
def test_fill_rejects_signal_bar_and_expired_orders(minutes):
    assert (
        modeled_buy_fill(
            SIGNAL, SIGNAL + timedelta(minutes=minutes), 100, 100.02, 1000, 101, 0.01, 20
        )
        is None
    )


def test_fill_uses_executable_side_slippage_and_observed_volume():
    then = SIGNAL + timedelta(minutes=2)
    assert modeled_buy_fill(SIGNAL, then, 100, 100.02, 1099, 100.10, 0.01, 20) == (
        Decimal("100.08"),
        10,
    )
    assert modeled_buy_fill(SIGNAL, then, 100, 100.02, 1099, 100.07, 0.01, 20) is None
    assert modeled_buy_fill(SIGNAL, then, 101, 100.02, 1099, 100.10, 0.01, 20) is None
    assert modeled_buy_fill(SIGNAL, then, 100, 100.02, 99, 100.10, 0.01, 20) is None
    assert modeled_buy_fill(SIGNAL, then, 100, 100.02, 1099, 100.10, 0.01, 3) == (
        Decimal("100.08"),
        3,
    )
