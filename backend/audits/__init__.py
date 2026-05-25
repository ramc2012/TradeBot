"""Lane audit framework — verify each trading lane against the six-invariant checklist.

A lane is "trustworthy" only when all six pass:
  1. Data integrity        — feed exists, no gaps, fresh
  2. Replay parity         — pure recompute matches live agent_signals byte-for-byte
  3. Gate attribution      — every blocked candidate logs which gate blocked it
  4. Backtest⇄live parity  — same window produces identical signal sets
  5. Trade reconciliation  — every paper trade reconciles to ticks within ±2s
  6. Edge persistence      — rolling 60d expectancy still positive vs 1y baseline
"""
