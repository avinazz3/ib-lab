# Automatic daily screening — v0.2

Run `python -m ib_lab daily --port 7496 --seed-snapshot data/real-001` from the repo
with TWS logged in and its Read-Only API setting enabled. After the initial seed,
the same command can be reused. `--port 7497` is the usual paper TWS alternative;
confirm the actual application setting. This command requests history only.

## Universe

Fetch both current Nasdaq Trader directories:

- https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
- https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

Official field definitions: https://www.nasdaqtrader.com/Trader.aspx?id=SymbolDirDefs

Require Test Issue=N, ETF=N, and Financial Status=N on Nasdaq listings. Retain
names explicitly containing Common Stock, Common Share(s), or Ordinary Share(s).
Exclude names containing preferred/preference, depositary/depository, warrants,
rights, units, notes, bonds, debentures, fund, ETF or ETN. Accept only simple
one-to-six-letter tickers; reject ambiguous symbols across the files. Supported
other-exchange codes map N→NYSE, A→AMEX, P→ARCA, Z→BATS, V→IEX. Resolve the actual
IBKR stock contract before downloading.

This is a conservative name-based classification. It excludes ADRs, special
class-share syntax and some genuine common stocks with ambiguous names. It can
include ordinary shares of acquisition companies. It does not certify issuer
domicile, security type, trading permissions or investability. These limitations
and every directory exclusion are stored. Do not describe the result as every
US common stock. Raw directories, retrieval time, publisher footer, hashes and
the selected CSV are preserved in each run. A missing footer, unexpected schema
or network failure stops discovery instead of silently falling back to stale files.

## Data cutoff and cache

Freeze a decision timestamp at run start minus `--completion-lag-minutes` (20 by
default). XNYS determines the latest session closed by that timestamp. Request
history ending immediately after that session's close; reject history that does
not reach the target. The time buffer accommodates the observed delayed feed;
it cannot guarantee final or exchange-certified data. Security-specific halts
can still produce missing bars and explicit exclusions.

SQLite stores each stock's native IBKR TRADES daily history and qualified conId
transactionally. A filelock lock prevents concurrent writers. Initial/new or
incompatible entries request two years. Fresh cached entries need no IBKR
connection; otherwise resolve the contract and request the elapsed calendar days
plus seven, at least ten days. Merge overlapping dates with newer observations.
If overlapping OHLC differs, overlap is absent, identity changes, or the full
history is 30 calendar days old, request full history rather than mix adjustment
bases. A periodic refresh limits, but does not eliminate, undetected older revisions.

TRADES history is split-adjusted, not dividend-adjusted, per IBKR:
https://www.interactivebrokers.com/docs/tws-api/doc/md/historical-bars

Snapshots with compatible native IBKR RTH two-year history can seed an empty
cache. Their hashes and identity are checked. Discard any bar that was not closed
at least 20 minutes before the seed snapshot's creation; this avoids treating an
old partial bar as a completed session. Existing cache entries are not overwritten
by seeding. Short IPO histories are retained but remain ineligible for the screen.

Successful stocks are committed immediately, so Ctrl-C leaves checkpoints for a
rerun. Failed updates preserve the prior cache entry but exclude that stock's bars
from the run snapshot. Errors and explicit budgets do not shrink the declared
universe silently. The snapshot keeps up to the latest 146 completed bars per stock
(127 for screening and extra volume-baseline context for research events);
the cache retains the broader history. It is intended for the frozen daily scan,
not arbitrary historical replay.

`--refresh-candidates PATH` accepts an existing candidates CSV (or a universe CSV).
Every listed instrument must belong to the selected broad universe. A header-only
shortlist is a valid no-op. Those stocks always re-resolve and fetch two-year
history, even if today's cached entry is fresh. The remaining stocks follow the
normal cache policy. The full selected universe is still ranked; do not substitute
a six-stock shortlist for it. Forced refresh keys are recorded in `universe.json`.

## Connectivity and retries

The shared broker adapter uses Tenacity for up to three total attempts per transient
request, waiting 2 then 4 seconds by default (capped at 10 seconds for additional
attempts). Timeouts, connection errors, SDK empty-history responses and recognized
162 pacing/connection/cancellation messages are retryable. Permission errors,
unresolved contracts and invalid OHLCV are not retried as transient failures.
The SDK cannot distinguish every historical timeout from an empty result;
`EmptyHistoryError` explicitly preserves that ambiguity. Empty responses alone do
not trigger the consecutive-outage stop, since some symbols legitimately have no
history; their exhausted attempts remain visible as individual failures.

On 1100, pause before issuing new requests and await 1101/1102 for up to
`--recovery-timeout` seconds (default 60). A socket disconnect reconnects with the
same positive client ID, Read-Only flag and StartupFetchNONE. If connectivity changes
while a response is in flight, discard it and retry the request. The data-only
client suppresses ib_async 2.1.0's default 1102 account-summary resubscription hook
for the lifetime of its context, then restores it on cleanup. No account-summary
subscriptions are requested by this application.

Failure to recover or three consecutive exhausted transient requests of the same
operation aborts collection with exit 2. Later instruments are not marked failed
in a cascade. `status.json` retains typed failures, broker messages and retry
records; successful cache transactions survive, and no completed scan is claimed
for an aborted collection. Rerun after recovery to resume from cache. Individual
nonfatal failures still produce a partial snapshot and scan. `--max-attempts`
changes the per-request attempt bound; download budgets continue to count stocks,
not the physical requests made during retries.

## Output and ranking

Each automatically timestamped run directory contains:

- `status.json`: running, completed, partial, or interrupted/failed status.
- `universe/`: source directories and discovery evidence (automatic mode).
- `universe.json`: source scope and any explicit subset limit.
- `snapshot/`: Parquet bars, manifest, cache action per stock, and failures.
- `scan/report.json` and `scan/candidates.csv`: screening results and candidate numbers.
- `scan/candidate-events.csv`: up to six dated price/volume observations per candidate.

`--universe PATH` overrides automatic discovery. `--max-symbols N` selects the
first N sorted listings and labels the result as a subset. `--max-downloads N`
caps stocks needing requests, not individual broker calls; a stock may need a
second historical request when adjustment changes require a full refresh.
Defaults apply no subset or download budget. Requests are serial and paced.

Rank only price/liquidity-eligible stocks with valid, complete recent history.
Always inspect the eligible denominator and failures: a partial collection yields
conditional rankings, not certified market-wide ranks. Zero candidates alone is
not an error. Failed downloads or scan data errors return exit 1; initialization
errors return 2; interruption returns 130. The default thresholds remain strict.
Pass `--config configs/breakout-review.json` to select the approved broader human
review rules. The scanner never adjusts thresholds automatically to produce candidates.

`report.json` attaches source download failures to the affected rows and aggregates
data error kinds. Per-instrument history coverage separates short contiguous
histories from internal missing sessions and wholly absent data. Insufficient
history remains ineligible; it is not assumed to be a confirmed IPO. A partial
snapshot generates an explicit ranking-coverage warning. These diagnostics do not
weaken the existing 127-session completeness requirement.

The exact setup rules remain in [scanner-rules.md](scanner-rules.md). Automated
universe/data orchestration does not implement entries, stops, sizing or exits.
