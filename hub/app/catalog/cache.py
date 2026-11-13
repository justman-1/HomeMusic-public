"""TTL cache for catalog/search responses in the `catalog_cache` table."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from app.db import session_scope, task_holds_write
from app.models import CatalogCache, utcnow


async def cache_get(key: str) -> Any | None:
    async with session_scope() as s:
        row = await s.get(CatalogCache, key)
        if row is None:
            return None
        if row.fetched_at + timedelta(seconds=row.ttl_s) < utcnow():
            return None
        return row.payload_json.get("v")


async def cache_set(key: str, value: Any, ttl_s: int) -> None:
    if task_holds_write():
        return  # the caller's own write transaction holds the lock: caching now would wait for itself (see db.py)
    # upsert: concurrent producers of the same key (e.g. gathered page fetches) must not collide
    row = {"key": key, "payload_json": {"v": value}, "ttl_s": ttl_s, "fetched_at": utcnow()}
    stmt = insert(CatalogCache).values(**row)
    stmt = stmt.on_conflict_do_update(index_elements=[CatalogCache.key], set_={k: stmt.excluded[k] for k in row if k != "key"})
    async with session_scope() as s:
        await s.execute(stmt)


async def cache_set_many(values: dict[str, Any], ttl_s: int) -> None:
    """Several keys in one transaction (`cache_set` each)."""
    if not values or task_holds_write():
        return
    now = utcnow()
    rows = [{"key": k, "payload_json": {"v": v}, "ttl_s": ttl_s, "fetched_at": now} for k, v in values.items()]
    stmt = insert(CatalogCache)
    stmt = stmt.on_conflict_do_update(index_elements=[CatalogCache.key],
                                      set_={k: stmt.excluded[k] for k in ("payload_json", "ttl_s", "fetched_at")})
    async with session_scope() as s:
        await s.execute(stmt, rows)


async def cache_delete(key: str) -> None:
    async with session_scope() as s:
        await s.execute(delete(CatalogCache).where(CatalogCache.key == key))


async def cached(key: str, ttl_s: int, producer: Callable[[], Awaitable[Any]]) -> Any:
    """Return the cached JSON-able value or produce, store and return it. None results are not cached."""
    hit = await cache_get(key)
    if hit is not None:
        return hit
    value = await producer()
    if value is not None:
        await cache_set(key, value, ttl_s)
    return value


async def cache_purge_expired() -> int:
    async with session_scope() as s:
        rows = (await s.execute(select(CatalogCache.key, CatalogCache.fetched_at, CatalogCache.ttl_s))).all()
        now = utcnow()
        dead = [k for k, at, ttl in rows if at + timedelta(seconds=ttl) < now]
        if dead:
            await s.execute(delete(CatalogCache).where(CatalogCache.key.in_(dead)))
        return len(dead)
