"""Log-hygiene regressions (2026-07-27).

TWO defects, one incident.

A) ``docker logs nomadcurie_backend`` produced nothing after 2026-07-24
   04:36:45Z — not over the weekend, not after a container restart.  The app
   was logging the whole time: two ungraceful host reboots left NUL runs
   inside the container's json-file log (3887 bytes at offset 115,945,681 and
   247 at 127,123,209), and Docker's SEQUENTIAL log reader aborts at the
   first NUL.  ~243k lines / 45 MB were unreachable.  Every service ran with
   ``json-file`` and NO options, so the file is unbounded and appended to
   across stop/start — the poison is permanent until the container is
   RECREATED.  Rotation makes a corrupt segment age out instead.

B) ``fyersDataSocket.log`` — the Fyers SDK's own, never-rotated FileHandler —
   had reached 1,325,984,093 bytes (1.33 GB) inside the ``./backend`` bind
   mount, on a host data volume at 98% (11 GiB free).  ``log_path=""`` meant
   the SDK wrote into the process CWD.

The dangerous part of fixing (B) is that truncating a file whose writer is
NOT ``O_APPEND`` leaves the writer's offset where it was and re-extends the
file with a NUL hole — i.e. it would MANUFACTURE defect (A).  These tests pin
both the in-place truncate and the refusal.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.sdk_log_guard import (
    DEFAULT_SDK_LOG_PATHS,
    sweep_sdk_logs,
    trim_oversized_log,
)


# ── B: the SDK log cap ───────────────────────────────────────────────────────


def test_oversized_log_is_trimmed_in_place_keeping_the_newest_tail(tmp_path: Path) -> None:
    path = tmp_path / "fyersDataSocket.log"
    path.write_bytes(b"old" * 400 + b"NEWEST-TAIL")
    inode_before = path.stat().st_ino

    report = trim_oversized_log(path, max_bytes=100, tail_bytes=11)

    assert report is not None and report["action"] == "trimmed"
    assert path.read_bytes() == b"NEWEST-TAIL"
    # In place: the inode must survive, or the SDK's already-open fds would
    # keep writing into a deleted inode and no space would be reclaimed.
    assert path.stat().st_ino == inode_before


def test_a_file_under_the_cap_is_left_completely_alone(tmp_path: Path) -> None:
    path = tmp_path / "fyersDataSocket.log"
    path.write_bytes(b"x" * 50)
    assert trim_oversized_log(path, max_bytes=100, tail_bytes=10) is None
    assert path.read_bytes() == b"x" * 50


def test_a_missing_file_is_a_silent_no_op(tmp_path: Path) -> None:
    assert trim_oversized_log(tmp_path / "absent.log", max_bytes=1) is None


def test_an_append_only_writer_resumes_without_leaving_a_nul_hole(tmp_path: Path) -> None:
    """THE safety property. A live O_APPEND writer keeps its fd open across
    the trim; its next write must land at the end of the retained tail, not
    at the old 1.33 GB offset (which would re-extend the file with NULs — the
    exact corruption that killed `docker logs`)."""
    path = tmp_path / "fyersDataSocket.log"
    path.write_bytes(b"")
    with path.open("ab") as writer:            # O_APPEND, like the SDK's handler
        writer.write(b"junk" * 500)
        writer.flush()

        trim_oversized_log(path, max_bytes=100, tail_bytes=8)

        writer.write(b"AFTER")
        writer.flush()

    data = path.read_bytes()
    assert b"\x00" not in data
    assert data.endswith(b"AFTER")
    assert len(data) == 8 + len(b"AFTER")


def test_truncation_is_refused_when_the_writer_is_not_append_only(
    tmp_path: Path, monkeypatch
) -> None:
    """Fail CLOSED. Without O_APPEND a truncate creates the NUL hole rather
    than reclaiming space, so the guard must decline and say so."""
    import core.sdk_log_guard as guard

    path = tmp_path / "fyersDataSocket.log"
    payload = b"y" * 500
    path.write_bytes(payload)
    monkeypatch.setattr(guard, "_is_append_only", lambda _p: False)

    report = guard.trim_oversized_log(path, max_bytes=100, tail_bytes=10)

    assert report["action"] == "refused_not_append_only"
    assert path.read_bytes() == payload  # untouched


def test_sweep_covers_every_known_sdk_log_path_and_survives_bad_ones(tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    small = tmp_path / "small.log"
    big.write_bytes(b"z" * 500)
    small.write_bytes(b"z" * 10)

    reports = sweep_sdk_logs(
        [big, small, tmp_path / "nope.log"], max_bytes=100, tail_bytes=10
    )

    assert [Path(r["path"]).name for r in reports] == ["big.log"]
    assert big.stat().st_size == 10
    assert small.stat().st_size == 10


def test_the_bind_mounted_legacy_location_is_still_swept() -> None:
    """The 1.33 GB file lives at /app/fyersDataSocket.log — inside the
    ./backend bind mount. Moving new writes elsewhere must not orphan it."""
    assert "/app/fyersDataSocket.log" in DEFAULT_SDK_LOG_PATHS


# ── B: the SDK no longer writes into the bind mount ──────────────────────────


def test_fyers_sdk_log_path_is_outside_the_bind_mount(monkeypatch, tmp_path: Path) -> None:
    """``log_path=""`` put the SDK's log in CWD == /app == ./backend on the
    host. It must now resolve to a directory that is not the bind mount."""
    from brokers.fyers import _sdk_log_path

    monkeypatch.setenv("FYERS_SDK_LOG_DIR", str(tmp_path / "sdk"))
    resolved = _sdk_log_path()
    assert resolved == str(tmp_path / "sdk")
    assert Path(resolved).is_dir()
    assert not resolved.startswith("/app")


def test_fyers_sdk_log_path_falls_back_rather_than_breaking_the_socket(monkeypatch) -> None:
    """A read-only or otherwise unusable directory must never stop the WS
    from opening — it degrades to the old CWD behaviour."""
    from brokers.fyers import _sdk_log_path

    monkeypatch.setenv("FYERS_SDK_LOG_DIR", "/proc/definitely/not/creatable")
    assert _sdk_log_path() == ""


# ── A: docker log rotation ───────────────────────────────────────────────────


def test_every_compose_service_caps_its_json_file_log() -> None:
    """Unbounded json-file logs are what made the 2026-07-24 corruption
    PERMANENT: one file per container, appended across restarts, so the NUL
    run poisoned every sequential read forever. With rotation it ages out."""
    yaml = pytest.importorskip("yaml")

    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    assert compose_path.exists(), compose_path
    config = yaml.safe_load(compose_path.read_text())

    services = config.get("services") or {}
    assert services, "no services parsed from docker-compose.yml"
    for name, service in services.items():
        logging_cfg = service.get("logging")
        assert logging_cfg, f"service {name} has no logging config (unbounded json-file)"
        assert logging_cfg.get("driver") == "json-file", name
        options = logging_cfg.get("options") or {}
        assert options.get("max-size"), f"service {name} has no max-size"
        assert int(str(options.get("max-file"))) >= 2, f"service {name} has no rotation depth"
