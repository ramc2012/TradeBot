# (B) OUR PANEL — the 2-3 day horizon, re-derived from our own data

Date: 2026-07-20. Research only; nothing wired, no flags changed.

Code: `backend/directional_options/research/panel_2d3d/`
(`extract_panel.py` → `build_panel.py` → `analyse.py`; full numeric dump in
`results.txt`). All PG queries bound `time` directly with literal UTC
timestamps — no function on the partitioning column.

## Panel construction

- Source: `option_premium_candles` (interval `30minute`) and
  `underlying_spot_candles` (`30minute`), 2025-01-01 → 2026-07-20.
- One snapshot per contract per session at the **15:15 IST bar** (09:45 UTC),
  i.e. the decision bar the owner's design would act on.
- 437,943 contract-sessions · 22,288 contracts · 217 underlyings · 382 sessions.
- Forward returns are joined **by the underlying's session index**, not by row
  position, so a collection gap cannot silently shorten the horizon.
- ATR14 is a true daily ATR built from 30m session bars, computed through the
  snapshot session close — causal at the decision bar.
- Moneyness is signed: negative = ITM, positive = OTM, in % of spot.
- Corrupt spot rows (the known cross-symbol tick contamination) were removed by
  nulling `atr_pct` outside (0.05%, 15%).

---

## 1. Holdability at 2-3 sessions — the prior number is REFUTED

Median premium change over the next **3 sessions**, monthly contracts,
premium ≥ Rs 1, all underlyings:

| moneyness | DTE 3-7 | DTE 8-22 | DTE 23+ |
|---|---|---|---|
| deep ITM (< −3%) | **−3.9%** | **−12.5%** | −20.4% |
| slight ITM (−3..−0.75%) | −22.1% | **−17.9%** | −16.2% |
| ATM (±0.75%) | −58.9% | −17.6% | −8.8% |
| slight OTM (0.75..3%) | −85.9% | −16.7% | +0.9% |
| far OTM (> 3%) | −87.5% | −24.2% | +14.1% |

At 2 sessions the same shape holds (slight-ITM DTE 8-22: −13.2%).

**Pure carry** — the same cells restricted to sessions where the spot moved
less than 0.25×ATR over the 3 days, i.e. what you pay to just hold:

| moneyness | DTE 3-7 | DTE 8-22 | DTE 23+ |
|---|---|---|---|
| deep ITM | −1.5% | **−3.9%** | −3.8% |
| slight ITM | −17.1% | −9.6% | −6.3% |
| ATM | −60.7% | −16.4% | −8.0% |
| slight OTM | −86.3% | −23.7% | −9.4% |
| far OTM | −88.9% | −36.9% | −13.1% |

**Verdict on the prior claim.** The remembered finding — *slightly-ITM,
DTE 8-22, monthly ⇒ ≈ 0% median 5-day premium change* — **does not survive**.
On this panel that cell bleeds **−17.9% over 3 sessions**, and still **−9.6%
with the spot flat**. It is stable quarter by quarter (2025Q2..2026Q2 flat-spot
carry: −13.1, −9.2, −8.7, −9.9, −11.4%), so it is not a one-regime artifact.
The prior study was CE-only, 20 names, one regime, and additionally
conditioned on `d_iv ≥ 0`; that conditioning, not the moneyness bucket, was
carrying the result.

**What IS holdable at this horizon is DEEP ITM, not slight ITM.** Deep ITM
(< −3%, i.e. ~0.65-0.8 delta) at DTE 8-22 carries at −3.9% over 3 flat
sessions — roughly a quarter of the slight-ITM bleed and a sixth of ATM. This
is the mechanically obvious answer (mostly intrinsic, little extrinsic to
decay) and it is the single most useful instrument-selection fact in this pass.

**ATM ≈ −37% / weekly ≈ −90% (prior, 5-day):** partially confirmed but on the
**DTE axis, not the weekly label**. Our panel is ~99.3% monthly-expiry
contracts (only 3,057 non-monthly rows) because the collector tracks monthly
chains — we **cannot** test "weekly vs monthly" as such. But DTE 3-7 (what a
weekly functionally is) shows ATM −58.9% and slight-OTM −85.9% over 3 sessions,
so the "weeklies are unholdable" conclusion stands on the DTE mechanism. ATM at
DTE 8-22 is −17.6% at 3 days (the −37% figure was 5 days), consistent.

---

## 2. A spot-ATR barrier in premium terms

**Realised premium response** (median 3-session premium % change, side-aligned,
by directional spot move measured in ATR units) — this is the honest
translation table, not a delta approximation:

DTE 8-22, monthly:

| moneyness | −1.5..−1 ATR | −0.5..+0.5 | +0.5..1 | +1..1.5 | +1.5..2 | >2 |
|---|---|---|---|---|---|---|
| deep ITM | −45.7% | −4.1% | +22.8% | +40.4% | +59.7% | +87.5% |
| slight ITM | −55.4% | −10.6% | +29.0% | +55.1% | +80.9% | +119.1% |
| ATM | −62.5% | −16.6% | +29.3% | +63.7% | +99.6% | +154.6% |
| slight OTM | −68.3% | −22.9% | +26.0% | +64.8% | +110.3% | +187.7% |
| far OTM | −68.1% | −37.3% | +14.1% | +61.1% | +116.2% | +253.3% |

DTE 3-7 is far more violent (ATM: −92% at −1 ATR, +130% at +1.25 ATR) and
DTE 23+ far more muted (ATM: −44% / +42%).

Equivalently, realised **elasticity** ω = (premium %Δ)/(directional spot %Δ)
over the hold, restricted to moves > 0.5 ATR (median):

| moneyness | DTE 3-7 | DTE 8-22 | DTE 23+ |
|---|---|---|---|
| deep ITM | 15.2 | 13.0 | 10.3 |
| slight ITM | 32.7 | 20.2 | 14.8 |
| ATM | 35.6 | 22.9 | 16.6 |
| slight OTM | 28.6 | 22.3 | 16.1 |

Median ATR14: **index 1.15% of spot**, **stock 2.60% of spot**. So a
1×ATR barrier translates to roughly:

- index, slight-ITM/ATM DTE 8-22 → **≈ +23-26% premium**
- stock, same → **≈ +52-60% premium**
- 1.5×ATR: index ≈ +35-40%, stock ≈ +79-89%

**Critical asymmetry the design must absorb:** the response is *not*
symmetric. At DTE 8-22 a −1 ATR adverse move costs ~−50 to −60% of premium
while a +1 ATR favourable move earns only ~+29%, because theta and the vega/IV
drop work with the loss and against the gain. A symmetric spot-ATR barrier
(±1 ATR) is therefore a **very asymmetric premium barrier** — roughly 2:1
against you. Any 1:1 spot-ATR stop/target is a losing geometry at the option
level even with a coin-flip-accurate signal.

**Barrier hit frequency within 3 sessions** (from the EOD close, touch basis;
daily bars cannot order two touches in the same window):

| | target 1×ATR | stop 1×ATR | both touched | neither |
|---|---|---|---|---|
| INDEX (n=1,858) | 37.4% | 39.3% | 3.4% | 26.7% |
| STOCK (n=66,789) | 36.8% | 34.3% | 3.5% | 32.4% |

At 1.5×ATR target / 1.0×ATR stop: index target 19.9% vs stop 39.3%; stock
20.1% vs 34.3%. **~30% of trades reach neither barrier and exit on the 3-day
time stop** — and the time-stop exit is precisely the flat-spot carry cell
(−10% slight-ITM, −16% ATM, −4% deep ITM). Time-stop bleed is not a rounding
error at this horizon; it is a third of all outcomes.

---

## 3. BANKNIFTY `oi_build_bias` fwd3 — NOT confirmed in the current panel

`directional_positioning_daily` only goes back to 2025-12-09 (1,017 rows,
8 underlyings, only 5 with usable history). Directional forward-3 return, sign
of `oi_build_bias` taken as the side:

| underlying | n | Spearman IC(bias, fwd3) | dir fwd3 mean | hit | t |
|---|---|---|---|---|---|
| BANKNIFTY | 142 | +0.047 | **+0.169%** | 53.5% | +0.94 |
| SENSEX | 133 | +0.101 | +0.183% | 54.1% | +1.33 |
| NIFTY | 133 | +0.085 | −0.119% | 51.1% | −0.80 |
| FINNIFTY | 111 | −0.084 | −0.173% | 44.1% | −0.77 |
| MIDCPNIFTY | 113 | +0.006 | −0.043% | 49.6% | −0.20 |

With the `d_atm_iv ≥ 0` gate: BANKNIFTY n=66, +0.088%, hit 56.1%, t=+0.34;
SENSEX n=62, +0.404%, hit 56.5%, t=+1.94.

**Verdict.** The BANKNIFTY fwd3 edge (prior: +0.54%, 62% hit, p=0.033) is
**not reproduced** — same sign, one third the magnitude, statistically
indistinguishable from zero (t=+0.94). NIFTY still inverts, which is
consistent with the prior "it is index-scoped". The only cell that looks
interesting now is **SENSEX** (+0.18% raw, +0.40% under the IV gate, t=1.94) —
and I would treat that as **noise-shopping across 5 underlyings × 2 gates
until proven otherwise**; at 5 underlyings, one t≈2 is the expected maximum
under the null. Nothing here clears costs: +0.18% of spot × ω≈20 ≈ +3.6% of
premium, versus a realistic 1.6-4% round-trip. **No analogue elsewhere.**

Caveat: this is 6 months of data, not the multi-year sample the original
finding used. This is a *failure to confirm on new data*, not proof the
original was wrong — but it is exactly the out-of-sample test that matters,
and the edge did not show up.

---

## 4. Round-trip cost vs the expected move

Statutory + discount brokerage on premium turnover (Rs 20/order both legs,
STT 0.1% sell premium, exchange 0.035%, SEBI, stamp, GST 18%):

| premium notional | round-trip cost | as % of premium |
|---|---|---|
| Rs 5,000 | Rs 56.5 | **1.13%** |
| Rs 20,000 | Rs 84.4 | 0.42% |
| Rs 50,000 | Rs 140.1 | 0.28% |
| Rs 200,000 | Rs 418.9 | 0.21% |

**The panel has no bid/ask** — spread must be assumed and it dominates.
Working bands: index near-ATM monthly ≈ 0.3% per side ⇒ **0.6% round trip**;
liquid stock options ≈ 0.8% per side ⇒ **1.6%**; thin strikes 2%+ per side ⇒
**4%+**. Note the tick is Rs 0.05 and median premium for far-OTM DTE 23+ is
Rs 15 — one tick alone is 0.33%, and those strikes quote several ticks wide.

Against the realised median |3-session premium move|:

| bucket | median \|3d move\| | cost@0.6% | cost@1.6% | cost@4% |
|---|---|---|---|---|
| slight ITM, DTE 3-7 | 63.0% | 1.0% | 2.5% | 6.3% |
| slight ITM, DTE 8-22 | 36.7% | 1.6% | 4.4% | 10.9% |
| ATM, DTE 8-22 | 40.5% | 1.5% | 3.9% | 9.9% |
| slight OTM, DTE 8-22 | 45.2% | 1.3% | 3.5% | 8.8% |

**Cost is NOT what kills the 2-3 day horizon.** At 1.6% round trip it is
3-4% of the typical move — an order of magnitude less lethal than for the
intraday fade candidate. This is the one clearly *favourable* finding: the
horizon is cost-tolerant. What kills it is carry (§1) and barrier asymmetry
(§2), not friction. Corollary: do not size positions so small that the flat
Rs 40 brokerage becomes 1.1% — keep premium notional ≥ Rs 20,000 per leg.

---

## 5. What directional accuracy is actually required

For a signal that calls the sign of the 3-session spot move with probability p:
E[ret] = p·E[ret|right] + (1−p)·E[ret|wrong]. Means winsorised at 1%/99%.

| moneyness | DTE | p break-even | p @1.6% cost | p @4% cost | p break-even (MEDIAN basis) |
|---|---|---|---|---|---|
| deep ITM | 8-22 | 57.8% | 60.2% | 63.9% | 64.3% |
| slight ITM | 8-22 | 54.8% | 56.8% | 59.8% | 63.7% |
| ATM | 8-22 | 53.9% | 55.7% | 58.4% | 65.7% |
| ATM | 23+ | 55.9% | 58.9% | 63.4% | 65.1% |
| slight OTM | 8-22 | 54.7% | 56.4% | 58.8% | 69.6% |
| far OTM | 23+ | **37.4%** | 39.2% | 41.9% | 47.4% |

Baseline P(3-session spot move up) in this panel = 50.8%.

Read this as the bar the owner's MA/ADX/RSI/MACD construct must clear:
**~56-60% directional accuracy at a 3-day horizon on the mainstream buckets**,
against a prior body of evidence in which every trend construct we have tested
had a *negative* IC. That is a very high bar for a construct our own research
has repeatedly measured as anti-predictive.

**The one anomaly: far-OTM, DTE 23+.** Break-even accuracy 37.4%, stable in
every quarter measured (2025Q2..2026Q3: 0.391, 0.396, 0.392, 0.335, 0.388,
0.306), and it survives dropping the top 1% of winners (0.392). This is pure
positive convexity / long-vega — you need to be right less than half the time
because the right tail is much fatter than the left. **I am not recommending
it and it is not validated:** (a) on the median basis it needs 47.4%, i.e. a
coin flip, so the advantage is entirely a tail phenomenon and will feel like
a long losing streak; (b) median premium is Rs 15 so spread is the worst of
any bucket and the cost table above understates it; (c) the collection is
ATM-anchored, so "far OTM" rows only exist for strikes the spot has recently
visited — an entry-selection bias that flatters exactly this bucket; (d) five
of the six quarters in the panel were rising markets. It is the only cell in
the panel worth a follow-up, with a proper spread model.

---

## 6. Data limits — what this panel cannot settle

- **Span.** 2025-01-01 → 2026-07-20; usable density only from 2025Q2.
  ~6 quarters, one broad regime (mildly rising equities, ex-2026Q1). Nothing
  here speaks to a crash, a sustained bear, or a vol-crush regime.
- **No bid/ask, anywhere.** Every cost number above is a modelled assumption
  layered on exact statutory charges. The single biggest unmeasured quantity.
- **Weeklies are effectively absent** (3,057 of 437,943 rows are non-monthly).
  The weekly-vs-monthly question is answered only via the DTE proxy.
- **ATM-anchored collection bias.** The tracker collects strikes near the
  money, so moneyness coverage is roughly uniform by construction (17-24% per
  bucket) but every contract in the panel was near ATM at *some* point. Deep
  ITM and far OTM rows are systematically "strikes the spot has visited".
- **Survivorship, quantified:** 82% of snapshots have a quote 3 sessions later
  (69-72% at DTE 3-7 where contracts expire out of the window; 83-94% at
  DTE ≥ 8). Rows *without* a fwd3 quote had a worse 1-day return
  (median −6.96% vs −4.41%) and were further from the money (|mny| 2.67% vs
  2.29%). So the surviving panel is **mildly optimistic** — the true carry is
  a little worse than the tables above, not better.
- **Known corrupt spot rows** (Fyers cross-symbol tick contamination, ~6.8k
  rows still in the table) leak into 30m spot; ATR outliers were filtered but
  the raw daily-return mean for 2026Q3 stocks is still visibly polluted
  (+21% mean daily). Section 2 ATR figures are cleaned; treat 2026Q3 stock
  cells as the least trustworthy.
- **Touch-order ambiguity.** With daily extremes we cannot tell which barrier
  was touched first in the 3.4% of windows where both were touched, nor model
  intraday stop slippage. A triple-barrier backtest needs 30m or finer bars
  for exit fidelity.
- **Positioning table is 6 months deep**, so §3 is a genuinely small-sample
  failure-to-confirm rather than a refutation.

---

## Bottom line for the wiring decision

1. **Slight-ITM DTE 8-22 is not the holdable contract.** Deep ITM (< −3% ITM,
   ~0.7 delta), DTE 8-22, monthly is — 3-session flat-spot carry −3.9% vs
   −9.6% (slight ITM) and −16.4% (ATM). If the lane goes long premium at a
   2-3 day horizon it should be buying delta, not optionality.
2. **A symmetric spot-ATR barrier is a ~2:1-against premium barrier.** −1 ATR
   costs ~55% of premium; +1 ATR earns ~29%. The target multiple must be
   materially larger than the stop multiple just to reach premium symmetry,
   and ~30% of trades hit neither barrier and pay the time-stop carry.
3. **Costs are tolerable at this horizon** (3-4% of the typical move at a
   1.6% round trip) — the one clean positive. Keep premium notional ≥ Rs 20k.
4. **The BANKNIFTY oi_build fwd3 edge did not reproduce** on the new panel.
   It should not be treated as a live conditioning input.
5. **The bar for the owner's spot-indicator construct is ~56-60% 3-day
   directional accuracy net of cost.** Our own prior work measured every
   trend/ADX construct as *negatively* predictive. On this evidence the honest
   prior is that the construct will not clear the bar, and part (C) should be
   run as a falsification test, not as a search for a positive configuration.
