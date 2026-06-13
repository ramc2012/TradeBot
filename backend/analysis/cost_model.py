"""
ONE shared NSE F&O option cost model for every backtest / walk-forward.

WHY THIS EXISTS
---------------
Cost assumptions were scattered and inconsistent across the analysis harnesses:
  - analysis/signal_backtest.py  : 2 bps flat            (far too small for options)
  - analysis/s1_bt.py            : 1.5%/side ~ 3% round  (premium %)
  - nifty_atm_*_walkforward.py   : ROUND_TRIP_COST_PCT=3.0
  - directional_options/config   : 0.75% entry + 0.6% exit slippage
  - analysis/s1_walkforward.py   : ZERO cost  <-- the +2489% June mirage
Zero / tiny cost is the classic phantom-edge generator, and this codebase has a
documented 32/32-OOS-negative history. Costs can only ever make a result MORE
conservative, so a single realistic model wired into every harness is pure
downside-protection: nothing that survives it is *worse* than reality, and most
apparent intraday-option edge collapses once a real ~3% round-trip is charged.

THE MODEL (premium-relative, per ATM-ish weekly/monthly index option)
---------------------------------------------------------------------
Cost is dominated by the bid-ask spread you cross, not statutory charges. We
model it as a fraction of the *entry premium* for a full buy->sell round trip:

    spread (entry half + exit half)   ~ 2.5%   (slippage_pct, the big one)
    STT (0.0625% of SELL premium)     ~ 0.06%
    exchange txn (~0.035% per side)   ~ 0.07%
    SEBI + stamp + GST-on-charges     ~ 0.05%
    brokerage (flat, premium-scaled)  ~ varies (~Rs20/order / notional)
    -----------------------------------------
    headline round trip               ~ 3.0% of entry premium

This reproduces the conservative s1_bt.py / nifty_atm 3% figure but is itemized
and tunable, and it scales the flat brokerage by the premium notional so cheap
options (where Rs20/order is a big %) are charged correctly.

Pure stdlib + dataclass. No pandas/app import -> safe to use anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NseOptionCostModel:
    """Itemized NSE index-option round-trip cost as a fraction of premium.

    All *_pct fields are fractions (0.0125 == 1.25%), not basis points.
    Defaults are calibrated so a typical ATM index option round-trips at ~3% of
    entry premium — matching the conservative numbers already hard-coded in
    s1_bt.py (1.5%/side) and the nifty_atm walk-forwards (ROUND_TRIP_COST_PCT=3).
    """

    # Bid-ask you cross, per leg, as a fraction of that leg's premium. This is
    # the dominant cost and is deliberately conservative for ATM index weeklies;
    # raise it for illiquid strikes via .scaled_for_liquidity().
    entry_slippage_pct: float = 0.014
    exit_slippage_pct: float = 0.014
    # Statutory / exchange (fractions of premium).
    stt_sell_pct: float = 0.000625      # STT 0.0625% on SELL-side option premium
    exchange_txn_pct: float = 0.00035   # ~0.035% per side, NSE option premium
    sebi_stamp_pct: float = 0.00010     # SEBI turnover + stamp (buy) ~ combined
    gst_pct: float = 0.18               # GST on (brokerage + exchange txn)
    # Flat brokerage per order (two orders per round trip). Scaled to a % of
    # premium via the contract notional so cheap options are charged correctly.
    brokerage_per_order: float = 20.0

    def round_trip_pct(self, entry_premium: float, lot_qty: float | None = None) -> float:
        """Round-trip cost as a FRACTION of entry premium.

        With `lot_qty=None` (default) returns the premium-RELATIVE components
        only (spread + statutory) — the ~3% headline used by premium-% harnesses
        that don't track contract notionals. Pass the real `lot_qty` (contract
        multiplier x lots, e.g. 50 for NIFTY) to additionally charge the flat
        per-order brokerage, which correctly penalizes cheap options.
        """
        p = float(entry_premium)
        if p <= 0:
            return 0.0
        spread = self.entry_slippage_pct + self.exit_slippage_pct
        txn = self.exchange_txn_pct * 2.0          # both legs
        stt = self.stt_sell_pct                    # sell leg only
        sebi_stamp = self.sebi_stamp_pct
        gst = self.gst_pct * txn
        total = spread + txn + stt + sebi_stamp + gst
        if lot_qty is not None and lot_qty > 0:
            notional = p * lot_qty
            brokerage_frac = (2.0 * self.brokerage_per_order) / notional if notional > 0 else 0.0
            total += brokerage_frac + self.gst_pct * brokerage_frac
        return total

    def net_return_pct(self, gross_return_pct: float, entry_premium: float = 100.0, lot_qty: float | None = None) -> float:
        """Subtract the round-trip cost from a gross premium-% return (fraction)."""
        return float(gross_return_pct) - self.round_trip_pct(entry_premium, lot_qty)

    def net_pnl(self, entry_premium: float, exit_premium: float, lot_qty: float = 1.0) -> float:
        """Absolute net P&L for buying `lot_qty` at entry, selling at exit, net of cost."""
        q = max(lot_qty, 1.0)
        gross = (float(exit_premium) - float(entry_premium)) * q
        cost = self.round_trip_pct(entry_premium, lot_qty) * float(entry_premium) * q
        return gross - cost

    def scaled_for_liquidity(self, spread_multiplier: float) -> "NseOptionCostModel":
        """Return a copy with the bid-ask legs widened (e.g. 2.0 for illiquid OTM)."""
        from dataclasses import replace
        return replace(
            self,
            entry_slippage_pct=self.entry_slippage_pct * spread_multiplier,
            exit_slippage_pct=self.exit_slippage_pct * spread_multiplier,
        )


# The single default every harness should import. ~3% round trip on a typical
# ATM index option premium.
NSE_OPTION_COST = NseOptionCostModel()

# A bare scalar for the simplest premium-% harnesses that just want one number.
ROUND_TRIP_COST_PCT: float = NSE_OPTION_COST.round_trip_pct(100.0)


if __name__ == "__main__":
    m = NSE_OPTION_COST
    for prem in (5.0, 20.0, 80.0, 200.0):
        print(f"premium Rs{prem:6.1f}  round-trip = {m.round_trip_pct(prem, lot_qty=50)*100:5.2f}%  "
              f"(spread-only approx {m.round_trip_pct(prem)*100:5.2f}%)")
