---
symbol: {symbol}
created: {created}

# Share of the portfolio you intend this to be, in % (must not exceed the
# single-stock limit in config/risk.yaml).
target_weight_pct:

# Metrics the thesis depends on. Percentages in % (roce: 18 means 18%), ratios as plain
# numbers (ocf_pat: 0.9). buy_value is the figure when you bought; tolerance is how far it
# may drift before the quarterly review calls it out.
# Known metrics: {metrics}
metrics:
  - {{metric: roce, buy_value: , tolerance: }}

# Kill criteria: pre-committed sell rules. Each must be machine-checkable, one of
#   - {{id: k1, metric: ocf_pat, op: "<", value: 0.7, periods: 2, text: "cash conversion breaks"}}
#   - {{id: k2, rule: auditor_resignation}}        # a rule id from config/red-flags.yaml
kill_criteria:
  - {{id: k1, metric: , op: "<", value: , periods: 2, text: ""}}
---

# {symbol} — investment thesis

## How it earns
TODO

## Moat
TODO

## What breaks it
TODO

## Why now / valuation
Optional.
