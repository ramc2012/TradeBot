# sniper-sidecar — mirror of the DEPLOYED /opt/sniper package

This directory is a faithful mirror of `/opt/sniper` on the prod EC2
(i-00a55c7e7633275d4, ap-south-1) as of 2026-06-10 — the code that actually runs the
sniper shadow lane (predict → score → dashboard → retrain) via the
`sniper-shadow.timer` / `sniper-retrain.timer` systemd units.

**This is NOT the same lineage as `sniper-phase0/`.** The research repo
(`sniper-phase0/src/nomad_sniper`) is the full Phase-0 feature-contract codebase;
this deployed package is a sidecar-specific fork (simplified feature families,
`ai_lane.py`, `sniper_scorer.py`, `sniper_retrain.py` exist only here). Until
2026-06-10 this package existed ONLY on the EC2 host — instance loss meant code loss.

## Deploying changes

The sidecar containers bind-mount `/opt/sniper` (`docker run --rm -v /opt/sniper:/sniper
sniper-shadow:latest ...`), so a deploy is just a file copy onto the host (base64 over
SSM) + `rm -rf /opt/sniper/nomad_sniper/**/__pycache__`. No image rebuild unless
dependencies change (then rebuild `sniper-shadow:latest` from `Dockerfile.sniper`,
which is `FROM tradebot-backend:latest`).

Keep this mirror in sync with any host-side edit, in both directions.

## Layout

- `nomad_sniper/` — the deployed package (features, models, integration: ai_lane /
  sidecar / scorer / retrain)
- `sniper_sidecar.py`, `sniper_scorer.py`, `sniper_retrain.py` — root-level entrypoints
  the systemd units invoke (WORKDIR `/sniper`)
- `Dockerfile.sniper` — also mirrored at `backend/deploy/Dockerfile.sniper`
