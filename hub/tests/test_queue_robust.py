"""A locked SQLite file must not strand a job or kill a worker: the outcome is written again, the worker lives on, and a
job left `running` by a lost worker goes back to the queue."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from app.db import dispose, init_engine, session_scope
from app.jobs import queue
from app.models import Job, utcnow


@pytest.fixture
async def db(db_file: Path, monkeypatch: pytest.MonkeyPatch):
    from app import config

    monkeypatch.setenv("CONFIG_FILE", str(db_file.parent / "none.yaml"))
    config.get_settings.cache_clear()
    config.get_settings().cfg.jobs.workers = 1
    monkeypatch.setattr(queue, "FINISH_BACKOFF_S", 0.0)
    init_engine(db_file)
    yield
    await dispose()
    config.get_settings.cache_clear()


def locked() -> OperationalError:
    return OperationalError("UPDATE jobs", {}, Exception("database is locked"))


async def _status(job_id: int) -> str:
    async with session_scope() as s:
        job = await s.get(Job, job_id)
        assert job is not None
        return job.status


async def test_reap_puts_a_lost_running_job_back_but_not_a_fresh_one(db):
    async with session_scope() as s:
        lost = (await queue.enqueue(s, "lost", {})).id
        fresh = (await queue.enqueue(s, "fresh", {})).id
    assert await queue._claim("w") and await queue._claim("w")
    async with session_scope() as s:
        await s.execute(update(Job).where(Job.id == lost).values(locked_at=utcnow() - timedelta(hours=2)))

    assert await queue.reap_stale(3600) == 1
    assert (await _status(lost), await _status(fresh)) == ("queued", "running")


async def test_the_outcome_is_written_again_after_a_locked_database(db, monkeypatch):
    async with session_scope() as s:
        job_id = (await queue.enqueue(s, "later", {})).id
    real = queue._finish_once
    calls = 0

    async def flaky(*a, **kw):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise locked()
        return await real(*a, **kw)

    monkeypatch.setattr(queue, "_finish_once", flaky)
    await queue._finish(job_id, ok=True)

    assert calls == 3
    assert await _status(job_id) == "done"


async def test_a_worker_survives_an_outcome_that_cannot_be_written(db, monkeypatch):
    ran: list[str] = []

    @queue.handler("robust_a")
    async def a(job):
        ran.append("a")

    @queue.handler("robust_b")
    async def b(job):
        ran.append("b")

    async def always_locked(*a, **kw):
        raise locked()

    monkeypatch.setattr(queue, "_finish_once", always_locked)
    async with session_scope() as s:
        await queue.enqueue(s, "robust_a", {})
        await queue.enqueue(s, "robust_b", {})
    pool = queue.WorkerPool(1)
    pool.start()
    try:
        for _ in range(100):
            if ran == ["a", "b"]:
                break
            queue.notify()
            await asyncio.sleep(0.05)
    finally:
        await pool.stop()

    assert ran == ["a", "b"]  # the only worker did not die on the first job's failed write
