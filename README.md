# StocksDailyStatus

Personal stock-status tracking project.

## OFS cutoff analyzer

The repository includes a fail-closed live non-retail OFS cutoff workflow. It
combines exact-price NSE and BSE bidding ladders and publishes a marginal price
only after cross-exchange reconciliation.

See [README_OFS.md](README_OFS.md) for workflow inputs, methodology, and result
interpretation.

## Strategy automation

Rule-based strategies, a code-level risk gate, an optional AI veto reviewer, and
paper/Zerodha Kite execution with a scheduled GitHub Action. See
[README_STRATEGIES.md](README_STRATEGIES.md).
