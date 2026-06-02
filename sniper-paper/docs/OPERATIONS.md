# Operations runbook

## Daily checklist

- [ ] 08:45 IST — Fyers session valid? Either TOTP auto-refresh worked, or paste new token + restart runner.
- [ ] 09:15 IST — Tick flow OK? `SELECT count(*) FROM paper_ticks WHERE ts > now() - interval '2 minutes';` returns > 0.
- [ ] 09:45 IST — First IB period complete; expect first detector candidates to start appearing in `paper_signals`.
- [ ] Throughout day — Dashboard shows live counter increasing.
- [ ] 15:30 IST — NSE/BSE close. NIFTY/SENSEX positions either closed or timed out.
- [ ] 23:30 IST — MCX close. CRUDE positions wound down.
- [ ] End of day — Daily report sanity: `sniper-paper status`.

## What to do when…

### …the runner stops emitting signals

1. Check Fyers WS connection:
   ```bash
   docker compose logs --tail 100 sniper-paper-runner | grep -i fyers
   ```
   Look for "WS open", "subscribing", or error messages.
2. Verify ticks landing:
   ```sql
   SELECT instrument, count(*), max(ts) FROM paper_ticks
   WHERE ts > now() - interval '5 minutes' GROUP BY instrument;
   ```
3. If all three instruments are quiet → Fyers session expired. Re-auth.
4. If only some are quiet → contract symbol may be stale (monthly rollover).

### …an OOD signal looks anomalous

Expected behavior. SENSEX and CRUDE are flagged `in_distribution=false`. The v0 model has not seen their distribution. Either:
- Disable trading on OOD instruments (`risk.allow_ood_paper_trades: false` in `paper.yaml`), or
- Wait for v0.1 which will train per-instrument models.

### …kill switch trips

Read `paper_daily_pnl` to confirm reason:
```sql
SELECT date, consec_losses, net_pnl, kill_switch_tripped FROM paper_daily_pnl
ORDER BY date DESC LIMIT 5;
```
If it's a real losing streak, leave it tripped — system did its job. If it's a bug (e.g. mis-calibrated stop), reset for next day and investigate.

### …the model needs retraining

See [DEPLOYMENT.md](DEPLOYMENT.md) "Updating the model". Don't promote unless walk-forward report shows skip_accuracy_by_ev ≥ 0.65 AND profit_factor_traded_set ≥ 1.5.

### …a contract symbol rotated to next month

Update `configs/paper.yaml`:
```yaml
instruments:
  - name: NIFTY
    near_month_symbol: "NSE:NIFTY25DECFUT"   # was 25NOV
```
Restart: `docker compose restart sniper-paper-runner`.

## Data retention

`paper_ticks` is the heaviest table. Add a TimescaleDB retention policy after the first month:

```sql
SELECT add_retention_policy('paper_ticks', INTERVAL '90 days');
```

90 days of ticks across 3 instruments at ~1 tick/second = ~700M rows. If disk grows too fast, reduce to 30 days.

## v0 → v0.1 path

After 60-90 days of paper trading:

1. Backfill OF features into historical training (now that ticks are captured).
2. Train **per-instrument** models — `nifty_v0.1`, `sensex_v0.1`, `crude_v0.1` — each with its own `metadata.feature_order`.
3. Update `model.active_model_pointer` to a directory containing all three boosters (loader logic needs a small upgrade for multi-model dispatch).
4. Compare 30 days of paper trading under v0.1 against v0 baseline before declaring victory.
