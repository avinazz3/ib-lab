from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from importlib.metadata import version
from time import monotonic

from ib_lab.models import DailyBar, Instrument


@dataclass(frozen=True)
class ConnectionSettings:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 31
    timeout: float = 20
    request_spacing: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535 or self.client_id <= 0:
            raise ValueError("Use a valid port and a positive, unique client ID")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not math.isfinite(self.request_spacing) or self.request_spacing < 0.25:
            raise ValueError("request spacing must be finite and at least 0.25 seconds")


class ReadOnlyBroker:
    """Only data operations are exposed. Also enable Read-Only API in TWS itself."""

    def __init__(self, settings: ConnectionSettings, ib=None):
        # Import inside the running async context, including on newer Python runtimes.
        from ib_async import IB

        self.settings = settings
        self.ib = ib if ib is not None else IB()
        self.ib.RaiseRequestErrors = True
        self.errors: list[dict] = []
        self._last_request = -math.inf

    def _error(self, req_id, code, message, contract=None) -> None:
        self.errors.append({"request_id": req_id, "code": code, "message": message})

    async def __aenter__(self):
        from ib_async import StartupFetchNONE

        self.ib.errorEvent += self._error
        try:
            await self.ib.connectAsync(
                self.settings.host,
                self.settings.port,
                clientId=self.settings.client_id,
                timeout=self.settings.timeout,
                readonly=True,
                raiseSyncErrors=True,
                fetchFields=StartupFetchNONE,
            )
        except BaseException:
            self.ib.disconnect()
            self.ib.errorEvent -= self._error
            raise
        return self

    async def __aexit__(self, *_exc):
        self.ib.disconnect()
        self.ib.errorEvent -= self._error

    async def _pace(self) -> None:
        delay = self.settings.request_spacing - (monotonic() - self._last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_request = monotonic()

    async def resolve(self, instrument: Instrument):
        from ib_async import Stock

        await self._pace()
        contract = Stock(
            instrument.symbol,
            "SMART",
            instrument.currency,
            primaryExchange=instrument.primary_exchange,
        )
        qualified = await asyncio.wait_for(
            self.ib.qualifyContractsAsync(contract), self.settings.timeout
        )
        if len(qualified) != 1 or qualified[0] is None or qualified[0].conId <= 0:
            raise ValueError(f"Could not uniquely resolve {instrument.key}")
        result = qualified[0]
        if result.secType != "STK" or result.currency != instrument.currency:
            raise ValueError("Qualified contract is not a USD stock")
        return result

    async def historical(self, contract, end: datetime, *, minute: bool = False):
        await self._pace()
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end,
            durationStr="2 D" if minute else "2 Y",
            barSizeSetting="1 min" if minute else "1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
            keepUpToDate=False,
            timeout=self.settings.timeout,
        )
        # ib_async may return an empty list on a historical-request timeout.
        if not bars:
            raise ValueError("No historical bars returned; inspect permissions and broker errors")
        return bars

    async def daily(self, instrument: Instrument, contract, end: datetime) -> list[DailyBar]:
        raw = await self.historical(contract, end)
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
            return {
                "requested_data_type": data_type,
                "observed_data_type": ticker.marketDataType if valid else None,
                "bid": bid,
                "ask": ask,
                "last": clean(ticker.last),
                "received_at": str(ticker.time) if ticker.time else None,
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
        return {
            "command": "probe",
            "observed_at": datetime.now(UTC).isoformat(),
            "ib_async_version": version("ib_async"),
            "server_version": self.ib.client.serverVersion(),
            "readonly_requested": True,
            "account_count": len(self.ib.managedAccounts()),
            "instrument": asdict(instrument),
            "con_id": contract.conId,
            "daily_bar_count": len(daily),
            "latest_daily_date": str(daily[-1].session),
            "minute_bar_count": len(minute),
            "latest_minute_at": str(minute[-1].date),
            "quote": quote,
            "broker_messages": self.errors,
            "note": "Port does not verify paper account mode; check the TWS/Gateway login.",
        }
