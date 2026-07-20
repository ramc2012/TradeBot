# (B) Relative strength vs NIFTY — does it pick the right instrument?

**Date:** 2026-07-21 · **Status:** research only. Nothing wired, no flags, no lane code touched.
**Harness:** `backend/directional_options/research/moves_rs/rs_*.py`
**Full numeric dump:** `backend/directional_options/research/moves_rs/rs_results.txt`
**Scope:** SPOT level only. Option translation is deliberately the next phase —
see "why spot is not the answer" below.

---

## Verdict, up front

**Relative strength versus NIFTY carries no usable directional information at
spot, on this sample. Zero of fifteen tests survive multiplicity — zero survive
even at raw 5% once the overlapping windows are de-overlapped.**

The primary formulation, 21-session relative return, has a cross-sectional IC of
**+0.0097 / +0.0105 / +0.0092** at 3 / 5 / 10 sessions. De-overlapped
t = **+0.62 / +0.52 / +0.31**. The best cell anywhere in the grid (beta-adjusted
alpha at 10 sessions, IC +0.038) reaches t = +1.11, p = 0.28, BH q = 0.86.

**But RS is *not* just beta** — that trap, the one worth checking, is not what
killed it. Cross-sectional corr(RS, beta) is **−0.013**; beta on its own has an
IC of ~0 (t ≤ +0.33); and stripping beta out *raises* the IC rather than
destroying it. RS is a genuinely beta-neutral construct here. It simply has
almost no signal in it.

**And the one thing RS did appear to do — select stocks that move more — turns
out to be trailing volatility wearing a costume.** |RS| → forward excursion
looks powerful (IC +0.11, t +12). Divide |RS| by trailing realised vol and the
IC collapses to **+0.008 (t +0.87)**. Plain trailing realised volatility is a
**3× stronger** mover-selector (IC +0.31, t +29) and needs no benchmark at all.

If the lane wants to select instruments that will *move*, the honest answer from
this pass is: **use realised volatility, not relative strength.**

---

## What was built

| File | Role |
|---|---|
| `rs_build_daily.py` | 30m tape → clean daily panel (`data_rs/rs_daily.parquet`) |
| `rs_features.py` | the four RS formulations + beta control + forward outcomes |
| `rs_test_causality.py` | prefix-invariance proof (+ a positive control) |
| `rs_analyse.py` | sections 0–8, writes `rs_results.txt` |

**Zero new PG load.** A second and third workflow were querying the database
during this pass, and PG has been OOM-killed twice in 24h by chunk-exclusion
failures. This study issues **no query at all**: it reuses the 30-minute
`underlying_spot_candles` CSVs already extracted by the `panel_2d3d` pass
(read-only), whose extraction bounded `time` directly with literal UTC
timestamps. Everything downstream is pandas on local parquet.

**File-ownership note.** A parallel workflow (Study A, monthly moves) is writing
into `moves_rs/` at the same time. Every file this study owns is prefixed `rs_`
and its artefacts live in `data_rs/`. No file of Study A's is read or written.

### Data hygiene (this tape is known to be dirty)

* Only the 13 canonical NSE 30m slots are kept (03:45–09:45 UTC = 09:15–15:15
  IST). The table also carries rows at every other half-hour for a handful of
  names; those are other-source / cross-symbol artefacts and are dropped.
* Session needs ≥ 10 of 13 bars; OHLC coherence asserted; 69,536 → 69,303 bars.
* Daily |close-to-close| > 25% dropped as a contamination candidate
  (Fyers cross-symbol tick contamination, 2026-07-20). **4 bars** removed.

### Coverage — and the honest limit it imposes

| | |
|---|---|
| names (stocks, indices excluded) | **209** |
| sessions | **312**, 2025-03-28 → 2026-07-15 |
| name-days | **65,076** |
| sessions per name | min 306, median 311 |
| median history per name | **≈ 14.8 months** |
| usable rows, primary feature @ fwd_5 | 59,642 over 286 sessions |
| first session with a valid 120-session beta | 2025-09-23 |

**≈15 months is one regime.** Every number below is a single-regime result and
must not be read as a multi-cycle one. Names with < 300 sessions were **excluded
outright**, not averaged over: 215 names cleared the bar, 209 survived the
index exclusion and the NIFTY-date alignment.

### Causality — proved, not asserted

`rs_test_causality.py` runs prefix-invariance: recompute the entire feature
block on rows `0..k` alone, assert the value at row `k` matches the full-series
value at `rtol = 1e-12`. **756 feature-value comparisons across 6 names × 14 cut
points × 9 features — all pass.** Two further guards:

* **Positive control.** The forward outcome columns *do* look ahead, so they
  *must* fail the same test. They do. This proves the test can detect lookahead
  at all, rather than passing vacuously.
* **Cross-sectional rank at date t is computed from date-t rows only** —
  asserted by recomputing on a truncated panel.

---

## 1. RS formulations — what was computed, what is primary

| name | definition |
|---|---|
| `rs_ret_21` | `log(P/N)_t − log(P/N)_{t−21}` — 21-session relative return. **PRIMARY** |
| `rs_ret_63` | same, 63 sessions |
| `rs_slope_21` | normalised OLS slope of `log(P/N)` over 21 sessions — trend *quality* of the ratio |
| `alpha_21` | 21-session return − `beta_120` × NIFTY 21-session return — beta-adjusted excess |
| `beta_120` | the control (120-session rolling OLS beta of daily returns) |

**Primary = `rs_ret_21`,** because the lane chooses *among instruments on a
single day*, so the natural object is a same-day cross-sectional comparison;
21 sessions matches the monthly frame the owner asked about; and it is the
plainest reading of "relative strength with NIFTY". Note `rs_ret_L` **is** the
price-ratio-and-its-trend formulation — it is exactly the log change in `P/N`.

**One methodological point stated plainly, because it is a common way to
double-count.** A per-date Spearman IC is rank-invariant, so `rs_ret_21` and its
cross-sectional percentile `rs_ret_21_rank` have **identical IC by
construction**. RS-rank is a distinct object only for decile and pooled work,
never for IC. It is therefore *not* reported as a separate formulation with a
separate IC.

Mean per-session cross-sectional Spearman between the forms:

| pair | ρ |
|---|---|
| `rs_ret_21` vs `rs_ret_63` | +0.553 |
| `rs_ret_21` vs `rs_slope_21` | +0.875 |
| `rs_ret_21` vs `alpha_21` | **+0.980** |
| `rs_ret_21` vs `beta_120` | **−0.013** |
| `alpha_21` vs `beta_120` | +0.010 |

---

## 2. Is RS just beta? — **No.** (and that is not what saves it)

This was the pre-registered trap: in a rising market, raw outperformance is
mostly high beta, and "RS" would then be a factor loading, not a selection
signal. Four independent tests, all pointing the same way:

**(a) Direct.** Cross-sectional corr(RS_21, beta_120) = **−0.013** (sd 0.221,
range −0.51…+0.54). Essentially orthogonal on average.

**(b) Market-conditional.** If RS were beta, its IC would flip with the market
and corr(IC, NIFTY forward return) would be strongly *positive*. Observed:

| h | IC \| NIFTY up | IC \| NIFTY down | corr(IC, NIFTY fwd) |
|---|---|---|---|
| 3 | −0.0379 (n=140) | +0.0547 (n=148) | **−0.368** |
| 5 | −0.0270 (n=139) | +0.0459 (n=147) | −0.345 |
| 10 | −0.0410 (n=139) | +0.0583 (n=142) | −0.278 |

The correlation is **negative**, the opposite of the beta signature. RS mildly
works when the market falls and mildly *anti*-works when it rises — which is a
defensive/low-beta tilt, not a beta tilt. Neither sub-sample is significant.

**(c) Strip beta out.** If RS were beta, removing beta would destroy it and
`beta_120` alone would carry the signal:

| feature | h=3 | h=5 | h=10 |
|---|---|---|---|
| `rs_ret_21` IC (t) | +0.0097 (+0.62) | +0.0105 (+0.52) | +0.0092 (+0.31) |
| `alpha_21` IC (t) | +0.0245 (+1.40) | +0.0299 (+1.32) | +0.0377 (+1.11) |
| `beta_120` IC (t) | +0.0015 (+0.06) | +0.0086 (+0.25) | +0.0164 (+0.33) |

Beta alone is worth nothing; beta-stripped RS is the *best* cell in the study.
**Verdict: RS is not beta in disguise.**

**(d) Fama-MacBeth**, `fwd ~ z(RS) + z(beta)` per session, de-overlapped t:

| h | RS coef | t | beta coef | t |
|---|---|---|---|---|
| 3 | +0.039%/sd | +0.67 | +0.036%/sd | +0.47 |
| 5 | +0.080%/sd | +0.82 | +0.067%/sd | +0.49 |
| 10 | +0.160%/sd | +0.81 | +0.158%/sd | +0.54 |

Neither loading is distinguishable from zero. The two do not compete because
neither is present.

**(e) The alpha caveat — do not get excited by `alpha_21`.** It requires 120
sessions of beta burn-in, so it is measured on a **later, shorter** sample
(189 dates vs 288). Re-running the primary on alpha's *exact* dates:

| h | `alpha_21` IC (t) | `rs_ret_21` on the SAME dates IC (t) |
|---|---|---|
| 3 | +0.0245 (+1.40) | +0.0161 (+0.83) |
| 5 | +0.0299 (+1.32) | +0.0167 (+0.67) |
| 10 | +0.0377 (+1.11) | +0.0158 (+0.44) |

About half of alpha's apparent advantage is the period, not the feature; the
remainder is not significant. Nothing here survives 15-test multiplicity.

---

## 3. Cross-sectional IC by horizon

Every IC is Spearman **within one session across names** (≥ 40 names required),
so a market-wide move cannot manufacture it. Overlapping forward windows inflate
raw t badly; the **de-overlapped** t (dates spaced h apart, averaged over all h
phase offsets) is the one quoted. Newey-West(h−1) shown for comparison.

| feature | h | dates | mean IC | raw t | **de-ov t** | worst phase | NW t | p | IC>0 |
|---|---|---|---|---|---|---|---|---|---|
| rs_ret_21 | 3 | 288 | +0.0097 | +1.08 | **+0.62** | +0.26 | +0.75 | 0.537 | 55.6% |
| rs_ret_21 | 5 | 286 | +0.0105 | +1.17 | **+0.52** | +0.43 | +0.66 | 0.606 | 54.2% |
| rs_ret_21 | 10 | 281 | +0.0092 | +1.01 | **+0.31** | −0.03 | +0.42 | 0.758 | 52.3% |
| rs_ret_63 | 3 | 246 | +0.0173 | +1.64 | +0.94 | +0.79 | +1.11 | 0.348 | 51.2% |
| rs_ret_63 | 5 | 244 | +0.0203 | +1.87 | +0.84 | +0.65 | +1.00 | 0.407 | 55.7% |
| rs_ret_63 | 10 | 239 | +0.0205 | +1.88 | +0.58 | +0.44 | +0.76 | 0.567 | 60.7% |
| rs_slope_21 | 3 | 289 | +0.0141 | +1.65 | +0.95 | +0.75 | +1.14 | 0.342 | 57.1% |
| rs_slope_21 | 5 | 287 | +0.0161 | +1.90 | +0.85 | +0.70 | +1.06 | 0.400 | 55.4% |
| rs_slope_21 | 10 | 282 | +0.0112 | +1.32 | +0.42 | −0.04 | +0.55 | 0.677 | 55.7% |
| alpha_21 | 3 | 189 | +0.0245 | +2.43 | +1.40 | +0.94 | +1.71 | 0.168 | 57.1% |
| alpha_21 | 5 | 187 | +0.0300 | +2.97 | +1.32 | +0.85 | +1.70 | 0.195 | 58.8% |
| alpha_21 | 10 | 182 | +0.0377 | +3.57 | +1.11 | +0.71 | +1.47 | 0.283 | 63.2% |
| beta_120 | 3 | 189 | +0.0015 | +0.10 | +0.06 | −0.13 | +0.07 | 0.950 | 49.2% |
| beta_120 | 5 | 187 | +0.0086 | +0.56 | +0.25 | +0.14 | +0.30 | 0.804 | 49.2% |
| beta_120 | 10 | 182 | +0.0165 | +1.06 | +0.33 | +0.11 | +0.41 | 0.742 | 49.5% |

**Look at what de-overlapping does.** `alpha_21` @ h=10 goes from raw t +3.57 —
which would read as a discovery — to +1.11. That gap *is* the overlap
artefact. Anyone reporting the raw t here would be reporting noise.

---

## 4. Decile monotonicity — not monotone

Mean cross-sectionally-demeaned forward return by RS decile (%):

| h | D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10 | ρ(decile) | D10−D1 (t) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3 | +0.07 | −0.02 | −0.02 | −0.03 | −0.06 | −0.04 | −0.03 | +0.00 | −0.00 | +0.14 | +0.224 | +0.077% (+0.45) |
| 5 | +0.09 | −0.03 | −0.01 | −0.09 | −0.10 | −0.05 | −0.07 | +0.02 | −0.02 | +0.24 | +0.164 | +0.149% (+0.50) |
| 10 | +0.11 | −0.04 | +0.02 | −0.12 | −0.15 | −0.05 | −0.07 | −0.07 | −0.09 | +0.46 | **−0.139** | +0.359% (+0.59) |

The shape is a shallow **U**, not a ramp: both extremes beat the middle. The
monotonicity ρ is +0.22 / +0.16 / **−0.14** — at the longest horizon the
ordering is mildly *inverted*. There is no decile ladder to stand on; whatever
is there lives entirely in D10, which is exactly the weak-evidence pattern the
brief warned about.

---

## 5. Era stability — sign-unstable

Four equal-length eras, primary feature:

| h | E1 (2025-03-28) | E2 (2025-07-23) | E3 (2025-11-18) | E4 (2026-03-11) |
|---|---|---|---|---|
| 3 | −0.008 (t −0.23) | +0.013 (+0.49) | +0.037 (+1.11) | −0.009 (−0.30) |
| 5 | −0.005 (−0.09) | +0.018 (+0.57) | +0.043 (+0.93) | −0.020 (−0.48) |
| 10 | −0.010 (−0.17) | +0.034 (+0.70) | +0.032 (+0.45) | −0.034 (−0.83) |

**The sign flips twice.** Negative in the first era, positive in the middle two,
negative again in the most recent — including the four months up to now. No era
is significant. This is what a zero-mean noise series looks like when you cut it
into quarters, and it is the opposite of the era-robustness that the PCR-OI spot
result showed (which was itself era-robust and *still* failed at option level).

---

## 6. Direction asymmetry — what little there is, is on the CE side

| h | leg | n | raw | XS-demeaned (t) | win | MFE | MAE | universe mean |
|---|---|---|---|---|---|---|---|---|
| 3 | CE / high-RS D10 | 6,037 | +0.31% | +0.144% (+1.27) | 52.7% | +2.99% | −2.54% | +0.16% |
| 3 | PE / low-RS D1 | 6,037 | −0.23% | −0.068% (−0.67) | 50.3% | +2.46% | −2.78% | +0.16% |
| 5 | CE / high-RS D10 | 5,995 | +0.52% | +0.243% (+1.30) | 53.3% | +3.97% | −3.30% | +0.28% |
| 5 | PE / low-RS D1 | 5,995 | −0.38% | −0.095% (−0.56) | 49.8% | +3.24% | −3.68% | +0.28% |
| 10 | CE / high-RS D10 | 5,890 | +0.96% | +0.461% (+1.30) | 54.0% | +5.84% | −4.65% | +0.50% |
| 10 | PE / low-RS D1 | 5,890 | −0.61% | −0.107% (−0.26) | 48.6% | +4.55% | −5.27% | +0.50% |

Pooling **would** have hidden this, so the split was worth doing:

* The **CE leg carries essentially all of it** — a consistent +0.14 / +0.24 /
  +0.46% of cross-sectional selection value, t ≈ +1.3 at every horizon. Stable
  in sign and scaling with horizon, but never significant.
* The **PE leg is worth ~nothing** — −0.07 / −0.10 / −0.11%, t between −0.26 and
  −0.67, and its win rate (50.3 / 49.8 / **48.6%**) is a coin flip *going the
  wrong way* at the longest horizon. Low-RS names do not fall.
* Roughly **half** of the CE leg's raw return is the universe's own drift
  (+0.16 / +0.28 / +0.50%), not selection.

This mirrors the known Indian single-stock asymmetry: the short leg does not
pay. Building the PE side of this lane on low RS has no support here.

---

## 7. Does RS select instruments that MOVE more? — it looks like it, and it doesn't

This is the question that matters most for a long-premium lane: the trade needs
a large excursion to pay for theta, so selecting for *movement* may beat
selecting for *direction*. Outcome measured as
`fwd_exc_h = max(|max-high move|, |min-low move|)` over t+1…t+h.

**The apparent finding.** Absolute RS is a strong mover-selector:

| h | IC(rs_ret_21 → excursion) | IC(\|RS\| → excursion) |
|---|---|---|
| 3 | +0.0295 (t +2.06, p 0.042) | **+0.1101 (t +12.21)** |
| 5 | +0.0308 (t +1.72, p 0.091) | **+0.1153 (t +9.80)** |
| 10 | +0.0418 (t +1.59, p 0.123) | **+0.1186 (t +7.32)** |

Mean forward 5-session excursion by signed-RS decile is a clean U:
D1 5.83%, D5 5.14%, D10 6.14% — the extremes of the RS distribution move ~20%
more than the middle. That is a real and large effect.

**The adversarial control that kills it as an *RS* finding.** A stock with a
large |relative move| is, mechanically, a volatile stock. So: does |RS| add
anything over plain trailing realised volatility (`rv_21`)?

| h | IC(rv_21 → excursion) | IC(\|RS\| / rv_21 → excursion) |
|---|---|---|
| 3 | **+0.3109 (t +28.71)** | +0.0077 (t +0.87, p 0.386) |
| 5 | **+0.3146 (t +22.65)** | +0.0113 (t +0.96, p 0.342) |
| 10 | **+0.3206 (t +16.73)** | +0.0115 (t +0.68, p 0.499) |

Divide the volatility out of |RS| and **the IC collapses from +0.11 to +0.01,
t from +12 to +0.9.** Trailing realised vol alone is roughly **3× stronger** and
requires no benchmark. Fama-MacBeth agrees: regressing forward excursion on both
ranks, `rv_21` loads at +0.66 to +1.15%/sd (t +11.5 to +21) while |RS| retains
only +0.09 to +0.16%/sd (t +2.2 to +3.9) — statistically alive but an order of
magnitude smaller, and a second-order refinement at best.

**So: "high RS names move more" is true and useless as an RS statement.** It is
"volatile names move more", which is the oldest fact in the book. If the lane
wants movers, the selector is realised volatility.

---

## 8. Multiplicity

Grid = 5 features × 3 horizons = **15 tests**; Bonferroni α = 0.0033.
Smallest de-overlapped p in the entire grid = **0.168**.

**Surviving BH-FDR 5%: 0 / 15. Surviving Bonferroni: 0 / 15.**

(The excursion tests of §7 are reported separately and are not part of this
grid; they are overwhelmingly significant, but as shown, as a volatility result
and not an RS one.)

---

## Why "it's only spot" makes this *worse*, not better

Two established facts bound how any of this could translate:

1. **Spot edges on stocks have already failed to survive the option layer once.**
   PCR-OI had a spot cross-sectional IC of −0.084/−0.093 at t ≈ −9 and
   era-robust — an order of magnitude stronger and vastly better-powered than
   anything measured here — and its CE-premium fwd5 cross-sectional IC came back
   **+0.0046 (t = 0.73), non-monotonic, wrong-signed.** A ~10% stock-option
   round-trip spread plus 15–42% five-day theta swamped a ~1% spot IC.
2. **Stock options bleed at every moneyness.** Measured 3-session flat-spot
   carry: stock deep-ITM −3.79%, stock slight-ITM −8.65%. A stock long-premium
   trade starts **4–9% behind** before direction matters; the honest floor for a
   barrier-managed 3-session trade is ≈ −5% to −6%.

The largest cross-sectional selection value RS produced anywhere is **+0.46%**
of spot, at 10 sessions, at t = +1.30. Against a −5% to −6% cost-and-carry
floor, that is not a small edge — it is a rounding error two orders of magnitude
short. **There is no version of the option translation that rescues this**, so
running it would be spending the effort to re-derive a foregone conclusion.

---

## What to do instead

1. **Do not use RS-vs-NIFTY as the lane's instrument-selection filter.** Not
   raw, not ranked, not beta-adjusted. It is not beta — it is just empty.
2. **If the lane needs movers, select on trailing realised volatility**, which
   is 3× stronger, needs no benchmark, and is trivially causal. That belongs in
   its own study (and needs the same option-level test before anyone believes
   it, for exactly the reasons above).
3. **Do not build the PE side on low RS.** The short leg is a coin flip going
   the wrong way; whatever direction signal exists sits entirely on the CE side
   and is not significant there either.
4. **The `alpha_21` cell (IC +0.038 @ h=10) is the only thing worth a revisit**,
   and only when there is ≥ 3 years of stock history — not because the evidence
   is suggestive today (t = +1.11, q = 0.86, and half the apparent advantage is
   the sample period) but because it is the one construct the data cannot yet
   properly test.

## Limits

* **≈15 months, one regime, 209 names.** Under-powered for a t ≈ 1 effect. This
  study cannot distinguish "no edge" from "small edge, not yet visible"; what it
  *can* say is that nothing here justifies wiring RS into a lane.
* Daily close-to-close only; no intraday entry modelling, no fills, no costs.
  Costs would only make every number worse.
* Survivorship: the universe is today's F&O list back-projected. That biases
  *toward* finding momentum/RS effects, and none were found — which strengthens
  the negative.
* NIFTY as the sole benchmark. A sector-relative RS was not tested and is the
  most plausible place a real effect could still be hiding.
