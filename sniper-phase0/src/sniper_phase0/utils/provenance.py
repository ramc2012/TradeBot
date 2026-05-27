from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def file_hash(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def config_hash(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def make_provenance(
    config: dict,
    input_paths: list[str | Path] | None = None,
    extra: dict | None = None,
) -> dict:
    return {
        "git_sha": git_sha(),
        "config_hash": config_hash(config),
        "input_hashes": {
            str(p): file_hash(p) for p in (input_paths or []) if Path(p).exists()
        },
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "extra": extra or {},
    }


def write_with_provenance(df, path: str | Path, provenance: dict) -> None:
    """Write a parquet with provenance attached as pandas metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.attrs["provenance"] = provenance
    df.to_parquet(path, index=False)
    sidecar = path.with_suffix(path.suffix + ".provenance.json")
    sidecar.write_text(json.dumps(provenance, indent=2, default=str))
