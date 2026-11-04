import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eventkit import Event
from tenacity import wait_none

from ib_lab.broker import (
    BrokerUnavailableError,
    ConnectionSettings,
    EmptyHistoryError,
    ReadOnlyBroker,
)
from ib_lab.models import Instrument


def fake_ib():
    ib = MagicMock()
    ib.isConnected.return_value = False
    ib.errorEvent = Event()
    ib.disconnectedEvent = Event()
    ib.errorEvent += ib._onError

    async def connect(*_args, **_kwargs):
        ib.isConnected.return_value = True

    def disconnect():
        ib.isConnected.return_value = False
        ib.disconnectedEvent.emit()

    ib.connectAsync = AsyncMock(side_effect=connect)
    ib.disconnect.side_effect = disconnect
    ib.qualifyContractsAsync = AsyncMock()
    ib.reqHistoricalDataAsync = AsyncMock()
    return ib


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr("ib_lab.broker.wait_exponential", lambda **_kwargs: wait_none())


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
        with pytest.raises(BrokerUnavailableError):
            async with ReadOnlyBroker(ConnectionSettings(), ib):
                pass
        ib.disconnect.assert_called_once()
        assert ib.connectAsync.await_count == 3

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
        while not ib.reqMktData.called:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ib.cancelMktData.assert_called_once_with(contract)

    asyncio.run(check())


def test_upstream_outage_pauses_requests_and_1102_does_not_fetch_accounts():
    async def check():
        from ib_async import IB

        ib = fake_ib()
        ib.errorEvent -= ib._onError
        ib._onError = IB._onError.__get__(ib, IB)
        ib.errorEvent += ib._onError
        broker = ReadOnlyBroker(ConnectionSettings(recovery_timeout=0.1), ib)
        ib.reqHistoricalDataAsync.return_value = ["restored"]
        async with broker:
            ib.errorEvent.emit(-1, 1100, "lost", None)
            task = asyncio.create_task(
                broker.historical(SimpleNamespace(conId=1), datetime.now(UTC))
            )
            await asyncio.sleep(0.01)
            ib.reqHistoricalDataAsync.assert_not_awaited()
            ib.errorEvent.emit(-1, 1102, "restored", None)
            ib.errorEvent.emit(-1, 1102, "restored", None)
            assert await task == ["restored"]
            ib.reqAccountSummaryAsync.assert_not_called()
        assert len(ib.errorEvent) == 1

    asyncio.run(check())


def test_response_during_connection_change_is_discarded_and_retried():
    async def check():
        ib = fake_ib()
        broker = ReadOnlyBroker(ConnectionSettings(request_spacing=0.25), ib)

        async def respond(*_args, **_kwargs):
            if ib.reqHistoricalDataAsync.await_count == 1:
                ib.errorEvent.emit(-1, 1100, "lost", None)
                asyncio.get_running_loop().call_soon(ib.errorEvent.emit, -1, 1101, "restored", None)
                return ["discard this response"]
            return ["fresh response"]

        ib.reqHistoricalDataAsync.side_effect = respond
        async with broker:
            assert await broker.historical(SimpleNamespace(conId=1), datetime.now(UTC)) == [
                "fresh response"
            ]
        assert ib.reqHistoricalDataAsync.await_count == 2
        assert broker.retries[0]["error_type"] == "ConnectionError"
        assert broker.retries[0]["operation"] == "historical:1"

    asyncio.run(check())


def test_socket_disconnect_reconnects_readonly_and_unrecovered_upstream_stops():
    async def check():
        ib = fake_ib()
        ib.reqHistoricalDataAsync.return_value = ["bar"]
        broker = ReadOnlyBroker(ConnectionSettings(recovery_timeout=0.01), ib)
        async with broker:
            ib.disconnect()
            await broker.historical(SimpleNamespace(conId=1), datetime.now(UTC))
            assert ib.connectAsync.await_count == 2
            assert all(c.kwargs["readonly"] for c in ib.connectAsync.await_args_list)
            ib.errorEvent.emit(-1, 1100, "lost", None)
            with pytest.raises(BrokerUnavailableError, match="did not recover"):
                await broker.historical(SimpleNamespace(conId=2), datetime.now(UTC))
            assert ib.reqHistoricalDataAsync.await_count == 1

    asyncio.run(check())


def test_socket_disconnect_wakes_a_request_already_waiting_for_upstream():
    async def check():
        ib = fake_ib()
        ib.reqHistoricalDataAsync.return_value = ["bar"]
        async with ReadOnlyBroker(ConnectionSettings(recovery_timeout=0.1), ib) as broker:
            ib.errorEvent.emit(-1, 1100, "lost upstream", None)
            task = asyncio.create_task(
                broker.historical(SimpleNamespace(conId=1), datetime.now(UTC))
            )
            await asyncio.sleep(0.01)
            ib.reqHistoricalDataAsync.assert_not_awaited()
            ib.disconnect()
            assert await task == ["bar"]
            assert ib.connectAsync.await_count == 2

    asyncio.run(check())


def test_transient_retries_are_bounded_and_permission_errors_are_not_retried():
    async def check():
        from ib_async.wrapper import RequestError

        ib = fake_ib()
        broker = ReadOnlyBroker(ConnectionSettings(max_attempts=2, request_spacing=0.25), ib)
        async with broker:
            ib.reqHistoricalDataAsync.side_effect = RequestError(1, 354, "Not subscribed")
            with pytest.raises(RequestError):
                await broker.historical(SimpleNamespace(conId=1), datetime.now(UTC))
            assert ib.reqHistoricalDataAsync.await_count == 1
            ib.reqHistoricalDataAsync.side_effect = None
            ib.reqHistoricalDataAsync.return_value = []
            with pytest.raises(EmptyHistoryError):
                await broker.historical(SimpleNamespace(conId=2), datetime.now(UTC))
            ib.reqHistoricalDataAsync.side_effect = TimeoutError()
            for con_id in (3, 4):
                with pytest.raises(TimeoutError):
                    await broker.historical(SimpleNamespace(conId=con_id), datetime.now(UTC))
            with pytest.raises(BrokerUnavailableError, match="Three consecutive"):
                await broker.historical(SimpleNamespace(conId=5), datetime.now(UTC))
        assert ib.reqHistoricalDataAsync.await_count == 9
        assert len(broker.retries) == 4

    asyncio.run(check())


@pytest.mark.parametrize(
    "data_type,age,passed",
    [(1, 0, True), (1, 10, False), (1, -10, False), (2, 0, False), (3, 0, False), (4, 0, False)],
)
def test_live_probe_requires_live_type_and_recent_receive_time(data_type, age, passed):
    async def check():
        ib = fake_ib()
        ib.reqMktData.return_value = SimpleNamespace(
            bid=10.0,
            ask=10.1,
            last=10.05,
            marketDataType=data_type,
            time=datetime.now(UTC) - timedelta(seconds=age),
        )
        broker = ReadOnlyBroker(ConnectionSettings(), ib)
        quote = await broker.quote_probe(SimpleNamespace(conId=1), 1)
        assert quote["live_quote_check_passed"] is passed
        assert quote["spread"] == pytest.approx(0.1)
        assert quote["exchange_freshness_verified"] is False
        assert quote["execution_ready"] is False

    asyncio.run(check())
