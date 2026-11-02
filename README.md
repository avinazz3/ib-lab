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
new directory after addressing failures; this release does not retry or resume
requests automatically. `--request-spacing` provides local pacing, and the SDK
also throttles messages. A larger production universe will require further
request budgeting and incremental caching.

IBKR's native scanner can be a future candidate source. This screen currently
operates over your supplied universe, avoiding any claim that a limited broker
scanner result covers the whole market.

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
All 25 tests pass on Python 3.12 and 3.14. Calendar dependencies currently emit
pandas deprecation warnings. Broker tests use fakes; an actual TWS/Gateway smoke
test still needs to be run by
the account holder. No real IBKR market data is included in the repository.

Exit codes: `0` success (including zero candidates), `1` partial data/scan,
`2` configuration/connection/input error, `130` interruption. A quote diagnostic
can succeed with no two-sided quote; inspect its status rather than treating
exit code zero as a declaration of market-data entitlement.
