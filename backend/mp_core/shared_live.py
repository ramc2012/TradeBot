"""Coalesce live analysis requests across API/strategy processes (15s window)."""
import asyncio
import json
import os
from uuid import uuid4
from time import monotonic

_RETRY_AT = 0.0


async def shared_live(symbol, build):
    global _RETRY_AT
    if not os.environ.get("SHARED_MP_REDIS_URL") or monotonic() < _RETRY_AT:
        return await build()
    from db.redis_client import get_redis
    from fastapi.encoders import jsonable_encoder
    client = await get_redis()
    key = f"mpof:live-v3:{symbol}"
    token = uuid4().hex
    leased = False
    try:
        for _ in range(100):
            raw = await client.get(key)
            if raw:
                return json.loads(raw)
            leased = bool(await client.set(key + ":lock", token, nx=True, ex=60))
            if leased:
                break
            await asyncio.sleep(0.1)
        if not leased:
            raise TimeoutError("Auction snapshot is being prepared; retry shortly")
    except TimeoutError:
        raise
    except Exception:
        _RETRY_AT = monotonic() + 30
        return await build()
    try:
        value = await build()
        await client.set(key, json.dumps(jsonable_encoder(value)), ex=15)
        return value
    finally:
        if leased:
            await client.eval("if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end", 1, key + ":lock", token)
