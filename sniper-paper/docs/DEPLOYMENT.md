# Deployment runbook — sniper-paper on EC2 15.206.56.206

This system runs **alongside** the existing nomad-curie auction-intelligence platform on the same EC2 box. It reuses the existing TimescaleDB and Redis services from `nomad-curie/docker-compose.yml`.

## Prerequisites (one-time)

1. **nomad-curie stack already running** on the host (`docker compose ps` shows `timescaledb`, `redis`, backend, frontend healthy).
2. **Fyers API access** — static IP `15.206.56.206` registered with Fyers in the developer portal. SEBI April 2026 framework: API access without a registered static IP is blocked.
3. **Fyers credentials** ready: `app_id`, `secret_id`, daily access token mechanism (manual paste OR TOTP secret).
4. **Disk**: ~2 GB free for image + 5 GB free for tick data accumulation (capped at retention policy in schema).
5. **Outbound** to `api.fyers.in` and `wss://api-t1.fyers.in` allowed (already true for nomad-curie).

## First-time deploy

```bash
# 1. Get the code on the host.
cd /home/ubuntu/nomad-curie
git pull   # if pulling from a remote, or scp the sniper-paper directory in
cd sniper-paper

# 2. Configure secrets.
cp configs/secrets.yaml.example configs/secrets.yaml
vim configs/secrets.yaml   # fill in Fyers creds + (if used) TOTP secret

# 3. Verify the nomad-curie network name.
docker network ls | grep nomad-curie
# Expect: nomad-curie_default
# If different, update networks.default.name in docker-compose.yml.

# 4. Apply the schema to the shared TimescaleDB.
docker compose run --rm sniper-paper-runner sniper-paper init-db

# 5. Verify the DB has NIFTY candles.
docker compose run --rm sniper-paper-runner sniper-paper introspect-db
# Look for "underlying_spot_candles" in tables and a NIFTY-ish symbol in top_symbols.

# 6. Pull candles and train.
docker compose run --rm sniper-paper-runner sniper-paper extract-nifty \
    --start 2023-01-01 --end 2026-05-01 --timeframe 30m --out data/nifty_candles.parquet

docker compose run --rm sniper-paper-runner sniper-paper train \
    --candles data/nifty_candles.parquet --notes "v0 candle-only model"

# 7. List + promote the new artifact.
docker compose run --rm sniper-paper-runner sniper-paper list-models
docker compose run --rm sniper-paper-runner sniper-paper promote nifty_candle_v0_YYYYMMDD_HHMMSS

# 8. Start the runner + API.
docker compose up -d
docker compose logs -f sniper-paper-runner
```

The dashboard is at `http://15.206.56.206:8001/`. The runner waits in the background and writes signals to `paper_signals` every 30 seconds during trading hours.

## Daily operations

- **08:50 IST** — Fyers re-auth. If `totp_secret` is set in `secrets.yaml`, the runner handles this automatically. Otherwise:
  - Manually generate the access token via Fyers web auth.
  - `vim configs/secrets.yaml` → paste new token.
  - `docker compose restart sniper-paper-runner`.
- **15:30 IST** — NSE/BSE close. NIFTY/SENSEX positions auto-timeout. CRUDE continues until 23:30.
- **23:30 IST** — MCX close. CRUDE positions wind down.
- **Daily** — `sniper-paper status` from inside the container or via the dashboard.

## Logs

```bash
docker compose logs -f sniper-paper-runner       # runner
docker compose logs -f sniper-paper-api          # API
docker compose exec sniper-paper-runner sniper-paper status
```

## Kill switch

The risk governor trips automatically on:
- 3 consecutive losses
- Daily net loss > ₹5,000 (configurable in `paper.yaml`)
- > 30 signals taken in a day

Manual kill:

```bash
# In a psql shell on the host:
docker compose exec timescaledb psql -U nomadcurie -d nomadcurie \
    -c "UPDATE paper_daily_pnl SET kill_switch_tripped = true WHERE date = CURRENT_DATE;"
# All new signals will be rejected with reason=kill_switch_tripped.

# To reset (next day):
docker compose exec timescaledb psql -U nomadcurie -d nomadcurie \
    -c "UPDATE paper_daily_pnl SET kill_switch_tripped = false WHERE date = CURRENT_DATE;"
```

## Updating the model

```bash
# Retrain with new data
docker compose run --rm sniper-paper-runner sniper-paper extract-nifty \
    --start 2024-01-01 --end 2026-MM-01 --out data/nifty_candles.parquet
docker compose run --rm sniper-paper-runner sniper-paper train \
    --candles data/nifty_candles.parquet --notes "retrain with N more months"

# Inspect walk-forward report before promoting
docker compose run --rm sniper-paper-runner cat artifacts/<new_artifact_id>/walk_forward_report.json

# If skip_accuracy_by_ev >= 0.65 and profit_factor_traded_set >= 1.5, promote:
docker compose run --rm sniper-paper-runner sniper-paper promote <new_artifact_id>
docker compose restart sniper-paper-runner
```

## Updating contract symbols (monthly)

NIFTY/SENSEX/CRUDE near-month contracts rotate. Update `near_month_symbol` in `configs/paper.yaml` and `docker compose restart sniper-paper-runner`. A future improvement: auto-rotation from Fyers symbol master.

## Health checks

```bash
curl http://15.206.56.206:8001/healthz                          # API alive
curl http://15.206.56.206:8001/api/status | jq                  # current paper state
docker compose exec timescaledb psql -U nomadcurie -d nomadcurie \
    -c "SELECT count(*) FROM paper_ticks WHERE ts > now() - interval '5 minutes';"
# Expect a non-zero count during trading hours.
```

## Rollback

```bash
docker compose down
# Promote the previous artifact:
docker compose run --rm sniper-paper-runner sniper-paper list-models
docker compose run --rm sniper-paper-runner sniper-paper promote <old_artifact_id>
docker compose up -d
```
