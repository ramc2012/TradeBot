"""Size cap for third-party SDK log files the app does not control.

WHY (2026-07-27).  ``fyers_apiv3``'s ``FyersDataSocket`` installs its OWN
``logging`` FileHandler and writes to ``<log_path>/fyersDataSocket.log``.  The
app passes ``log_path=""``, so it landed in the process CWD — ``/app``, which
is the ``./backend`` BIND MOUNT, i.e. straight onto the host's data volume.
By 2026-07-27 that file was 1,325,984,093 bytes (1.33 GB) on a volume with
11 GiB free (98% full).

Two properties of that writer make this the right shape of fix:

* the SDK NEVER rotates it and offers no size option — the only lever we have
  is truncation from outside;
* every open fd on it carries ``O_APPEND`` (verified on the live container:
  pid 9, fds 48/72/74, flags ``02402001`` = ``O_WRONLY|O_APPEND|O_LARGEFILE|
  O_CLOEXEC``).  With ``O_APPEND`` each write seeks to EOF first, so after a
  truncate the writer resumes at offset 0 and the space is reclaimed
  immediately.  WITHOUT ``O_APPEND`` a truncate would leave the writer's
  offset at 1.33 GB and re-extend the file with a 1.33 GB hole of NUL bytes —
  which is precisely the corruption that broke ``docker logs`` on 2026-07-24.
  ``_is_append_only`` re-checks this at runtime and REFUSES to truncate a file
  whose writer is not append-only, rather than trusting today's observation
  forever.

Never ``rm``: the inode stays alive behind the open fds, no space comes back
until the process exits, and the SDK keeps writing into a deleted inode.

That the file is an error firehose (32,728 × ``list index out of range`` in
the last 20 MB, plus ``TypeError: 'NoneType' object is not subscriptable`` in
``data_ws.py __symbol_conversion``) is a SEPARATE, unfixed SDK defect — this
module only stops it from filling the disk, and deliberately preserves the
newest ``tail_bytes`` so the evidence is not destroyed wholesale.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from loguru import logger


# Where the Fyers SDK's own file logger writes. Kept as a list because the
# SDK's log_path has changed across versions and the file may exist in the
# legacy (bind-mounted) location as well as the configured one.
DEFAULT_SDK_LOG_PATHS: tuple[str, ...] = (
    "/app/fyersDataSocket.log",
    "/var/log/tradebot-sdk/fyersDataSocket.log",
    "/app/fyersApi.log",
    "/var/log/tradebot-sdk/fyersApi.log",
)

# Cap and retained tail. 64 MB is ~2 days of the observed firehose rate and
# small enough to be greppable; the newest 8 MB survives each trim so a
# just-happened error is never lost to the guard itself.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_TAIL_BYTES = 8 * 1024 * 1024

# One sweep every 15 minutes. The firehose peaked around 5 KB/s, so the file
# cannot outrun this by more than a few MB between passes.
DEFAULT_INTERVAL_SECONDS = 900.0


def _is_append_only(path: Path) -> bool | None:
    """Is EVERY open write fd on ``path`` O_APPEND?

    Returns None when the answer cannot be established (no ``/proc``, e.g. on
    macOS), in which case the caller decides.  Truncating a file whose writer
    is NOT append-only re-extends it with a NUL hole, so this must fail
    CLOSED wherever it can be checked.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    try:
        target = os.path.realpath(path)
    except OSError:
        return None
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(fd) != target:
                    continue
                flags_line = next(
                    line
                    for line in (pid_dir / "fdinfo" / fd.name).read_text().splitlines()
                    if line.startswith("flags:")
                )
            except (OSError, StopIteration):
                continue
            flags = int(flags_line.split()[1], 8)
            if not flags & (os.O_WRONLY | os.O_RDWR):
                continue  # a reader cannot be corrupted by a truncate
            if not flags & os.O_APPEND:
                return False
    # Either no writer is open (nothing can re-extend the file) or every
    # writer is append-only. Both are safe to truncate.
    return True


def trim_oversized_log(
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> dict[str, Any] | None:
    """Trim one log file back to its newest ``tail_bytes`` if it exceeds the cap.

    Returns a report dict when it acted, None when the file was absent or
    under the cap.  Never raises — a log-hygiene sweep must not be able to
    take the app down.
    """
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if size <= int(max_bytes):
        return None

    append_only = _is_append_only(target)
    if append_only is False:
        logger.error(
            "[SdkLogGuard] {p} is {mb:.0f} MB but its writer is NOT O_APPEND — "
            "refusing to truncate (that would re-extend the file with a NUL hole). "
            "Rotate it on the next restart instead.",
            p=str(target), mb=size / 1e6,
        )
        return {"path": str(target), "size_before": size, "action": "refused_not_append_only"}

    keep = max(0, int(tail_bytes))
    tail = b""
    try:
        if keep:
            with target.open("rb") as fh:
                fh.seek(-keep, os.SEEK_END)
                tail = fh.read()
        # Truncate IN PLACE (never unlink) so the SDK's open fds keep writing
        # into the same inode and the space is reclaimed at once.
        with target.open("r+b") as fh:
            fh.truncate(0)
            if tail:
                fh.write(tail)
                fh.flush()
    except OSError as exc:
        logger.warning("[SdkLogGuard] trim of {p} failed: {err}", p=str(target), err=str(exc)[:160])
        return {"path": str(target), "size_before": size, "action": "error", "error": str(exc)[:160]}

    logger.warning(
        "[SdkLogGuard] trimmed {p}: {before:.0f} MB → {after:.0f} MB "
        "(cap {cap:.0f} MB, kept newest {keep:.0f} MB)",
        p=str(target), before=size / 1e6, after=len(tail) / 1e6,
        cap=max_bytes / 1e6, keep=keep / 1e6,
    )
    return {
        "path": str(target),
        "size_before": size,
        "size_after": len(tail),
        "action": "trimmed",
    }


def sweep_sdk_logs(
    paths: Iterable[str | Path] | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> list[dict[str, Any]]:
    """Trim every oversized known SDK log. Returns the reports for the ones acted on."""
    reports: list[dict[str, Any]] = []
    for path in paths if paths is not None else DEFAULT_SDK_LOG_PATHS:
        try:
            report = trim_oversized_log(path, max_bytes=max_bytes, tail_bytes=tail_bytes)
        except Exception as exc:  # noqa: BLE001 - hygiene must never break the app
            logger.warning("[SdkLogGuard] sweep error on {p}: {err}", p=str(path), err=str(exc)[:160])
            continue
        if report:
            reports.append(report)
    return reports


async def run_sdk_log_guard(
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    paths: Iterable[str | Path] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> None:
    """Background loop: sweep at startup, then every ``interval_seconds``.

    Runs off the event loop in a worker thread — a multi-hundred-MB read of
    the tail must never stall the tick path (the 2026-07-13 no-trade day was
    caused by exactly that class of mistake).
    """
    import asyncio

    while True:
        try:
            await asyncio.to_thread(
                sweep_sdk_logs, paths, max_bytes=max_bytes, tail_bytes=tail_bytes
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SdkLogGuard] sweep pass failed: {err}", err=str(exc)[:160])
        await asyncio.sleep(max(60.0, float(interval_seconds)))
