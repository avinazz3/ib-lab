from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from math import floor, inf

from AlgorithmImports import (
    CashAmount,
    ConstantFeeModel,
    DataNormalizationMode,
    EquityFillModel,
    InteractiveBrokersFeeModel,
    LimitOrder,
    OrderEvent,
    OrderFee,
    OrderFeeParameters,
    OrderStatus,
    QuoteBar,
    Resolution,
    TradeBar,
)

from ib_lab.entry import ENTRY_TIMEOUT, entry_levels, modeled_buy_fill


class BreakoutFillModel(EquityFillModel):
    def __init__(self, algorithm):
        super().__init__()
        self._algorithm = algorithm
        self._fee_model = InteractiveBrokersFeeModel()
        self._fills = {}

    def limit_fill(self, asset, order):
        now = self._algorithm.utc_time
        event = OrderEvent(order, now, OrderFee.ZERO)
        last_fill, fees_paid = self._fills.get(order.id, (None, 0.0))
        if last_fill == now:
            return event
        bar = asset.cache.get_data(TradeBar)
        quote = asset.cache.get_data(QuoteBar)
        if (
            bar is None
            or quote is None
            or bar.is_fill_forward
            or quote.is_fill_forward
            or bar.end_time != asset.local_time
            or quote.end_time != bar.end_time
            or quote.bid is None
            or quote.ask is None
        ):
            return event
        ticket = self._algorithm.transactions.get_order_ticket(order.id)
        remaining = int(order.quantity - ticket.quantity_filled)
        result = modeled_buy_fill(
            order.time,
            now,
            quote.bid.close,
            quote.ask.close,
            float(bar.volume),
            order.limit_price,
            asset.symbol_properties.minimum_price_variation,
            remaining,
        )
        if result is not None:
            price, quantity = result
            event.fill_price = float(price)
            event.fill_quantity = quantity
            event.status = (
                OrderStatus.FILLED if quantity == remaining else OrderStatus.PARTIALLY_FILLED
            )
            cumulative = LimitOrder(
                order.symbol, int(ticket.quantity_filled) + quantity, order.limit_price, order.time
            )
            fee = self._fee_model.get_order_fee(OrderFeeParameters(asset, cumulative))
            fees_due = max(fees_paid, float(fee.value.amount))
            event.order_fee = OrderFee(CashAmount(fees_due - fees_paid, fee.value.currency))
            self._fills[order.id] = (now, fees_due)
        return event


@dataclass
class OpeningRange:
    base: float
    atr: float
    tick: float
    leadership: float
    opening_minutes: int = 0
    opening_high: float = 0
    low: float = inf
    result: str = "no_crossing"
    ticket: object | None = None
    entry_limit: float = 0
    stop_risk: float = 0

    @property
    def pending(self) -> int:
        if self.ticket is None or self.ticket.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.INVALID,
        ):
            return 0
        return int(self.ticket.quantity - self.ticket.quantity_filled)


class OpeningRangeExecution:
    """One-session entry pilot with native LEAN tickets and portfolio accounting."""

    def __init__(self, algorithm):
        self._algo = algorithm
        self._fill_model = BreakoutFillModel(algorithm)
        self._states = {}
        self._order_events = Counter()
        algorithm.set_cash(100_000)

    def start(self, report, members, hours, cutoff):
        self._open = hours.get_next_market_open(cutoff, False)
        self._close = hours.get_next_market_close(self._open, False)
        for row in report["results"]:
            if not row["accepted"]:
                continue
            symbol = members[row["instrument_key"]]
            # The dated Symbol preserves identity across ticker changes.
            asset = self._algo.add_security(
                symbol, Resolution.MINUTE, False, 1, False, None, DataNormalizationMode.RAW
            )
            asset.set_fill_model(self._fill_model)
            # The fill model charges the cumulative commission even on partial fills.
            asset.set_fee_model(ConstantFeeModel(0))
            f = row["features"]
            self._states[symbol] = OpeningRange(
                f["base_ceiling"],
                f["atr14"],
                float(asset.symbol_properties.minimum_price_variation),
                max(f["percentile_21"], f["percentile_63"], f["percentile_126"]),
            )
        self._order = sorted(self._states, key=lambda s: (-self._states[s].leadership, str(s.id)))
        self._algo.log(f"ENTRY subscribed={len(self._order)} cash=100000")

    def on_data(self, data):
        now = self._algo.time
        for symbol in self._order:
            state = self._states[symbol]
            if state.pending and (
                now > self._close or self._algo.utc_time - state.ticket.time > ENTRY_TIMEOUT
            ):
                state.ticket.cancel("Entry expired")
            if state.result != "no_crossing" or now >= self._close or symbol not in data.bars:
                continue
            bar = data.bars[symbol]
            if (
                bar.time < self._open
                or bar.is_fill_forward
                or bar.volume <= 0
                or bar.end_time != now
            ):
                continue
            state.low = min(state.low, float(bar.low))
            if bar.time < self._open + timedelta(minutes=5):
                state.opening_minutes += 1
                state.opening_high = max(state.opening_high, float(bar.high))
                continue
            if state.opening_minutes != 5:
                state.result = "missing_opening_range"
                continue
            levels = entry_levels(state.base, state.opening_high, state.low, state.tick)
            close = Decimal(str(bar.close))
            if close < levels.trigger:
                continue
            if close > levels.limit:
                state.result = "beyond_entry_cap"
            elif not 0 < levels.limit - levels.stop <= Decimal(str(state.atr)) or levels.stop <= 0:
                state.result = "invalid_stop_distance"
            else:
                self._submit(symbol, state, levels)

    def _submit(self, symbol, state, levels):
        portfolio = self._algo.portfolio
        equity = float(portfolio.total_portfolio_value)
        active = reserved = planned_risk = 0
        for key, other in self._states.items():
            pending = other.pending
            held = int(portfolio[key].quantity)
            active += bool(pending or held)
            reserved += pending * other.entry_limit
            planned_risk += (pending + held) * other.stop_risk
        limit = float(levels.limit)
        risk = float(levels.limit - levels.stop)
        gross_left = equity * 0.50 - float(portfolio.total_holdings_value) - reserved
        quantity = floor(
            min(
                equity * 0.0025 / risk,
                (equity * 0.01 - planned_risk) / risk,
                equity * 0.10 / limit,
                gross_left / limit,
            )
        )
        if active >= 5 or quantity <= 0:
            state.result = "portfolio_limit"
            return
        state.stop_risk = risk
        state.entry_limit = limit
        state.result = "submitted"
        state.ticket = self._algo.limit_order(symbol, quantity, limit, "Breakout entry")
        self._algo.log(
            f"ENTRY submitted={symbol.value} at={self._algo.time} id={state.ticket.order_id} "
            f"qty={quantity} trigger={levels.trigger} limit={limit} planned_stop={levels.stop}"
        )

    def on_order_event(self, event):
        if event.fill_quantity or event.status in (OrderStatus.CANCELED, OrderStatus.INVALID):
            self._order_events[str(event.status)] += 1
            self._algo.log(
                f"ENTRY event={event.status} symbol={event.symbol.value} id={event.order_id} "
                f"at_utc={event.utc_time} qty={event.fill_quantity} price={event.fill_price} "
                f"fee={event.order_fee.value.amount} message={event.message}"
            )

    def finish(self):
        outcomes = Counter(
            "missing_opening_range" if s.opening_minutes != 5 else s.result
            for s in self._states.values()
        )
        self._algo.log(f"ENTRY outcomes={dict(outcomes)} events={dict(self._order_events)}")
