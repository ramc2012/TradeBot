"""Load the application's dependency-light shared cache without importing its API."""
import importlib.util
from pathlib import Path
import sys

_path = Path(__file__).resolve().parents[2] / "backend" / "mp_core" / "cache.py"
if not _path.exists():
    _path = Path("/app/mp_core/cache.py")
_name = "tradebot_shared_mp_cache"
if _name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_name, _path)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
cached_json = sys.modules[_name].cached_json
