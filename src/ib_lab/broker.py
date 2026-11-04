from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import version
from time import monotonic

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from ib_lab.calendar import Sessions
from ib_lab.models import DailyBar, Instrument


class BrokerUnavailableError(ConnectionError):
    """A sustained outage must stop collection rather than fail the whole universe."""


class EmptyHistoryError(ValueError):
    """The SDK returns an empty result for both some timeouts and empty histories."""


def retryable(error: BaseException) -> bool:
    if isinstance(error, BrokerUnavailableError):
        return False
    if isinstance(error, (ConnectionError, TimeoutError, EmptyHistoryError)):
        return True
    return getattr(error, "errorCode", None) == 162 and any(
        text in str(error).lower() for text in ("pacing", "query cancelled", "connection")
    )


def failure_details(error: BaseException) -> dict:
    result = {"error_type": type(error).__name__, "error": str(error) or type(error).__name__}
    if getattr(error, "errorCode", None) is not None:
        result["error_code"] = error.errorCode
    return result


@dataclass(frozen=True)
class ConnectionSettings:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 31
    timeout: float = 20
    request_spacing: float = 1.0
    max_attempts: int = 3
    recovery_timeout: float = 60

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535 or self.client_id <= 0:
            raise ValueError("Use a valid port and a positive, unique client ID")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not math.isfinite(self.request_spacing) or self.request_spacing < 0.25:
            raise ValueError("request spacing must be finite and at least 0.25 seconds")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not math.isfinite(self.recovery_timeout) or self.recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be finite and positive")


class ReadOnlyBroker:
    """Only data operations are exposed. Also enable Read-Only API in TWS itself."""

    def __init__(self, settings: ConnectionSettings, ib=None):
        # Import inside the running async context, including on newer Python runtimes.
        from ib_async import IB

        self.settings = settings
        self.ib = ib if ib is not None else IB()
        self.ib.RaiseRequestErrors = True
        self.errors: list[dict] = []
        self.retries: list[dict] = []
        self._last_request = -math.inf
        self._ready = asyncio.Event()
        self._ready.set()
        self._upstream_lost = False
        self._generation = 0
        self._failed_requests = {"resolve": 0, "historical": 0}

    def _error(self, req_id, code, message, contract=None) -> None:
        self.errors.append({"request_id": req_id, "code": code, "message": message})
        if code == 1100:
            self._upstream_lost = True
            self._ready.clear()
            self._generation += 1
        elif code in (1101, 1102):
            self._upstream_lost = False
            self._ready.set()
            self._generation += 1

    def _disconnected(self) -> None:
        self._ready.set()  # Wake an upstream-loss waiter so it can reconnect the socket.
        self._generation += 1

    def _retrying(self, operation: str):
        def record(state):
            item = {
                "operation": operation,
                "failed_attempt": state.attempt_number,
                "retry_in_seconds": state.next_action.sleep,
                **failure_details(state.outcome.exception()),
            }
            self.retries.append(item)
            logging.getLogger(__name__).warning("Retrying %s: %s", operation, item)

        return AsyncRetrying(
            stop=stop_after_attempt(self.settings.max_attempts),
            wait=wait_exponential(multiplier=2, max=10),
            retry=retry_if_exception(retryable),
            before_sleep=record,
            reraise=True,
        )

    async def _connect(self):
        from ib_async import StartupFetchNONE

        try:
            async for attempt in self._retrying("connect"):
                with attempt:
                    if self.ib.isConnected():
                        self.ib.disconnect()
                    self._upstream_lost = False
                    self._ready.clear()
                    await self.ib.connectAsync(
                        self.settings.host,
                        self.settings.port,
                        clientId=self.settings.client_id,
                        timeout=self.settings.timeout,
                        readonly=True,
                        raiseSyncErrors=True,
                        fetchFields=StartupFetchNONE,
                    )
                    if not self._upstream_lost:
                        self._ready.set()
        except Exception as error:
            raise BrokerUnavailableError(
                "Could not connect to IBKR after bounded attempts"
            ) from error

    async def _wait_ready(self):
        deadline = monotonic() + self.settings.recovery_timeout
        while True:
            if not self.ib.isConnected():
                await self._connect()
            if not self._upstream_lost:
                return
            try:
                await asyncio.wait_for(self._ready.wait(), max(0, deadline - monotonic()))
            except TimeoutError as error:
                raise BrokerUnavailableError(
                    "IBKR upstream connection did not recover; cache checkpoints retained"
                ) from error

    async def _request(self, operation, label, request):
        try:
            async for attempt in self._retrying(f"{operation}:{label}"):
                with attempt:
                    await self._pace()
                    generation = self._generation
                    try:
                        result = await request()
                    except Exception as error:
                        if self._generation != generation or not self.ib.isConnected():
                            raise ConnectionError("Connection changed during request") from error
                        raise
                    if self._generation != generation or not self.ib.isConnected():
                        raise ConnectionError("Connection changed; request result discarded")
                    self._failed_requests[operation] = 0
                    return result
        except Exception as error:
            self._failed_requests[operation] = (
                self._failed_requests[operation] + 1
                if retryable(error) and not isinstance(error, EmptyHistoryError)
                else 0
            )
            if self._failed_requests[operation] >= 3:
                raise BrokerUnavailableError(
                    f"Three consecutive {operation} requests exhausted transient retries; "
                    "stopping collection with checkpoints retained"
                ) from error
            raise

    async def __aenter__(self):
        # ib_async 2.1.0's default 1102 handler unconditionally subscribes to account
        # summaries. This exclusively owned, data-only client must not do that.
        self.ib.errorEvent -= self.ib._onError
        self.ib.errorEvent += self._error
        self.ib.disconnectedEvent += self._disconnected
        try:
            await self._connect()
            await self._wait_ready()
        except BaseException:
            self.ib.disconnect()
            self._remove_handlers()
            raise
        return self

    def _remove_handlers(self):
        self.ib.errorEvent -= self._error
        self.ib.disconnectedEvent -= self._disconnected
        self.ib.errorEvent += self.ib._onError

    async def __aexit__(self, *_exc):
        self.ib.disconnect()
        self._remove_handlers()

    async def _pace(self) -> None:
        await self._wait_ready()
        delay = self.settings.request_spacing - (monotonic() - self._last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        await self._wait_ready()
        self._last_request = monotonic()

    async def resolve(self, instrument: Instrument):
        from ib_async import Stock

        contract = Stock(
            instrument.symbol,
            "SMART",
            instrument.currency,
            primaryExchange=instrument.primary_exchange,
        )
        qualified = await self._request(
            "resolve",
            instrument.key,
            lambda: asyncio.wait_for(
                self.ib.qualifyContractsAsync(contract), self.settings.timeout
            ),
        )
        if len(qualified) != 1 or qualified[0] is None or qualified[0].conId <= 0:
            raise ValueError(f"Could not uniquely resolve {instrument.key}")
        result = qualified[0]
        if result.secType != "STK" or result.currency != instrument.currency:
            raise ValueError("Qualified contract is not a USD stock")
        return result

    async def historical(
        self, contract, end: datetime, *, minute: bool = False, duration: str | None = None
    ):
        async def request():
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime=end,
                durationStr=duration or ("2 D" if minute else "2 Y"),
                barSizeSetting="1 min" if minute else "1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
                timeout=self.settings.timeout,
            )
            if not bars:
                raise EmptyHistoryError(
                    "No historical bars returned (empty history or SDK timeout); "
                    "inspect broker messages"
                )
            return bars

        return await self._request("historical", contract.conId, request)

    async def daily(
        self, instrument: Instrument, contract, end: datetime, *, duration: str = "2 Y"
    ) -> list[DailyBar]:
        raw = await self.historical(contract, end, duration=duration)
        bars = []
        for item in raw:
            day = item.date
            if isinstance(day, datetime):
                day = day.date()
            elif isinstance(day, str):
                day = (
                    date.fromisoformat(day)
                    if "-" in day
                    else datetime.strptime(day, "%Y%m%d").date()
                )
            if not isinstance(day, date):
                raise ValueError("Unexpected daily timestamp type")
            bar = DailyBar(
                instrument.key,
                day,
                float(item.open),
                float(item.high),
                float(item.low),
                float(item.close),
                float(item.volume),
            )
            bar.validate()
            bars.append(bar)
        return bars

    async def quote_probe(self, contract, data_type: int = 1) -> dict:
        await self._pace()
        self.ib.reqMarketDataType(data_type)
        ticker = self.ib.reqMktData(contract, "", False, False)
        generation = self._generation
        try:
            deadline = monotonic() + min(self.settings.timeout, 8)
            while monotonic() < deadline:
                bid, ask = ticker.bid, ticker.ask
                if all(math.isfinite(p) and p > 0 for p in (bid, ask)) and ask >= bid:
                    break
                await asyncio.sleep(0.1)

            def clean(value):
                return float(value) if math.isfinite(value) and value > 0 else None

            bid, ask = clean(ticker.bid), clean(ticker.ask)
            valid = bid is not None and ask is not None and ask >= bid
            received = ticker.time
            age = (
                (datetime.now(UTC) - received).total_seconds()
                if received is not None and received.tzinfo is not None
                else None
            )
            checks = {
                "two_sided_quote": valid,
                "live_data_type": valid and ticker.marketDataType == 1,
                "recent_receive_time": age is not None and 0 <= age <= 5,
                "connection_stable": (
                    self.ib.isConnected()
                    and not self._upstream_lost
                    and self._generation == generation
                ),
            }
            return {
                "requested_data_type": data_type,
                "observed_data_type": ticker.marketDataType if valid else None,
                "bid": bid,
                "ask": ask,
                "last": clean(ticker.last),
                "received_at": str(ticker.time) if ticker.time else None,
                "receive_age_seconds": age,
                "spread": ask - bid if valid else None,
                "spread_fraction": (ask - bid) / ((ask + bid) / 2) if valid else None,
                "checks": checks,
                "live_quote_check_passed": all(checks.values()),
                "exchange_freshness_verified": False,
                "status": "two_sided_quote_received" if valid else "no_two_sided_quote",
                "execution_ready": False,
                "note": (
                    "Diagnostic only; receive time does not establish exchange quote freshness."
                ),
            }
        finally:
            self.ib.cancelMktData(contract)

    async def probe(self, instrument: Instrument, data_type: int) -> dict:
        decision_at = datetime.now(UTC)
        contract = await self.resolve(instrument)
        daily = await self.daily(instrument, contract, decision_at)
        minute = await self.historical(contract, decision_at, minute=True)
        quote = await self.quote_probe(contract, data_type)
        observed_at = datetime.now(UTC)
        calendar = Sessions(decision_at.date() - timedelta(days=550), decision_at.date())
        target = calendar.completed(decision_at.date() - timedelta(days=550), decision_at)[-1]
        latest_minute = minute[-1].date
        return {
            "command": "probe",
            "observed_at": observed_at.isoformat(),
            "ib_async_version": version("ib_async"),
            "server_version": self.ib.client.serverVersion(),
            "readonly_requested": True,
            "account_count": len(self.ib.managedAccounts()),
            "instrument": asdict(instrument),
            "con_id": contract.conId,
            "daily_bar_count": len(daily),
            "latest_daily_date": str(daily[-1].session),
            "last_completed_session": target.isoformat(),
            "completed_daily_bar_count": sum(b.session <= target for b in daily),
            "minute_bar_count": len(minute),
            "latest_minute_at": str(minute[-1].date),
            "latest_minute_age_seconds": (
                (observed_at - latest_minute).total_seconds()
                if isinstance(latest_minute, datetime) and latest_minute.tzinfo is not None
                else None
            ),
            "quote": quote,
            "broker_messages": self.errors,
            "retries": self.retries,
            "note": "Port does not verify paper account mode; check the TWS/Gateway login.",
        }
