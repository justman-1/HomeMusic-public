"""A write transaction held open across an await is visible: who holds the lock now, and a slow one is logged with the
place in the Hub's code it began at."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from app import db
from app.jobs import queue


@pytest.fixture
async def engine(db_file: Path, monkeypatch: pytest.MonkeyPatch):
    from app import config

    monkeypatch.setenv("CONFIG_FILE", str(db_file.parent / "none.yaml"))
    config.get_settings.cache_clear()
    db.init_engine(db_file)
    yield
    await db.dispose()
    config.get_settings.cache_clear()


async def test_a_slow_write_transaction_is_logged_with_where_it_began(engine, monkeypatch, caplog):
    monkeypatch.setattr(db, "SLOW_WRITE_S", 0.01)
    caplog.set_level(logging.WARNING, logger="app.db")

    async with db.session_scope() as s:
        await queue.enqueue(s, "probe", {})  # the first write takes the lock
        held = db.holders()
        await asyncio.sleep(0.05)  # e.g. a catalog call inside the transaction

    assert len(held) == 1 and "test_a_slow_write_transaction_is_logged_with_where_it_began" in held[0]
    assert db.holders() == []  # committed: nobody holds it
    [rec] = [r for r in caplog.records if "slow write transaction" in r.getMessage()]
    assert "test_a_slow_write_transaction_is_logged_with_where_it_began" in rec.getMessage()


async def test_a_quick_write_is_not_logged(engine, caplog):
    caplog.set_level(logging.WARNING, logger="app.db")
    async with db.session_scope() as s:
        await queue.enqueue(s, "probe", {})
    assert not [r for r in caplog.records if "slow write" in r.getMessage()]


async def test_the_cache_does_not_wait_for_its_own_tasks_write_lock(engine):
    """An album row held its write transaction and the catalog, inside it, cached the album through its own session:
    that session waited for the lock of its own task for `busy_timeout`, then «database is locked»."""
    import time

    from app.catalog.cache import cache_get, cached

    async def produce():
        return {"album": 1}

    t0 = time.monotonic()
    async with db.session_scope() as s:
        await queue.enqueue(s, "probe", {})  # this task now holds the write lock
        got = await asyncio.wait_for(cached("dz:album:1", 60, produce), 3)

    assert got == {"album": 1} and time.monotonic() - t0 < 3  # at once, not after busy_timeout
    assert await cache_get("dz:album:1") is None  # not cached then
    assert await cached("dz:album:1", 60, produce) == {"album": 1}
    assert await cache_get("dz:album:1") == {"album": 1}  # outside a held transaction it is cached as before
