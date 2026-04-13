# Auction IQ NTM VolX Proxy

## Why this exists

Vtrender and Shai publicly describe `NTM VolX` as a near-the-money options control lens:

- near-the-money options volume visualization
- call-versus-put pressure
- "who is in control right now"
- `VXR` ranges that separate balanced from one-sided control

The exact proprietary formula is not public, so Auction IQ uses a transparent proxy built from the option-chain fields already available in our broker adapters.

## Public references used

- https://vtrender.com/pillar/learning-pathway
- https://vtrender.com/pillar/ntm-volx
- https://vtrender.com/glossary/ntm-volume-option-charts
- https://vtrender.com/posts/ntm-volx-guide-how-to-read-control-in-the-options-market

## Current proxy

For the nearest `N` CE/PE strike pairs around spot:

1. Build a per-side pressure score for each strike pair.
2. Weight closer strikes more heavily than farther strikes.
3. Penalize poor-liquidity contracts using spread percentage.
4. Use two demand components:
   - premium turnover: `ltp * volume`
   - positive OI addition: `max(oi - prev_oi, 0) * ltp * oi_change_multiplier`
5. Slightly reward same-side premium expansion when `prev_close` is available.

Per-entry pressure is:

`distance_weight * liquidity_weight * (premium_turnover + oi_notional) * premium_weight`

Then aggregate:

- `call_pressure = sum(call_entry_pressure)`
- `put_pressure = sum(put_entry_pressure)`
- `net_pressure = (call_pressure - put_pressure) / (call_pressure + put_pressure)`
- `VXR = dominant_pressure / opposing_pressure`

## Interpretation in Auction IQ

- `dominant_side = CALLS` maps to directional bias `LONG`
- `dominant_side = PUTS` maps to directional bias `SHORT`
- near-balanced readings map to `FLAT`

Regime buckets:

- balanced
- calls_lean / puts_lean
- calls_control / puts_control
- calls_extreme / puts_extreme

## Where it is used

- `analysis.ntm_volx` API payload
- Auction IQ operator/workspace charts
- sleeve-confidence overlay
- counter-bias suppression when NTM pressure is extreme
- option strike selection bonus inside the execution mapper

## Important limitation

This is an inferred implementation inspired by Vtrender's public descriptions, not a reverse-engineered clone of their internal chart. If better broker-side fields become available, the first upgrades should be:

- intraday option volume deltas instead of snapshot volume totals
- better writer-versus-buyer inference
- strike-by-strike history, so `VXR` can be trended over time instead of read as a single snapshot
