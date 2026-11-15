# Historical breakout backtest with LEAN

## Question

Does the mechanical long breakout, using the current review configuration,
improve net results over a simpler momentum breakout with identical execution
and risk constraints? This experiment covers the breakout in [rules.md](../rules.md)
and the lifecycle in the [project specification](project-spec.md). Every valid
trigger is considered for an order subject to portfolio limits. Comparing human
approvals requires decisions made blind to later prices and dated before entry.

## Engine and data

Use QuantConnect LEAN for the market clock, order and portfolio simulation,
and corporate-action handling. Use AlgoSeek US Equities for trades and quotes,
the US Equity Security Master for security identity and actions, and historical
Morningstar equity universes for daily membership. The provider's coverage is
part of the experiment definition. Verify venue and share-type coverage before
calling the results representative of the live eligible universe. Keep a stable
security ID through symbol changes and include members that later delist.

Historical discovery applies the existing price and liquidity checks and ranks
21-, 63- and 126-session returns across the eligible historical universe on each
date. The existing 98th-percentile requirement applies. A current IBKR scanner
result has no role in setting historical membership. The experiment tests the
same setup checks with a different discovery rule from the capped live IBKR
scans. Save prospective IBKR runs to measure that discovery rule.

Build a rolling daily panel for screening and subscribe to intraday data for
candidates and open positions. Reuse `ib_lab.screen` and
`configs/breakout-review.json`. Keep provider conversion outside the feature
calculations and verify parity by passing identical inputs through both paths.

## Information available at each decision

1. Freeze dated membership and the screen before the intended regular-session
   open. Use only observations available by the decision timestamp, with the
   latest daily bar from the previous completed session. Use the exchange
   calendar for early closes, holidays and daylight saving time. Require the
   same 127 consecutive sessions as the production screen. Record the
   membership time, last bar time and each missing-history reason.
2. Use raw closing prices and share volumes for the price and dollar-volume
   eligibility checks. Adjust prior closes and OHLC for splits effective by the
   decision to compute returns and features on a consistent price scale. Apply
   the raw liquidity inputs separately from adjusted features in the shared
   screen. Keep fill prices on the raw scale and handle splits and dividend
   cash flows in the portfolio. Convert frozen levels when a split takes
   effect between screening and execution. Record the vendor data vintage,
   action policy and coverage limits. Whole-history adjusted prices can
   incorporate later actions into historical price levels.
3. Process events in simulated time. A minute bar becomes available at its
   end. Do not expose end-of-day volume, the session's final low, or the
   result of an order before the corresponding event. Do not fill forward
   missing trades or quotes for execution.

## Order and portfolio assumptions

| Step | First experiment |
| --- | --- |
| Opening range | Use regular-session trades from 09:30 up to but excluding 09:35 New York time. Require all five genuine one-minute trade bars in the minute model. |
| Entry signal | One tick above the higher of the frozen base ceiling and opening-range high. With minute data, require the first completed bar close at or above the trigger after the range is available. This is a bar-close proxy for the intraday crossing; use ordered ticks to check it. |
| Entry fill | Submit a buy limit after the signal, at most 10 bps above the trigger rounded down to a valid tick. Use only subsequent market data and cap fills at the limit. Cancel the unfilled remainder after two minutes or at session end, whichever is earlier. Skip fills without a valid quote and actual traded volume. |
| Initial stop | One tick below the regular-session low known at the signal. Require the distance from the maximum entry price to the stop to be positive and no larger than the previous daily ATR14. Keep the stop fixed until an exit rule raises it. |
| Stop execution | Use a marketable sell after a stop trigger. Model an overnight gap or halt reopening with the first executable price, which can be below the stop. Check same-minute entry and stop sequences with tick data or use adverse ordering when both are feasible. Record ambiguous cases. |
| Partial and final exits | Sell one-third, rounded down, at the next regular open after three complete holding sessions. Entry day is excluded. Raise the remaining stop to at least the entry price after the partial fills. A completed close below SMA10 schedules a full exit at the next regular open. The full exit supersedes a partial due at the same open. Protective orders stay active until flat. Use observable opening auction prices where supported and otherwise the first executable regular-session quote. |
| Portfolio | Start each research partition flat with $100,000 cash. Long-only, no leverage. Risk 0.25% of equity per trade, with ceilings of 10% per name, 50% gross exposure, five positions and 1% total planned stop risk. Reserve exposure, buying power and risk for pending entry orders. Break ties between simultaneous orders by highest prior-session leadership percentile, then stable ID. |
| Trading costs | Use the applicable IBKR commission schedule and record its parameters. Use bid/ask plus a proposed 5 bps adverse slippage per side beyond the spread. Cap fills at 1% of reported contemporaneous traded volume, rounded down. Model residual shares and test heavier costs and lower participation. |

The capital, entry cap, timeout, slippage and participation assumptions are
proposed research settings. Freeze them before measuring performance. A minute
quote and traded volume provide a liquidity proxy. They cannot establish the
order's place in the queue. LEAN's default equity fill model assumes full
fills, so a custom fill policy is needed for the participation and timing
assumptions above. Keep positions without an observable liquidation price in
the report, record any documented delisting proceeds and disclose the valuation
assumption for unresolved holdings.

## Research and evaluation

1. **Audit data access.** Run the procedure below before wiring the strategy.
   Verify the dated universe, prior daily rows, and real minute trades and
   two-sided quotes. Then check split and ticker-change examples, a delisting
   while holding, and an early close. Measure missing data across the
   development universe before setting any additional coverage filters.
2. **Verify chronological behavior.** On identical inputs, check
   candidate and feature parity independently of fill logic. Change bars,
   constituent records and split events after a cutoff and require earlier
   decisions to stay identical. Exercise gaps, same-minute entry and stop,
   missing quotes, halts, partial fills and competing orders.
3. **Freeze the comparison.** Proposed development 2016–2019, validation
   2020–2022 and final test 2023–2025, subject to data access. Use pre-period
   history for warm-up only. Run each period from a flat book, with no trades
   carried across partitions. Mark any positions still open at the boundary
   and disclose their treatment. Log parameter trials and keep the final test
   unread until the execution assumptions and strategy are fixed. Retain any
   post-test changes for a new evaluation period.
4. **Compare on the same terms.** Run an ablation that keeps the same universe,
   eligibility, leadership and 15-session ceiling, drops the impulse and other
   setup checks, and uses the same order, cost and portfolio models. This tests
   what the setup checks add. Also show SPY total return as market context.
5. **Report uncertainty.** Include net return, max drawdown, net expectancy in
   initial-risk units, trade count, fill rates, turnover, exposure, return by
   period and concentration in large winners. Report candidates, triggers,
   blocked trades, missing data and unresolved holdings. Use a block bootstrap
   on paired daily portfolio returns to reflect clustered and overlapping
   trades. Show results after doubled slippage and reduced volume participation.
   Pin the engine revision, strategy code and config hash, data sources and
   date range, and retain decisions, orders, fills and the equity curve.

## Run the first data audit

Create a Python project in QuantConnect's cloud editor. Copy
[`research/lean/main.py`](../research/lean/main.py) into its `main.py` and click
**Backtest**. This five-session audit has zero orders. It uses AAPL and TWTR
from July 9–13, 2018, to test access to one current and one delisted name.

Check the `AUDIT` lines in the log:

- `history` should report 127 daily bars per name, ending on July 6 and
  preceding the simulated cutoff. This checks access and the time boundary.
  Before screening, validate all 127 dates against the exchange calendar.
- `universe` should report a nonempty dated membership with both names present
  before each intended July 9–13 session. Check timestamps and skipped dates.
- `intraday` should show five opening-range trade minutes per name per session,
  positive trade counts and usable two-sided quotes. Record quote gaps, since
  they can prevent fills even when there are trades.

The audit checks access and representative data. It produces no strategy
returns. Cloud editor access is a practical way to start. QuantConnect lists
separate requirements for paid CLI access and local dataset downloads.

## References

- [AlgoSeek US Equities](https://www.quantconnect.com/data/algoseek-us-equities)
- [Historical fundamental universes](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/equity/fundamental-universes)
- [Time slices and the time frontier](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/timeslices)
- [US equity resolutions and price normalization](https://www.quantconnect.com/docs/v2/research-environment/datasets/us-equity)
- [LEAN equity fill model](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/trade-fills/supported-models/equity-model)
- [QuantConnect plans](https://www.quantconnect.com/pricing)
- [Bailey et al., The Probability of Backtest Overfitting](https://escholarship.org/uc/item/4w1110bb)
