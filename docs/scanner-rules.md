# Breakout screen v0.1

These are explicit engineering proxies, not an exact replication of a discretionary
trader. All fractions are decimal (0.30 means 30%); ATR and moving averages are in
price units. Config changes produce a different configuration hash.

At decision time t, find the most recent completed XNYS session d. Require all 127
sessions ending at d. Exclude future/incomplete session rows before validation or
feature calculation. Duplicate sessions, invalid OHLCV, and nontrading dates in
the historical input window cause an explicit data rejection. We use the same
127-session feature warm-up for each symbol.

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
  first five base sessions, strictly positive.
- ATR14: initialize with mean of the first 14 true ranges in the 127-session
  warm-up, then apply `(13*ATR + TR)/14` for each subsequent session.
- Proximity = `abs(C[d] - SMA20[d]) / ATR14[d]`, at most `max_sma_distance_atr`.
- Trend = `SMA20[d] > SMA20[d-5]`.

Pass all checks to become a candidate. The scanner deliberately does not optimize
the base length per stock. Zero denominators are data/feature failures.

The report stores the base ceiling and ATR for subsequent intraday research. It
does not calculate an entry, a day-low stop, or a position quantity from daily bars.

## Limitations

The XNYS calendar is a regular-session calendar proxy for this initial US universe;
it does not describe individual security halts. The screen requires continuous
recent session history and consequently rejects recent IPOs and stocks with
missing/halted-session bars. This is an intentional v0.1 scope restriction that
must be evaluated, since it can omit meaningful momentum candidates.

Historical scans use the snapshot's data vintage and declared membership. An
as-of timestamp prevents future-price use in the feature code, but cannot turn
later revised data or today's universe into a point-in-time historical dataset.
