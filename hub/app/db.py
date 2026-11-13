"""SQLite (WAL) engine, async sessions and Alembic migrations on startup."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.config import get_settings

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# --- who holds the write lock --------------------------------------------------------------------------------
# SQLite has one writer at a time: a write transaction left open across a slow await (a catalog or Navidrome call)
# makes every other writer wait `busy_timeout` and fail with «database is locked». The first write of a transaction is
# noted with the place its session was opened at (`session_scope`); a transaction that held the lock longer than SLOW_WRITE_S is logged
# at its end, and a «database is locked» lists who holds the lock at that moment.
SLOW_WRITE_S = float(os.environ.get("HUB_DB_SLOW_WRITE_S", "3"))
_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
# id(connection record info) -> (monotonic start, where, the task): the task by weakref, an id() is reused
_holders: dict[int, tuple[float, str, weakref.ref | None]] = {}


def _caller() -> str:
    """Where in the Hub's code a session is opened. Called from `session_scope`, a plain frame of the running task:
    reading the frames of the current stack is safe (unlike the frames of coroutines parked by SQLAlchemy's greenlet,
    which may crash the interpreter)."""
    f = sys._getframe(1)
    while f is not None:
        name = f.f_code.co_filename
        if "/app/" in name and not name.endswith("/app/db.py") and "site-packages" not in name:
            return f"{name.rsplit('/app/', 1)[-1]}:{f.f_lineno} {f.f_code.co_name}"
        f = f.f_back
    return "?"


def _after_begin(session, _transaction, connection) -> None:
    connection.info["site"] = session.info.get("site", "?")


def _where(conn) -> str:
    task = _task()
    site = conn.info.get("site", "?") if conn is not None else "?"
    return f"{task.get_name() if task is not None else '?'}: {site}"


def _task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _before_execute(conn, _cursor, statement, _params, _context, _executemany) -> None:
    if id(conn.info) not in _holders and statement.lstrip().upper().startswith(_WRITES):
        task = _task()
        _holders[id(conn.info)] = (time.monotonic(), _where(conn), weakref.ref(task) if task is not None else None)


def task_holds_write() -> bool:
    """The current task has a write transaction open: another session of it that writes would wait for itself
    (`busy_timeout`, then «database is locked»), so best-effort writes (the catalog cache) are skipped."""
    me = _task()
    return me is not None and any(ref is not None and ref() is me for _, _, ref in _holders.values())


def _checkin(_dbapi_conn, record) -> None:
    """Back to the pool: whatever it held is over (a pool reset rolls back below the commit/rollback events)."""
    _holders.pop(id(record.info), None)


def _end(conn) -> None:
    held = _holders.pop(id(conn.info), None)
    if held is not None and (dur := time.monotonic() - held[0]) > SLOW_WRITE_S:
        log.warning("slow write transaction: held the lock %.1fs, began at %s", dur, held[1])


def holders() -> list[str]:
    """Write transactions open now: how long and where they began."""
    now = time.monotonic()
    return [f"{now - t:.1f}s {where}" for t, where, _ in sorted(_holders.values(), key=lambda h: h[0])]


def _on_error(ctx) -> None:
    if "database is locked" in str(ctx.original_exception):
        log.warning("database is locked at %s; holding the lock: %s", _where(ctx.connection),
                    holders() or "nobody in this process")


event.listen(Session, "after_begin", _after_begin)


def _set_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def init_engine(db_path: Path | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", connect_args={"timeout": 30})
    event.listen(_engine.sync_engine, "connect", _set_pragmas)
    event.listen(_engine.sync_engine, "before_cursor_execute", _before_execute)
    event.listen(_engine.sync_engine, "commit", _end)
    event.listen(_engine.sync_engine, "rollback", _end)
    event.listen(_engine.sync_engine, "handle_error", _on_error)
    event.listen(_engine.sync_engine.pool, "checkin", _checkin)
    _holders.clear()
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session that commits on success and rolls back on error."""
    async with sessionmaker()() as s:
        s.info["site"] = _caller()
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Use with `scope="function"`: the commit then happens before the response is sent
    (with the default request scope a client could see a stale state right after a write, e.g. 401 after login),
    and long responses (SSE) don't hold a connection."""
    async with session_scope() as s:
        yield s


def run_migrations(db_path: Path | None = None) -> None:
    """alembic upgrade head (sync, called once at startup before the event loop work starts)."""
    from alembic import command
    from alembic.config import Config

    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    # keep the app's JSON logging (env.py would otherwise apply alembic.ini's logging config)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
