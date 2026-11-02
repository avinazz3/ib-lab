import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ib_lab.broker import ConnectionSettings, ReadOnlyBroker
from ib_lab.models import Instrument


def fake_ib():
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.qualifyContractsAsync = AsyncMock()
    ib.reqHistoricalDataAsync = AsyncMock()
    return ib


def test_connection_is_readonly_and_closes_on_exception():
    async def check():
        ib = fake_ib()
        with pytest.raises(RuntimeError):
            async with ReadOnlyBroker(ConnectionSettings(), ib):
                raise RuntimeError("test interrupted")
        assert ib.connectAsync.await_args.kwargs["readonly"] is True
        assert ib.connectAsync.await_args.kwargs["clientId"] > 0
        ib.disconnect.assert_called_once()
        ib.placeOrder.assert_not_called()

    asyncio.run(check())


def test_failed_connection_also_disconnects():
    async def check():
        ib = fake_ib()
        ib.connectAsync.side_effect = TimeoutError()
        with pytest.raises(TimeoutError):
            async with ReadOnlyBroker(ConnectionSettings(), ib):
                pass
        ib.disconnect.assert_called_once()

    asyncio.run(check())


def test_missing_contract_and_empty_history_are_explicit_failures():
    async def check():
        ib = fake_ib()
        ib.qualifyContractsAsync.return_value = [None]
        broker = ReadOnlyBroker(ConnectionSettings(request_spacing=0.25), ib)
        with pytest.raises(ValueError, match="resolve"):
            await broker.resolve(Instrument("AAPL", "NASDAQ"))
        ib.reqHistoricalDataAsync.return_value = []
        with pytest.raises(ValueError, match="No historical"):
            await broker.historical(SimpleNamespace(conId=1), datetime.now(UTC))

    asyncio.run(check())


def test_quote_subscription_is_cancelled_and_delayed_type_is_preserved():
    async def check():
        ib = fake_ib()
        ib.reqMktData.return_value = SimpleNamespace(
            bid=10.0, ask=10.1, last=10.05, marketDataType=3, time=datetime.now(UTC)
        )
        contract = SimpleNamespace(conId=1)
        broker = ReadOnlyBroker(ConnectionSettings(), ib)
        quote = await broker.quote_probe(contract, 3)
        assert quote["observed_data_type"] == 3
        assert quote["execution_ready"] is False
        ib.cancelMktData.assert_called_once_with(contract)
        ib.placeOrder.assert_not_called()

    asyncio.run(check())


def test_invalid_pacing_and_reserved_client_id_are_rejected():
    with pytest.raises(ValueError):
        ConnectionSettings(client_id=0)
    with pytest.raises(ValueError):
        ConnectionSettings(request_spacing=0)


def test_quote_subscription_is_cancelled_on_task_cancellation():
    async def check():
        ib = fake_ib()
        ib.reqMktData.return_value = SimpleNamespace(
            bid=float("nan"),
            ask=float("nan"),
            last=float("nan"),
            marketDataType=1,
            time=None,
        )
        contract = SimpleNamespace(conId=1)
        broker = ReadOnlyBroker(ConnectionSettings(), ib)
        task = asyncio.create_task(broker.quote_probe(contract))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ib.cancelMktData.assert_called_once_with(contract)

    asyncio.run(check())
