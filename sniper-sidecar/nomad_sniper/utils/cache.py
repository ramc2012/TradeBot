"""Build persistence — skip expensive rebuilds when nothing changed.

Each build step (grid features, grid labels) writes its output parquet plus a sidecar
``<output>.manifest.json`` recording a hash of everything that determines the output:
the input file fingerprints (path + size + mtime) and the relevant config knobs.

On re-run, if the output parquet exists and its manifest hash matches the current inputs,
the step is skipped and the cached parquet is reused. Pass ``--force`` to rebuild anyway.

This makes the 1-minute feature build (tens of minutes) a one-time cost.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def fingerprint_files(paths: list[Path]) -> list[dict]:
    """Stable fingerprint per file: relative name + size + mtime (ns). Sorted."""
    out = []
    for p in sorted(paths, key=lambda x: x.name):
        try:
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
        except FileNotFoundError:
            out.append({"name": p.name, "missing": True})
    return out


def build_hash(inputs: list[Path], config: dict[str, Any]) -> str:
    """Hash of input fingerprints + the config that affects the output."""
    payload = {
        "inputs": fingerprint_files(inputs),
        "config": _canonical(config),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def manifest_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".manifest.json")


def is_cached(output: Path, inputs: list[Path], config: dict[str, Any]) -> bool:
    """True iff `output` exists and its manifest matches the current inputs+config."""
    mp = manifest_path(output)
    if not output.exists() or not mp.exists():
        return False
    try:
        saved = json.loads(mp.read_text())
    except Exception:  # noqa: BLE001
        return False
    return saved.get("hash") == build_hash(inputs, config)


def write_manifest(output: Path, inputs: list[Path], config: dict[str, Any]) -> None:
    mp = manifest_path(output)
    mp.write_text(json.dumps({
        "hash": build_hash(inputs, config),
        "output": output.name,
        "n_inputs": len(inputs),
        "config": _canonical(config),
    }, indent=2, sort_keys=True))


def _canonical(config: dict[str, Any]) -> dict[str, Any]:
    """Round floats / stringify so the hash is stable across YAML re-reads."""
    out: dict[str, Any] = {}
    for k, v in sorted(config.items()):
        out[k] = round(v, 8) if isinstance(v, float) else v
    return out
