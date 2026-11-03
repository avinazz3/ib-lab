# ib-lab

Explainable momentum research and read-only Interactive Brokers tooling using
[`ib_async`](https://github.com/ib-api-reloaded/ib_async).

The first milestone implements an IBKR connection/data probe, daily-data snapshots,
and an offline breakout scanner inspired by
[Qullamaggie's setups](https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/).
Its numerical filters are research hypotheses. No profitability is established.

The data adapter uses `ib_async`, session scheduling uses `exchange-calendars`,
ATR/true range use TA-Lib, rankings use pandas, and storage uses PyArrow. Small
project functions connect these libraries and express the strategy-specific rules.

## What works now

- `daily`: automatic listing discovery, transactional incremental history cache,
  and the existing scanner in one command. See [daily workflow](docs/daily-workflow.md).
- `probe`: read-only connection, contract qualification, daily/minute history, and a
  quote diagnostic that distinguishes requested and observed market-data types.
- `download`: serial, paced daily history collection for a supplied universe;
  compressed Parquet, contract IDs, provenance, checksums, and per-symbol failures.
- `scan`: completed-session breakout features, cross-sectional rankings, and
  explicit acceptance/rejection reasons for every instrument.
- `demo`: a deterministic, entirely synthetic snapshot to exercise the workflow.

This milestone contains no order-submission functionality. Intraday entries,
historical execution simulation, portfolio management, episodic pivots, and
parabolic trades are subsequent milestones. See [the project specification](docs/project-spec.md).

## Install

Use a virtual environment. The local test suite passes on Python 3.12 and 3.14;
the package declares Python 3.11+ and CI also exercises Python 3.13.

With [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev --python 3.12
uv run ib-lab --help
```

Or with an existing Python 3.12 installation:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
ib-lab --help
```

The `uv.lock` file pins the uv environment. Plain pip follows the bounds in
`pyproject.toml` rather than the lockfile. There is no separate `ibapi` dependency.

## Try it without IBKR

Run from the repository root. Choose a new output directory for each run:

```bash
uv run ib-lab demo --out data/demo-001
uv run ib-lab scan \
  --snapshot data/demo-001 \
  --as-of 2026-10-30T22:00:00Z \
  --config configs/breakout.json \
  --out runs/demo-001
```

Expected: 100 fabricated instruments, 100 rank-eligible instruments, one candidate
(`DEMO099`), zero data errors. This verifies program behavior using constructed
data; it is not a historical strategy result.

Inspect `runs/demo-001/report.json` for all feature values, checks, reasons,
configuration hash, and input hashes. `candidates.csv` contains only passing rows.
`candidate-events.csv` contains up to six dated price/volume observations per candidate.

## Review a broader shortlist

Use the approved review configuration with either `scan` or `daily`. It allows
base depth up to 25%, a contraction ratio up to 1.0 and distance from SMA20 up to
2 ATR; rising support is informational. Price, liquidity, leadership, impulse,
rising SMA20 and data-quality requirements remain mandatory. The default and
`configs/breakout.json` preserve the strict rules.

To compare rules, rescan the same snapshot with the same original decision time.
For example, for the saved daily run ending on 2 November 2026:

```bash
python -m ib_lab scan \
  --snapshot runs/daily-20261103T042251-132922Z/snapshot \
  --as-of 2026-11-03T04:02:51.132922+00:00 \
  --config configs/breakout-review.json \
  --out runs/review-001
```

This is offline and makes no IBKR requests. Replace the paths and timestamp for
other snapshots; each output directory must be new. Existing partial-data status
and exclusions still apply, so this run may return exit 1 while producing reports.

Review `candidates.csv` for the summary, return start dates and check results;
`candidate-events.csv` for dated price changes, opening gaps, volume and relative
volume; and `report.json` for all instruments, provenance and research coverage.
Return/change values are fractions (0.05 = 5%); relative volume is a multiple.
Use the dates for external news research, then make your own review decision.
This change does not implement approval storage, sizing or order execution.

For subsequent daily collection with the review rules:

```bash
python -m ib_lab daily --port 7496 --config configs/breakout-review.json
```

## Run the automatic daily workflow

With your current live TWS connection in Read-Only API mode:

```bash
python -m ib_lab daily --port 7496 --seed-snapshot data/real-001
```

This discovers a broad current universe, imports compatible history from your
existing snapshot, updates the cache, and writes one consolidated scan report.
Run the same command subsequently; existing cache entries are preserved. Output
directories are timestamped automatically. Default cache: `data/history-cache.sqlite`.

Initial collection for several thousand stocks can take hours. Each completed
stock is checkpointed; interruption does not discard that work. An optional
`--max-downloads 250` bounds the number of stocks needing requests in one invocation;
unprocessed listings remain visible as failures and the report is partial. A rerun
continues from checkpoints. `--max-symbols 100` is an explicitly labelled alphabetical
subset for testing; its rankings do not represent the broad universe.

The 20-minute completion lag defaults to excluding a just-closed session while
your data is delayed. This is a timing buffer, not proof of data freshness. The
workflow is read-only and does not monitor intraday entries or place orders.

## Connect on your own machine

1. Log into an IBKR **paper** session in TWS or IB Gateway.
2. Enable socket API access. Keep **Read-Only API enabled in TWS/Gateway**.
3. Confirm the actual API port. Common paper defaults are TWS `7497` and Gateway
   `4002`, but settings can differ. A port number does not prove account mode.
4. Confirm US stock API market-data subscriptions. Data visible in TWS does not
   automatically establish API entitlement. Never commit login credentials.
5. Run the probe on the same machine as TWS/Gateway, or supply an explicitly
   reachable host through your own network configuration.

```bash
uv run ib-lab probe --symbol AAPL --primary-exchange NASDAQ \
  --port 7497 --client-id 31 --out runs/probe-001
```

The report records SDK/server versions, history counts, observed quote type, and
broker messages. Types: `1` live, `2` frozen, `3` delayed, `4` delayed frozen. You
can request `--data-type 3` for a delayed-data diagnostic. Historical entitlements
are a separate check: delayed quotes do not guarantee historical access.

Run during the regular session for the most useful live-quote diagnostic. Receiving
bars or a delayed/frozen quote does not establish an execution-ready feed. The
probe never marks a feed as ready to trade.

The adapter passes `readonly=True`. This client flag controls client behavior;
the TWS/Gateway Read-Only API setting provides broker-side order protection.
The adapter exposes only data methods and no code submits or cancels orders.

## Collect and screen real data

The input CSV requires `symbol,primary_exchange`; see
[`examples/universe.csv`](examples/universe.csv). You curate eligible common-stock
listings: qualifying an IBKR `STK` contract alone does not exclude ETFs or ADRs.

```bash
uv run ib-lab download --universe examples/universe.csv \
  --port 7497 --client-id 31 --out data/us-001
```

Then scan using a timestamp after the final session represented in the snapshot
has closed. For example, for a snapshot containing complete history through
30 October 2026:

```bash
uv run ib-lab scan --snapshot data/us-001 \
  --as-of 2026-10-30T22:00:00Z \
  --config configs/breakout.json --out runs/us-001
```

Replace the timestamp for your actual data. `--as-of` always requires an explicit
UTC offset. Exchange holidays, daylight-saving changes, and early closes come
from the XNYS calendar. The current daily bar is excluded until its session closes.

The ten-name example is for connectivity and feature inspection. It is **not** a
full US market universe. At the default leadership threshold it cannot select a
candidate: its maximum midrank percentile is 95. Supply a broader dated universe
for useful leadership screening. The scanner reports the denominator and warns
when fewer than 50 instruments are rank-eligible.

Downloads request two years of native regular-session `TRADES` daily bars. Keep
this price basis separate from other providers' adjusted data. Snapshots preserve
the downloaded vintage; they do not reconstruct what historical data or universe
membership was available on a past date. There is no delisting dataset here.

Each snapshot/run path must be new. A failed symbol makes the snapshot `partial`
and the command exit nonzero. Complete symbols remain inspectable. Re-run into a
new directory after addressing failures; the `download` command does not resume.
Use `daily` for checkpointed incremental updates. `--request-spacing` provides local pacing, and the SDK
also throttles messages. A larger production universe will require further
request budgeting and incremental caching.

The `scan` command operates over a supplied snapshot. `daily` can discover current
listings automatically from Nasdaq Trader, or accept an explicit universe CSV.
Neither relies on a limited broker-scanner result as if it covered the whole market.

## Rules and interpretation

See [`docs/scanner-rules.md`](docs/scanner-rules.md) for exact formulas. Default
parameters live in `configs/breakout.json`. Unknown configuration keys fail.

Only instruments with a complete most-recent 127-session history, valid OHLCV,
and sufficient price/liquidity enter the ranking denominator. Ties use midrank
percentiles: `100 * (count_less + 0.5 * count_equal) / count`. Identical returns
all score 50 rather than all becoming leaders.

The daily scanner produces a **setup candidate**, not a buy order. Intraday
opening-range confirmation, sizing, initial stops, and exits are not implemented
yet. None of the daily outputs should be interpreted as a completed trade signal.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests cover a constructed accepted setup, missing/invalid data, duplicate sessions,
future-data isolation, incomplete sessions, early closes, DST, ranking ties,
checksums, read-only connections, request failures, and quote-subscription cleanup.
The original 25 tests passed on Python 3.12 and 3.14; additional tests cover
discovery, incremental caching, seeding, interruption and locking. Calendar dependencies emit
pandas deprecation warnings. Broker tests use fakes; an actual TWS/Gateway smoke
test still needs to be run by
the account holder. No real IBKR market data is included in the repository.

Exit codes: `0` success (including zero candidates), `1` partial data/scan,
`2` configuration/connection/input error, `130` interruption. A quote diagnostic
can succeed with no two-sided quote; inspect its status rather than treating
exit code zero as a declaration of market-data entitlement.
