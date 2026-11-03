# Breakout screen v0.2

These are explicit engineering proxies, not an exact replication of a discretionary
trader. All fractions are decimal (0.30 means 30%); ATR and moving averages are in
price units. Config changes produce a different configuration hash.

At decision time t, find the most recent completed XNYS session d. Require all 127
sessions ending at d. Exclude future/incomplete session rows before validation or
feature calculation. Duplicate sessions, invalid OHLCV, and nontrading dates in
the historical input window cause an explicit data rejection. We use the same
127-session feature warm-up for each symbol.

Every row includes required/available session counts, first/latest available dates,
and a count plus sample of missing sessions. Data errors are classified separately
from failed setup checks. A contiguous short tail ending on the target session is
`insufficient_history`; holes or a missing latest session are `missing_sessions`;
no bars is `no_history`. The snapshot manifest can identify an upstream
`download_failure`, which is attached to the affected report row. A short history
is not proof that the company recently listed. None of these diagnoses makes an
incomplete instrument rank-eligible.

## Eligibility and ranking

- `close >= min_price`.
- Mean of `close * volume` over the latest 20 sessions >= `min_dollar_volume`.
- For L in 21, 63, 126, return is `C[d] / C[d-L] - 1`.
- Rank only data-complete, price/liquidity-eligible instruments, using midrank
  percentiles. Pass leadership if any horizon reaches `leadership_percentile`.

Rows failing eligibility still appear in the report, with the failed checks.
The intended universe is user-supplied common stocks, not whatever symbols happen
to have succeeded in a broker scanner. Reported rankings remain conditional on
this list and data coverage; exclusions are reported rather than hidden.

## Setup proxies

- Base = sessions d-14 through d inclusive (15 sessions).
- Prior impulse = `C[d-15] / min(C[d-57:d-15]) - 1`, inclusive endpoints,
  at least `min_impulse`. This uses 43 closes ending immediately before the base.
- Base depth = `(max(base highs) - min(base lows)) / C[d]`, at most `max_base_depth`.
- True range at j = max(`H[j]-L[j]`, `abs(H[j]-C[j-1])`, `abs(L[j]-C[j-1])`).
- Contraction = mean TR over final five base sessions divided by mean TR over the
  preceding ten base sessions, at most `max_contraction`.
- Support proxy = minimum low of final five base sessions minus minimum low of
  first five base sessions. Require it to be strictly positive only when
  `require_rising_support` is true (the default).
- ATR14: initialize with mean of the first 14 true ranges in the 127-session
  warm-up, then apply `(13*ATR + TR)/14` for each subsequent session.
- Proximity = `abs(C[d] - SMA20[d]) / ATR14[d]`, at most `max_sma_distance_atr`.
- Trend = `SMA20[d] > SMA20[d-5]`.

Pass all mandatory checks to become a candidate. `checks` always records the actual
result, including a false informational support check. `informational_checks`
identifies checks excluded from rejection reasons and acceptance. The scanner does not optimize
the base length per stock. Zero denominators are data/feature failures.

The report stores the base ceiling and ATR for subsequent intraday research. It
does not calculate an entry, a day-low stop, or a position quantity from daily bars.

## Strict and review configurations

The default and `configs/breakout.json` retain the strict numerical rules.
`configs/breakout-review.json` applies the approved relaxation for human review:

| Criterion | Strict | Review |
| --- | --- | --- |
| Price | >= $5 | Same |
| Mean 20-session dollar volume | >= $10 million | Same |
| Leadership on any 21/63/126-session horizon | >= 98th percentile | Same |
| Prior impulse | >= 30% | Same |
| Base depth | <= 20% | <= 25% |
| Contraction ratio | <= 0.70 | <= 1.00 |
| Absolute distance from SMA20 | <= 1 ATR | <= 2 ATR |
| Support proxy | > 0 required | Informational |
| SMA20 change over five sessions | > 0 | Same |
| Valid, complete latest 127 sessions | Required | Same |

Both modes use identical feature windows, ranking populations and formulas.
Relaxed thresholds are research hypotheses, not evidence of improved returns.
`accepted` means the stock passed the selected screen; it does not mean a human
approved it or that the system can place a trade. Human approval and execution
are not implemented by this change.

## Candidate research numbers

`candidates.csv` has one row per candidate: symbol/exchange/currency, last completed
session and close, 21/63/126-session returns and their start dates, leadership
percentiles, mean 20-session volume and dollar volume, the existing setup features,
all check results and whether rising support is required. Returns end on the last
completed session. `report.json` retains every instrument and its rejection reasons.

Each candidate also has a `research` object in the report. `candidate-events.csv`
flattens its event rows. Within the latest 126 completed sessions, select the three
largest absolute close-to-close returns and the three highest relative-volume
observations. Combine dates selected by both and sort chronologically, giving at
most six rows per candidate. Ties favor the earlier date. Each row contains:

- Date and `selected_for` (price change, relative volume, or both).
- `price_change = close / previous_session_close - 1`.
- `opening_gap = open / previous_session_close - 1`.
- Actual daily volume and `relative_volume = volume / mean(previous 20 session volumes)`.

Returns, price changes and gaps are decimal fractions: 0.05 means 5%. Relative volume
is a multiple: 2 means twice the preceding average. The current session is excluded
from its volume baseline. A zero or incomplete baseline yields null (blank in CSV),
never infinity or a shorter mean. Such a date can still appear as a price event.

Research uses up to 146 completed sessions for the volume context. Old snapshots
may contain only 127 bars; all 126 price changes remain available, but only the last
107 sessions can then have a full volume baseline. The event window and
`relative_volume_session_count` disclose actual coverage. Missing older context
does not alter the 127-session eligibility requirement. Event selection considers
only available complete baselines. These are largest observed moves, not verified
anomalies or explanations of their causes. News collection and generated research
questions are outside the application.

## Limitations

The XNYS calendar is a regular-session calendar proxy for this initial US universe;
it does not describe individual security halts. The screen requires continuous
recent session history and consequently rejects recent IPOs and stocks with
missing/halted-session bars. This is an intentional v0.1 scope restriction that
must be evaluated, since it can omit meaningful momentum candidates.

Historical scans use the snapshot's data vintage and declared membership. An
as-of timestamp prevents future-price use in the feature code, but cannot turn
later revised data or today's universe into a point-in-time historical dataset.
