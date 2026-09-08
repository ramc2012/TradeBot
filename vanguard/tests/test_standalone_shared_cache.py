"""Production uses filename entry points, not pytest's augmented import path."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('path,expression', [
    ('features/m5_timing.py', "assert ns['value_area_from_bars']([100.0],[101.0])"),
    ('ingest/futures_oi.py', "from model.shared_mp_cache import cached_json; assert cached_json('test-script', [1], lambda: {'ok':True})['ok']"),
    ('research/mp_auction.py', "import numpy as np; assert ns['Profile'](np.array([100.0]),np.array([101.0]),0.5).counts.sum()>0"),
])
def test_filename_entrypoint_resolves_cache_without_pythonpath(tmp_path,path,expression):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('SHARED_MP_REDIS_URL', None)
    code = f"import runpy; ns=runpy.run_path({str(ROOT/path)!r}); {expression}"
    subprocess.run([sys.executable,'-I','-c',code],cwd=tmp_path,env=env,check=True,capture_output=True,text=True)
