# Backtest the daily breakout screen

The first backtest measures whether the current daily filters select stocks
with better subsequent price returns than other eligible stocks in the same
IBKR shortlist. It uses the scanner outcomes recorded before each entry
session. The baseline rules are in [rules.md](../rules.md).

## Freeze the experiment

Before collecting the evaluation period, record the Git revision, scanner
mode, configuration, planned sessions, holding period and cost assumption.
Start with `--ibkr-scanner --config configs/breakout-review.json`, five
holding sessions and 10 basis points of modeled cost per side. These are
research assumptions. Run the same setup every session, including days when
it produces zero candidates. Use a later, untouched period to evaluate any
change to the 15-session base or prior-advance calculation.

Archive each full daily run directory after it finishes. Use a consistent
collection time after the completion lag and before the next regular US
session opens. Keep the existing scan report, snapshot and discovery
metadata with their original timestamps. The current IBKR shortlist,
downloaded history and present-day listing files cannot recover past
shortlists. IBKR also documents that historical bars may vary by request
date and may be unavailable for delisted stocks.

## Timing, outcomes and costs

1. **Signal.** The original archived scan determines the candidate and control
   groups. The control group is the same day's shortlist members that passed
   data, price and liquidity checks but missed a setup rule. The runner checks
   the report and snapshot hashes, and verifies that the decision, snapshot
   and report timestamps precede the next NYSE session open. Older reports
   without a recorded report creation time cannot be used.
2. **Entry.** Model a long entry at the next session's regular-hours open.
   Use the NYSE calendar for weekends, holidays and early closes. By default,
   hold through the close of the fifth session, counting entry day as day one.
3. **Outcome prices.** Use a later, separate regular-hours daily snapshot
   from the same source and price basis. It supplies future prices only
   for scoring the archived signals. Match both instrument key and IBKR
   contract ID. The outcome snapshot's request cutoff must be at or after
   the exit session's close. Collect after the daily bar settles.
4. **Model.** Per event, net price return is
   `exit_close * (1 - cost) / (entry_open * (1 + cost)) - 1`.
   The cost is a fixed proxy for fees, spread and slippage. Each event
   receives equal weight within its group. Daily opens and closes are bar
   prices, so observed returns are conditional on the fill assumption.
   Use entry and exit prices from the same history vintage. This is a
   price-return comparison without a separate dividend cash-flow model.
5. **Missing outcomes.** Keep every selected event in the report. A still-open
   holding window, a missing or zero-volume bar, or an unresolved or changed
   contract ID makes its return unavailable. A group mean requires all its
   outcomes. A paired comparison requires both group means for that day.
   Record missing scan dates within the supplied date range and check the
   endpoints against the planned period. Check archive coverage and missing
   outcomes before interpreting complete-day averages.

The headline statistic is the average of the daily candidate-minus-control
differences on paired sessions, weighting days equally. Report the number
of candidate events, paired sessions, and missing observations alongside it.
Overlapping holding windows create dependent returns. The output is an
event study rather than a capital-constrained portfolio simulation.

## Run it

Capture each signal session with the current version of the software:

```bash
python -m ib_lab daily --port 7496 --ibkr-scanner \
  --config configs/breakout-review.json --out runs/bt-session-01
```

List the archived runs from the planned period to generate the symbols for
outcome collection. Use a fresh output directory each time:

```bash
python -m ib_lab backtest --runs runs/bt-session-* --out runs/bt-universe
```

Once the last holding window has ended, collect the outcome history from
TWS and score the same archive:

```bash
python -m ib_lab download --port 7496 \
  --universe runs/bt-universe/outcome-universe.csv --out data/bt-outcomes
python -m ib_lab backtest --runs runs/bt-session-* --outcomes data/bt-outcomes \
  --holding-sessions 5 --cost-bps 10 --out runs/bt-results-01
```

`backtest.json` includes the cohorts, event prices and statuses, counts,
input fingerprints and paired comparison. Multiple archives must use the
same strategy version, configuration, ranking scope and data source.
The timestamp checks establish chronology from the recorded metadata.
Trusted originals are required to establish when the inputs were captured.

## Execution replay

The full strategy test needs chronological intraday observations and the
entry and exit rules in the [project specification](project-spec.md).
Build the opening range from completed 09:30–09:35 bars, look for the first
eligible crossing after the range closes, and use the session low known at
that point for the initial stop. Treat ambiguous intrabar orderings
conservatively or use finer data. Model fees, spread, adverse fills,
overnight gaps, partial exits and overlapping positions before reporting
portfolio P&L. Human approval requires decisions recorded before entry.

For a longer historical study, use dated membership and bars available at
each decision time, including delisted names. Substituting a historical
universe for saved IBKR scan hits defines a different discovery rule.

[IBKR historical data limitations](https://www.interactivebrokers.com/docs/tws-api/doc/md/market-data-limitations)
