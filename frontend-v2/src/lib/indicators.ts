/**
 * Client-side technical indicators for chart overlays.
 *
 * KAMA (Kaufman Adaptive Moving Average) — an ER-weighted MA that speeds up in
 * trends and flattens in chop. Computed here so any spot CandleChart can overlay
 * it without a backend round-trip (the option-premium study already receives
 * KAMA from the API). Defaults KAMA(10, 2, 30) match the backend's label.
 */

export function computeKama(
  closes: (number | null | undefined)[],
  erPeriod = 10,
  fast = 2,
  slow = 30,
): (number | null)[] {
  const n = closes.length;
  const out: (number | null)[] = new Array(n).fill(null);
  if (n === 0) return out;

  const fastSC = 2 / (fast + 1);
  const slowSC = 2 / (slow + 1);

  // Work over the finite closes; indices with null close carry the prior KAMA.
  let prevKama: number | null = null;
  for (let i = 0; i < n; i++) {
    const c = closes[i];
    if (c == null || !Number.isFinite(c)) {
      out[i] = prevKama; // hold last value across a gap
      continue;
    }
    if (i < erPeriod || prevKama == null) {
      // Warm-up: seed KAMA with the close once we have one, but don't emit a
      // line until we have a full ER window (keeps the overlay honest).
      prevKama = prevKama == null ? c : prevKama;
      out[i] = i < erPeriod ? null : prevKama;
      continue;
    }
    const past = closes[i - erPeriod];
    if (past == null || !Number.isFinite(past)) {
      out[i] = prevKama;
      continue;
    }
    const change = Math.abs(c - past);
    let volatility = 0;
    for (let j = i - erPeriod + 1; j <= i; j++) {
      const a = closes[j];
      const b = closes[j - 1];
      if (a != null && b != null && Number.isFinite(a) && Number.isFinite(b)) {
        volatility += Math.abs(a - b);
      }
    }
    const er = volatility === 0 ? 0 : change / volatility;
    const sc = Math.pow(er * (fastSC - slowSC) + slowSC, 2);
    prevKama = prevKama + sc * (c - prevKama);
    out[i] = prevKama;
  }
  return out;
}
