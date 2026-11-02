# IBKR momentum scanner and trading system — project specification v0.2

Prepared 2 November 2026. Status: research design with initial read-only tooling and daily scanner implemented. No live broker connection or strategy performance has been verified.

## Objective and scope

Build a reproducible Python research and execution system inspired by Qullamaggie's three setups. Separate candidate discovery, setup classification, trade triggers, portfolio constraints, and broker execution. Preserve the information available at every decision so that a signal can be replayed and explained.

Initial scope: US exchange-listed common stocks, USD, long-only breakout research and paper execution. Subsequent releases add episodic pivots, then parabolic shorts and rebound longs. Starting with one strategy gives us an interpretable baseline; the architecture must support all three.

The linked article is a discretionary description, not an executable specification or independently verified performance record. Our thresholds and timing conventions below are engineering hypotheses. We must measure their fidelity and performance separately.

## Sources and verified integration constraints

Primary strategy reference: https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/

Official API introduction: https://www.interactivebrokers.com/docs/tws-api/doc/introduction

Official architecture: https://www.interactivebrokers.com/docs/tws-api/doc/architecture

Official Python installation: https://www.interactivebrokers.com/docs/tws-api/doc/install-the-tws-api-on-mac-os-linux

Official scanners: https://www.interactivebrokers.com/docs/tws-api/doc/market-scanner/subscription

Official market-data restrictions: https://www.interactivebrokers.com/docs/tws-api/doc/md/introduction

Official paper-trading limitations: https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/

Alternative Python client maintained by its authors: https://github.com/ib-api-reloaded/ib_async

IBKR scanner requests return contracts, with at most 50 results per scan code and 10 concurrent API scans. Quotes must be requested separately. Therefore these results are a candidate source, not an exhaustive implementation of our stock-universe screens.

API market-data entitlements must be checked separately from what appears free in TWS. Historical-data pacing and availability restrict bulk research. In particular, IBKR does not provide historical data for securities no longer trading. A present-day universe backtest using only IBKR cannot establish historical, market-wide performance without survivorship bias.

Paper fills use top-of-book simulation; stops and complex orders are simulated. Paper execution validates software behavior, not production fill quality.

## Technology decision

Selected client: `ib_async` (user decision), behind a read-only data adapter for the first milestone. Connect to TWS during development and optionally IB Gateway later. Pin the Python client and record the TWS/Gateway server version in the probe. Broker-independent strategy code remains reusable.

`ib_async` provides a sync/async interface. It implements the broker protocol itself and does not require `ibapi`; they are alternative client implementations, not a required stack. A compatibility smoke test should precede choosing it. Do not make strategy code depend on either client's objects.

Proposed application stack: NumPy/pandas for an understandable first research implementation, Parquet for bars and snapshots, DuckDB for analysis, SQLite for transactional orders and checkpoints, pytest for strategy and lifecycle invariants. Pin versions after compatibility checks. Python is adequate for this strategy's daily screening and minute-scale triggers; submillisecond execution is not a project requirement.

## Data contracts

Use a stable instrument identifier internally and resolve it to IBKR conId, currency, exchange, primary exchange, and tick-size rules. A ticker alone is not historical identity.

Store UTC timestamps plus the exchange session date derived from America/New_York and an exchange calendar. Handle DST, holidays, and shortened sessions explicitly. Keep extended-hours data separate from regular-session data.

Each observation records event time, local receive time, source, adjustment policy, and quality flags. Each signal records its strategy/configuration version, input snapshot, feature values, trigger, intended stop, and pass/reject reasons.

Daily features use completed sessions only. Split-adjusted research bars must be on a consistent basis; live execution prices are tradable, unadjusted prices. Handle splits in held quantities and outstanding orders. Preserve data revisions rather than silently overwriting the evidence used by a past signal.

Cache daily bars broadly; request minute bars and quotes only for shortlisted instruments. Use a paced request queue with cancellation, retries, and explicit incomplete-data outcomes. Missing observations never silently become zeros.

## First breakout experiment

All values here are initial research choices. Freeze this baseline before looking at out-of-sample results.

Let d be the last completed session, C its close, H/L its high/low, and V its volume. Compute features on a common adjusted basis. Use enough warm-up data for every lookback.

| Component | Initial measurable rule |
| --- | --- |
| Tradability | Previous close at least $5; mean daily dollar turnover over 20 completed sessions at least $10 million; valid stock identity and required data |
| Leadership | Cross-sectional return percentile at least 98 on any of 21, 63, or 126 sessions within the eligible universe |
| Base window | Fix the last 15 completed sessions as the initial candidate base; do not optimize its length per winning chart |
| Prior impulse | At the close immediately preceding the base, gain from the lowest close over its preceding 42 sessions is at least 30% |
| Base depth | (Maximum high minus minimum low in base) / latest close at most 20% |
| Contraction | Mean true range over final five base sessions / mean true range over preceding ten base sessions at most 0.70 |
| Rising support proxy | Minimum low in final five base sessions exceeds minimum low in first five base sessions |
| Trend proximity | Latest close within one ATR14 of SMA20; SMA20 higher than five sessions earlier |

These proxies may exclude attractive charts or include weak ones. Save each component independently so we can diagnose the mismatch instead of changing everything at once. Manually review a dated sample of accepted and rejected charts with future candles hidden. Keep a separate held-out set of labels.

True range is max(H-L, abs(H-previous close), abs(L-previous close)). ATR14 uses Wilder smoothing initialized from 14 true ranges. Record this convention; different implementations otherwise produce different signals.

Freeze the base ceiling, all daily features, and the eligible universe before the new regular session. A seed watchlist is sufficient for integration testing, but its cross-sectional rankings and results apply only to that watchlist.

## Intraday trigger and trade lifecycle

Initial opening range: completed regular-session trades in [09:30, 09:35) exchange local time. It becomes available at 09:35, not before. Ignore premarket prints when defining this range.

Initial trigger level: one valid tick above max(frozen base ceiling, opening-range high). Requiring both is our added interpretation. It prevents a simple intraday range break below daily resistance from qualifying as a breakout.

Use observed crossing events after the range completes. Initial entry policy: an explicitly capped marketable limit order after a valid crossing; cancel an unfilled remainder after a configured timeout. This avoids pretending an unlimited market fill and a guaranteed-price limit fill are equivalent. If the market is already beyond the chase cap, skip the entry and log why. Resolve tick rounding from instrument rules.

At entry time tau, initial stop reference is the regular-session low observed through tau, less a tick buffer. Require positive stop distance no greater than the prior session's ATR14. Freeze the initial protective stop after entry; later lower lows cannot widen it. Crucially, the day's eventual low is unavailable at entry.

Use a fixed partial-exit policy for the first experiment: sell one third at the next regular-session open after three complete holding sessions, provided the position remains open, then raise the remaining protective stop to entry if appropriate. Entry session does not count as a complete holding session. Define rounding for small positions and never submit zero quantity.

For the remainder, a completed daily close below SMA10 produces an exit decision; execute at the next eligible regular-session open. Keep an intraday protective stop active. A daily-close trend exit is not a stop order continuously tracking the moving average. Treat alternative partial-exit timings and SMA choices as separately versioned experiments.

Orders and position quantities follow actual executions, including partial fills. Every filled share must have acknowledged protective coverage. If protection is rejected or cannot be confirmed, stop new entries, cancel any unfilled parent remainder, reconcile broker state, and attempt an emergency position reduction according to an explicit policy. Never presume a successful API submission means broker acceptance.

## Risk and portfolio controls

Initial paper-only settings: planned loss budget 0.25% of current equity per trade, single-name notional ceiling 10%, gross exposure ceiling 50%, at most five positions, and aggregate planned stop loss at most 1%. These are our conservative starting constraints, not validated optimal values.

For estimated entry P, stop S, equity E, loss fraction r, cost allowance per share k, and name ceiling w:

q = floor(min(r*E/(P-S+k), w*E/P, remaining buying power/P, liquidity quantity cap)).

Also reserve portfolio risk and buying power for pending entries. Recalculate after actual fills; cancel residual quantity if the realized fill would breach limits. Account for existing positions and orders in the same instrument. Planned stop loss does not cap actual loss through gaps, halts, or poor fills.

Block new entries on stale/delayed quotes, missing bars, excessive spread, unknown session state, unresolved broker reconciliation, or triggered exposure limits. Persist a manual stop-new-entries switch separately from a flatten request: cancelling all orders must not accidentally remove protection from existing positions.

## Episodic pivot extension

Separate a price/volume candidate from a catalyst-confirmed trade. A gapper is not automatically an episodic pivot. Store news release time, first available time, source, catalyst category, and human confirmation where needed.

Define gap as current regular-session open / prior adjusted-equivalent close - 1. Premarket gap is an estimate and must be labelled separately. Never substitute a revised or split-inconsistent previous close.

Measure cumulative volume through minute m against historical cumulative volume through the same minute, using completed prior sessions. Compare full-day volume only once the day is complete. Validate premarket-volume coverage separately. Thresholds, prior-trend exclusions, and news classification remain research decisions to specify before this strategy can trade.

Earnings growth and consensus surprise require timestamped historical releases and estimates. Initially allow manual catalyst labels rather than claiming an OHLCV-only system understands surprise. Add a provider only after checking coverage and cost.

## Parabolic extension

Features to research: multi-session returns, consecutive up sessions, acceleration, market capitalization, distance from moving averages normalized by ATR, intraday reversal structure, and session VWAP behavior.

Use a state machine for any VWAP-rejection variant: initial breakdown, rebound toward VWAP, rejection confirmation, then eligible entry. Comparing price below VWAP alone does not encode this sequence. Freeze a precise definition before testing.

Short execution additionally needs available borrow, borrow costs, permissions, applicable short-sale restrictions, recalls, halts, and squeeze scenarios. Do not infer historical borrow availability from present-day shortability. Rebound longs are a distinct experiment; they are not a mechanical inversion of every short rule.

## Architecture and interfaces

| Module | Responsibility |
| --- | --- |
| universe | Dated membership, instrument identity, eligibility |
| data | Provider adapters, pacing, immutable raw observations, cached bars |
| features | Pure, timestamped daily/intraday calculations |
| strategies | Setup classification, stateful triggers, exit decisions |
| portfolio | Sizing, reservations, exposure, risk limits |
| execution | Order intents, broker adapter, fill handling, protection |
| state | Durable checkpoints, reconciliation, deduplication |
| research | Replay, fill models, experiments, reports |
| cli | scan, inspect, replay, paper, status commands |

Strategies produce broker-independent intents. Both replay and paper trading consume the same feature and decision code. Broker callbacks are normalized into events, queued, persisted, and processed without running heavy calculations in the callback path.

Use stable intent identifiers to prevent duplicate entries across retries or restarts. At reconnect, reconcile positions, executions, and open orders before allowing new entries. Keep strategy identity separate when two setups select the same instrument so we can avoid accidental double exposure.

## Evaluation and verification

1. Validate data and candidate fidelity before calculating profitability. Record the eligible-universe denominator, missing-data exclusions, and scanner coverage.
2. Replay minute data for entry ordering and initial stops; daily bars alone cannot establish which happened first. If entry and stop can occur within the same bar, use finer data or a conservative ambiguous-bar assumption.
3. Charge commissions, spreads, adverse slippage, and gap-through-stop fills. Cap participation and model unfilled orders. Stress execution costs rather than trusting one estimate.
4. Use chronological development, validation, and untouched test periods. Prevent overlapping trades from leaking across splits. Freeze a small set of hypotheses and log every parameter variant.
5. For market-wide historical claims, use historical listings and delisted instruments. If unavailable, label results as conditional on the surviving/watchlist universe and start prospective logging.
6. Report net expectancy in initial-risk units, mean winner/loser, drawdown, turnover, exposure, capacity constraints, holding times, and results by period/regime. Measure portfolio-level behavior and concentration of profits in a few trades. Show uncertainty with dependence-aware resampling.
7. Compare against simple momentum/breakout baselines and ablate contraction/support filters. A complicated screen must justify its additions out of sample.
8. Test meaningful failure cases: future data changes cannot alter past signals; duplicate callbacks cannot double quantities; partial fill plus disconnect cannot lose protection; delayed data blocks entry; gap-through stops use attainable fills.

## Implementation milestones

| Milestone | Concrete completion evidence |
| --- | --- |
| M0: integration probe | Read-only paper connection; instrument resolution; one daily/minute download; entitlement and delay status logged |
| M1: reproducible scanner | Dated watchlist/universe input; cached daily data; per-filter features and rejection reasons; candidate chart review |
| M2: research replay | Same strategy code on historical events; documented costs and timing; baseline and held-out reports |
| M3: paper execution | Intent-to-fill lifecycle; protective orders; restart/reconnect recovery; durable audit trail |
| M4: episodic pivots | Timestamped catalyst workflow; intraday volume baseline; independently evaluated strategy |
| M5: parabolic variants | Explicit pattern state machines; realistic short constraints; separate evaluation |

No live profitability or automated execution is established by this specification. Live operation is a later, explicit deployment decision after reviewing measured strategy and execution behavior.

## Inputs needed for the next implementation step

- Repository: https://github.com/avinazz3/ib-lab
- Whether a usable IBKR paper account and TWS/Gateway are already available.
- Which US equity data subscriptions are enabled for API use.
- Whether the initial historical-data budget is IBKR-only or can include a dedicated source.

Proceed with US common stocks, ib_async, local development, breakout-first research, and paper execution in a later milestone. Keep market-data and execution adapters separate so a later historical provider does not require rewriting the strategy.
