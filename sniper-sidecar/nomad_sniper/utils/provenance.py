"""Reproducibility: every saved artifact carries (config_hash, git_sha, created_at_ist).

Without this, you cannot tell which config produced which result. With it, you can.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any

from nomad_sniper.utils.timeutil import now_ist


def compute_config_hash(config: dict[str, Any]) -> str:
    """Stable hash of a config dict. Sorted keys, no whitespace."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def get_git_sha() -> str:
    """Current git SHA, or 'nogit' if not in a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0:
            sha = out.stdout.strip()
            # Mark dirty trees
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                sha += "-dirty"
            return sha
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "nogit"


def make_provenance(config: dict[str, Any]) -> dict[str, str]:
    """Standard provenance block to attach to every artifact."""
    return {
        "config_hash": compute_config_hash(config),
        "git_sha": get_git_sha(),
        "created_at_ist": now_ist().isoformat(),
    }


def write_artifact_metadata(path, config: dict[str, Any], extra: dict | None = None) -> None:
    """Write a `.meta.json` next to an artifact, capturing how it was produced."""
    from pathlib import Path

    path = Path(path)
    meta = make_provenance(config)
    if extra:
        meta.update(extra)
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, default=str))
