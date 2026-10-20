/**
 * The trade book must prefer an explicit net P&L when a lane keeps the
 * screen figure on realized_pnl (MACD Refined). A lane that already stores
 * net on realized_pnl, with no net alias, is unchanged.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { deriveTradeStats, pnlOf } from "../src/lib/strategy-stats.ts";

test("pnlOf prefers realized_pnl_net over a gross realized_pnl", () => {
  assert.equal(
    pnlOf({ realized_pnl: -139500, realized_pnl_net: -160000 }),
    -160000,
  );
});

test("pnlOf uses realized_pnl when the lane has no net alias", () => {
  assert.equal(pnlOf({ realized_pnl: 688.4, realized_pnl_gross: 750 }), 688.4);
});

test("a measured zero net is kept", () => {
  assert.equal(pnlOf({ realized_pnl: 40, realized_pnl_net: 0 }), 0);
});

test("an empty closed book does not invent a win rate or a profit factor", () => {
  const stats = deriveTradeStats([]);
  assert.equal(stats.count, 0);
  assert.equal(stats.winRate, null);
  assert.equal(stats.profitFactor, null);
  assert.equal(stats.expectancy, null);
  assert.equal(stats.net, null);
});

test("a scratch sample is a measured zero, not a missing sample", () => {
  const stats = deriveTradeStats([{ realized_pnl: 0 }, { realized_pnl: 10 }]);
  assert.equal(stats.count, 2);
  assert.equal(stats.winRate, 0.5);
  assert.equal(stats.net, 10);
  assert.equal(stats.profitFactor, Infinity);
});
