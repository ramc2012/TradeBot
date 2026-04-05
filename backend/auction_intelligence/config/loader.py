from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "defaults.json"


@lru_cache
def load_default_config() -> dict[str, Any]:
    return json.loads(_DEFAULT_CONFIG_PATH.read_text())


def clone_default_config() -> dict[str, Any]:
    return deepcopy(load_default_config())
