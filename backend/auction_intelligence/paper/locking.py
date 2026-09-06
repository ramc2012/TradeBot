"""Cross-process book lock on the shared durable runtime volume."""
import asyncio
import fcntl
from contextlib import asynccontextmanager
from time import monotonic


@asynccontextmanager
async def book_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        deadline = monotonic() + 30
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() > deadline:
                    raise TimeoutError("Paper book busy; retry next cycle")
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
