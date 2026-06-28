#!/usr/bin/env bash
# Import broker credentials from the cloud (prod) deploy into the local Docker stack.
#
# WHY this works: creds live in Postgres table app_runtime_state (key
# 'broker_credentials') as a JSONB payload of Fernet-encrypted values. The Fernet
# key is base64(sha256(SECRET_KEY)) — deterministic from SECRET_KEY. So we copy the
# encrypted blob verbatim AND set the local SECRET_KEY to match the cloud's, after
# which the local backend can decrypt the imported values.
#
# REQUIRES (provide via env):
#   REMOTE_SECRET_KEY   the cloud box's SECRET_KEY (from its env/.env)   [required]
#   REMOTE_DB           full conn string to cloud DB                      [optional]
#                       default: postgresql://nomadcurie:nomadcurie@15.206.56.206:5433/nomadcurie
#                       (needs SG inbound 5433 open to this machine's IP)
#   PAYLOAD_FILE        path to a file containing the raw JSONB payload,  [optional]
#                       used INSTEAD of REMOTE_DB when the port can't be opened.
#
# Usage A (DB-to-DB, port opened):
#   REMOTE_SECRET_KEY=xxxx ./tools/import_cloud_creds.sh
# Usage B (pasted payload, no open port):
#   REMOTE_SECRET_KEY=xxxx PAYLOAD_FILE=/tmp/payload.json ./tools/import_cloud_creds.sh
set -euo pipefail

DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
REMOTE_DB="${REMOTE_DB:-postgresql://nomadcurie:nomadcurie@15.206.56.206:5433/nomadcurie}"

if [ -z "${REMOTE_SECRET_KEY:-}" ]; then
  echo "ERROR: set REMOTE_SECRET_KEY (the cloud box's SECRET_KEY)." >&2; exit 1
fi

# 1. Obtain the encrypted payload (either by pulling from the remote DB, or from a file)
PAYLOAD=""
if [ -n "${PAYLOAD_FILE:-}" ]; then
  echo "→ Reading payload from $PAYLOAD_FILE"
  PAYLOAD="$(cat "$PAYLOAD_FILE")"
else
  echo "→ Pulling payload from remote DB ($REMOTE_DB) via local psql container..."
  PAYLOAD="$("$DOCKER" exec nomadcurie_db psql "$REMOTE_DB" -tAc \
    "SELECT payload FROM app_runtime_state WHERE state_key='broker_credentials'")"
fi
if [ -z "${PAYLOAD// /}" ]; then
  echo "ERROR: empty payload — nothing to import." >&2; exit 1
fi
echo "✓ Got payload (${#PAYLOAD} bytes)"

# 2. Match local SECRET_KEY to the cloud's so the Fernet ciphertext decrypts.
echo "→ Setting local SECRET_KEY to match cloud..."
/usr/bin/python3 - "$ENV_FILE" "$REMOTE_SECRET_KEY" <<'PY'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t = re.sub(r'^SECRET_KEY=.*$', f'SECRET_KEY={sys.argv[2]}', t, flags=re.M)
p.write_text(t); print("✓ .env SECRET_KEY updated")
PY

# 3. Upsert the encrypted payload into the LOCAL DB.
echo "→ Writing payload into local app_runtime_state..."
printf '%s' "$PAYLOAD" | "$DOCKER" exec -i nomadcurie_db psql -U nomadcurie -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE IF NOT EXISTS app_runtime_state (
  state_key TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
\set payload `cat`
INSERT INTO app_runtime_state (state_key, payload, updated_at)
VALUES ('broker_credentials', :'payload'::jsonb, NOW())
ON CONFLICT (state_key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW();
SQL
echo "✓ Payload written to local DB"

# 4. Recreate the backend so it boots with the matching SECRET_KEY and reloads creds.
echo "→ Recreating backend container..."
( cd "$(dirname "$ENV_FILE")" && "$DOCKER" compose up -d --force-recreate backend )

echo ""
echo "Done. Verify in ~20s with:"
echo "  curl -s http://localhost:8000/api/auth/all-credentials-status | python3 -m json.tool"
