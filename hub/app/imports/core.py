"""An import's life: sign-in → fetch (a snapshot of the collection) → preview → matching in batches → apply.

Job kinds (all below the user's work in the queue: `PRIORITY_IMPORT`):
- `import_auth`      — waits for the user to confirm the sign-in code, then fetches
- `import_fetch`     — reads the collection from the service into collections and items, checks the library
- `import_resolve`   — matches a batch of rows (`resolve.py`) and acts on them (`apply.py`), queues the next batch
- `import_playlists` — writes the Navidrome playlists and the ♥ in the service's order (coalesced: one per import)
- `import_finish`    — when the downloads are over: the last rewrite, ★ albums and artists, the token goes

Modes: `economy` — playlists and ♥ at once from what the library has, the rest is listened to from the sources
(the Import page, `playQueue` in the app) and downloaded once listened to (`listened`); `full` — everything is
downloaded in the background.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from datetime import timedelta
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ImportAuthOut,
    ImportCollectionOut,
    ImportItemOut,
    ImportOut,
    ImportStatsOut,
)
from app.config import get_settings
from app.db import session_scope
from app.events import bus
from app.imports import apply
from app.imports.resolve import CatalogDown, resolve_album, resolve_artist, resolve_track
from app.imports.sources import yandex
from app.imports.sources.base import ITEM_KIND, AuthPending, Snapshot, SourceError
from app.jobs.queue import PRIORITY_IMPORT, PermanentError, RetryLater, enqueue, handler, notify, on_final_failure
from app.library.owned import find_existing
from app.models import DownloadItem, Import, ImportCollection, ImportItem, Job, utcnow
from app.security import decrypt, encrypt

log = logging.getLogger(__name__)

RESOLVE_BATCH = 50
RESOLVE_CONCURRENCY = 4
PLAYLISTS_DELAY_S = 30  # playlists are rewritten at most this often while downloads come in
FINISH_RECHECK_S = 120
LOCK_RETRY_S = 1.0
LOCK_TRIES = 6  # one row of a batch meeting a locked SQLite file is tried again this many times
RESCUE_DELAY_S = 300  # a matching/finishing job that failed for good is queued again after this
BYTES_PER_S = 40_000  # 320 kbps
DEFAULT_DURATION_S = 240
# matching and downloading order: playlists, then likes (newest first), albums, artists; dislikes are only kept
KIND_ORDER = {"playlist": 0, "likes": 1, "albums": 2, "artists": 3, "dislikes": 4}
TRACK_KINDS = ("likes", "playlist")
ACTIVE = ("auth", "fetching", "running")


def _locked(exc: BaseException) -> bool:
    """A locked SQLite file, as it is or under the catalog's «down» (its cache lives in the same file)."""
    return isinstance(exc, OperationalError) or isinstance(exc.__cause__, OperationalError)


# --- state + events --------------------------------------------------------------------------------------


async def get_import(s: AsyncSession, import_id: int) -> Import | None:
    return await s.get(Import, import_id)


def _free_bytes() -> int | None:
    try:
        return shutil.disk_usage(get_settings().music_root).free
    except OSError:
        return None


# how far a row of a track that sits in several collections has got: the most advanced one is the track's
_PROGRESS = {"done": 0, "queued": 1, "waiting": 2, "failed": 3, "not_found": 4, "pending": 5}


def _further(new: tuple, old: tuple) -> bool:
    """`new` and `old` are (match, status, raw) of the same track from two collections."""
    if old[0] == "owned":
        return False
    return new[0] == "owned" or _PROGRESS.get(new[1], 9) < _PROGRESS.get(old[1], 9)


async def stats(s: AsyncSession, imp: Import, colls: list[ImportCollection]) -> ImportStatsOut:
    track_colls = {c.id for c in colls if c.selected and c.kind in TRACK_KINDS}
    extra_colls = {c.id for c in colls if c.selected and c.kind in ("albums", "artists")}
    rows = (await s.execute(
        select(ImportItem.collection_id, ImportItem.external_id, ImportItem.match, ImportItem.status,
               ImportItem.raw_json)
        .where(ImportItem.import_id == imp.id))).all()
    out = ImportStatsOut(free_bytes=_free_bytes())
    tracks: dict[str, tuple[str | None, str, dict]] = {}
    for cid, ext, match, status, raw in rows:
        if status == "skipped":
            continue
        if cid in track_colls:
            row = (match, status, raw or {})
            if (prev := tracks.get(ext)) is None or _further(row, prev):
                tracks[ext] = row
        elif cid in extra_colls:
            out.extras += 1
            out.extras_done += status in ("done", "not_found")  # nothing more will happen to a not found one
    out.tracks = len(tracks)
    for match, status, raw in tracks.values():
        # what it is: the verdicts add up to the tracks (a downloaded track stays under its verdict)
        if match == "owned":
            out.owned += 1
            continue
        if match in ("exact", "manual"):  # «manual»: the user picked the file by hand
            out.exact += 1
        elif match == "probable":
            out.probable += 1
        elif match == "not_found":
            out.not_found += 1
        # where it is
        if status == "done":
            out.done += 1
            continue
        out.to_download += 1
        out.est_bytes += (raw.get("duration_s") or DEFAULT_DURATION_S) * BYTES_PER_S
        if status == "pending":
            out.pending += 1
        elif status == "waiting":
            out.waiting += 1
        elif status == "queued":
            out.downloading += 1
        elif status == "failed":
            out.failed += 1
    out.activity_at = (await s.execute(
        select(func.max(DownloadItem.updated_at))
        .where(func.json_extract(DownloadItem.query_json, "$.import.id") == imp.id))).scalar_one_or_none()
    return out


async def import_out(s: AsyncSession, imp: Import) -> ImportOut:
    colls = (await s.execute(select(ImportCollection).where(ImportCollection.import_id == imp.id)
                             .order_by(ImportCollection.position))).scalars().all()
    counts: dict[int, tuple[int, int]] = {}
    for cid, n, owned in (await s.execute(
        select(ImportItem.collection_id, func.count(ImportItem.id),
               func.sum(case((ImportItem.match == "owned", 1), else_=0)))
        .where(ImportItem.import_id == imp.id, ImportItem.status != "skipped")
        .group_by(ImportItem.collection_id))).all():
        counts[cid] = (n, int(owned or 0))
    auth = None
    a = imp.auth_json or {}
    if imp.status == "auth" and a.get("user_code"):
        auth = ImportAuthOut(user_code=a["user_code"], verification_url=a["verification_url"],
                             expires_at=a["expires_at"])
    return ImportOut(
        id=imp.id, source=imp.source, method=imp.method, status=imp.status, mode=imp.mode, account=imp.account,
        has_token=bool(imp.token_enc), keep_token=imp.keep_token,
        download_albums=bool((imp.options_json or {}).get("download_albums")), auth=auth, error=imp.error,
        stats=await stats(s, imp, list(colls)),
        collections=[ImportCollectionOut(id=c.id, kind=c.kind, title=c.title, selected=c.selected,
                                         count=counts.get(c.id, (0, 0))[0], owned=counts.get(c.id, (0, 0))[1],
                                         cover_url=c.cover_url, nd_playlist_id=c.nd_playlist_id) for c in colls],
        created_at=imp.created_at, started_at=imp.started_at, finished_at=imp.finished_at,
    )


def item_out(it: ImportItem, kind: str, nd_song_id: str | None = None, dl: DownloadItem | None = None) -> ImportItemOut:
    raw = it.raw_json or {}
    return ImportItemOut(
        id=it.id, collection_id=it.collection_id, position=it.position, kind=ITEM_KIND.get(kind, "track"),
        artist=raw.get("artist") or raw.get("name") or "", title=raw.get("title") or raw.get("name") or "",
        album=raw.get("album"), duration_s=raw.get("duration_s"), cover_url=raw.get("cover_url"), ref=it.ref,
        match=it.match, score=it.score, status=it.status, nd_song_id=nd_song_id, download_item_id=it.download_item_id,
        download_status=dl.status if dl else None, error=dl.error if dl else None,
    )


def publish(imp: Import) -> None:
    bus.publish(imp.user_id, "import", {"id": imp.id, "status": imp.status})


async def set_status(import_id: int, status: str, **fields: Any) -> Import | None:
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None:
            return None
        imp.status = status
        for k, v in fields.items():
            setattr(imp, k, v)
    publish(imp)
    return imp


async def enqueue_once(s: AsyncSession, kind: str, import_id: int, *, delay_s: float = 0) -> None:
    """One queued job of this kind per import (a batch of downloads coming in asks for one rewrite)."""
    pending = (await s.execute(
        select(Job.id).where(Job.kind == kind, Job.status == "queued",
                             func.json_extract(Job.payload_json, "$.import_id") == import_id).limit(1)
    )).scalar_one_or_none()
    if pending is None:
        await enqueue(s, kind, {"import_id": import_id}, delay_s=delay_s, priority=PRIORITY_IMPORT)


# --- creating an import ------------------------------------------------------------------------------------


async def create(s: AsyncSession, user_id: int, *, source: str, method: str, token: str | None = None,
                 url: str | None = None) -> Import:
    if source != "yandex":
        raise SourceError("Этот сервис пока не поддерживается")
    imp = Import(user_id=user_id, source=source, method=method, status="fetching", auth_json={}, options_json={})
    if method == "device":
        s.add(imp)
        await s.flush()
        code = await yandex.device_start(f"homemusic-{user_id}-{imp.id}")
        expires = utcnow() + timedelta(seconds=int(code.get("expires_in") or 300))
        imp.status = "auth"
        imp.auth_json = {"device_code": encrypt(code["device_code"]), "user_code": code["user_code"],
                         "verification_url": code.get("verification_url") or "https://ya.ru/device",
                         "interval": int(code.get("interval") or 5), "expires_at": expires.isoformat()}
        await enqueue(s, "import_auth", {"import_id": imp.id}, delay_s=imp.auth_json["interval"],
                      priority=PRIORITY_IMPORT)
        return imp
    if method == "token":
        if not (token or "").strip():
            raise SourceError("Вставьте токен")
        imp.token_enc = encrypt(token.strip())
    elif method == "profile":
        imp.account = yandex.profile_login(url or "")
    else:
        raise SourceError("Неизвестный способ")
    s.add(imp)
    await s.flush()
    await enqueue(s, "import_fetch", {"import_id": imp.id}, priority=PRIORITY_IMPORT)
    return imp


@handler("import_auth")
async def handle_auth(job: Job) -> None:
    import_id = int(job.payload_json["import_id"])
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status != "auth":
            return
        auth = dict(imp.auth_json or {})
    try:
        token = await yandex.device_poll(decrypt(auth["device_code"]))
    except AuthPending as e:
        if utcnow().isoformat() > auth["expires_at"]:
            await set_status(import_id, "failed", error="Код входа устарел — получите новый", auth_json={})
            return
        raise RetryLater(auth.get("interval") or 5, str(e)) from e
    except SourceError as e:
        await set_status(import_id, "failed", error=str(e), auth_json={})
        return
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status != "auth":
            return
        imp.token_enc = encrypt(token)
        imp.auth_json = {}
        imp.status = "fetching"
        await enqueue(s, "import_fetch", {"import_id": imp.id}, priority=PRIORITY_IMPORT)
    notify()
    publish(imp)


# --- fetching ----------------------------------------------------------------------------------------------


async def fetch_snapshot(imp: Import) -> Snapshot:
    token = decrypt(imp.token_enc) if imp.token_enc else None
    login = imp.account if imp.method == "profile" else None
    return await asyncio.to_thread(yandex.fetch, token, login)


async def store_snapshot(s: AsyncSession, imp: Import, snap: Snapshot) -> int:
    """Collections and items of the snapshot; a repeated fetch (sync) keeps what it had, adds what is new, follows
    the new order and drops what left the service. Returns the number of new rows."""
    if snap.account and imp.method != "profile":
        imp.account = snap.account
    colls = {c.external_id: c for c in (await s.execute(
        select(ImportCollection).where(ImportCollection.import_id == imp.id))).scalars()}
    fresh = 0
    for pos, sc in enumerate(snap.collections):
        coll = colls.get(sc.external_id)
        if coll is None:
            coll = ImportCollection(import_id=imp.id, kind=sc.kind, external_id=sc.external_id, title=sc.title,
                                    selected=sc.selected, nd_song_ids=[])
            s.add(coll)
            await s.flush()
        coll.title, coll.position, coll.cover_url = sc.title, pos, sc.cover_url
        old = {i.external_id: i for i in (await s.execute(
            select(ImportItem).where(ImportItem.collection_id == coll.id))).scalars()}
        seen: set[str] = set()
        for ipos, si in enumerate(sc.items):
            seen.add(si.external_id)
            it = old.get(si.external_id)
            if it is None:
                it = ImportItem(import_id=imp.id, collection_id=coll.id, external_id=si.external_id, raw_json=si.raw,
                                status="pending")
                s.add(it)
                fresh += 1
                if sc.kind in ("likes", "playlist", "dislikes"):
                    lib = await find_existing(s, artist=si.raw.get("artist") or "", title=si.raw.get("title") or "",
                                              duration_s=si.raw.get("duration_s"))
                    if lib is not None:
                        it.match, it.track_id, it.score = "owned", lib.id, 1.0
            elif it.status == "skipped" and (it.raw_json or {}).get("removed"):
                it.status, it.raw_json = "pending", si.raw
            it.position = ipos
        for ext, it in old.items():
            if ext not in seen and it.status != "skipped":
                it.status, it.raw_json = "skipped", {**(it.raw_json or {}), "removed": True}
    await s.flush()
    return fresh


@handler("import_fetch")
async def handle_fetch(job: Job) -> None:
    import_id = int(job.payload_json["import_id"])
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status not in ("fetching", "running", "done"):
            return
    try:
        snap = await fetch_snapshot(imp)
    except SourceError as e:
        await set_status(import_id, "failed" if imp.started_at is None else imp.status, error=str(e))
        return
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None:
            return
        fresh = await store_snapshot(s, imp, snap)
        imp.error = None
        if imp.started_at is None:
            imp.status = "preview"
        else:  # «Обновить»: the new rows go the way the first ones went
            imp.status, imp.finished_at = "running", None
            await enqueue(s, "import_resolve", {"import_id": imp.id}, priority=PRIORITY_IMPORT)
    log.info("import %s: fetched %s collections, %s new rows", import_id, len(snap.collections), fresh)
    notify()
    publish(imp)


@on_final_failure("import_resolve")
@on_final_failure("import_finish")
async def _rescue(job: Job, exc: Exception) -> None:
    """Matching and finishing are one chain, each job queues the next: a job that used up its attempts (a locked
    SQLite file, the catalog down for long) must not leave a running import with nothing queued. The chain is
    started again a little later; the import stays `running` meanwhile."""
    import_id = int(job.payload_json["import_id"])
    for attempt in range(1, 4):
        try:
            async with session_scope() as s:
                imp = await s.get(Import, import_id)
                if imp is None or imp.status != "running":
                    return
                await enqueue_once(s, job.kind, import_id, delay_s=RESCUE_DELAY_S)
            break
        except OperationalError:  # the same locked file that failed the job
            if attempt == 3:
                raise
            await asyncio.sleep(2.0 * attempt)
    log.warning("import %s: %s failed for good (%r), queued again in %ss", import_id, job.kind, exc, RESCUE_DELAY_S)
    notify()


async def resume_running() -> int:
    """At start: a `running` import that has no matching or finishing job left (its chain broke before this fix, or a
    job was lost) is started again. Returns how many."""
    async with session_scope() as s:
        n = 0
        for import_id in (await s.execute(select(Import.id).where(Import.status == "running"))).scalars().all():
            alive = (await s.execute(
                select(Job.id).where(Job.kind.in_(("import_resolve", "import_finish")),
                                     Job.status.in_(("queued", "running")),
                                     func.json_extract(Job.payload_json, "$.import_id") == import_id).limit(1)
            )).scalar_one_or_none()
            if alive is None:
                await enqueue(s, "import_resolve", {"import_id": import_id}, priority=PRIORITY_IMPORT)
                n += 1
        return n


@on_final_failure("import_fetch")
async def _fetch_failed(job: Job, exc: Exception) -> None:
    await set_status(int(job.payload_json["import_id"]), "failed", error=f"Не удалось прочитать коллекцию: {exc}"[:500])


# --- start, matching ----------------------------------------------------------------------------------------


async def start(s: AsyncSession, imp: Import, *, collection_ids: list[int], mode: str, download_albums: bool,
                keep_token: bool) -> None:
    if imp.status != "preview":
        raise PermanentError("Импорт уже запущен")
    for c in (await s.execute(select(ImportCollection).where(ImportCollection.import_id == imp.id))).scalars():
        c.selected = c.id in collection_ids
    imp.mode = mode
    imp.keep_token = keep_token
    imp.options_json = {**(imp.options_json or {}), "download_albums": download_albums}
    imp.status = "running"
    imp.started_at = utcnow()
    await enqueue(s, "import_resolve", {"import_id": imp.id}, priority=PRIORITY_IMPORT)


async def _next_batch(s: AsyncSession, imp: Import) -> list[tuple[ImportItem, ImportCollection]]:
    rows = (await s.execute(
        select(ImportItem, ImportCollection)
        .join(ImportCollection, ImportCollection.id == ImportItem.collection_id)
        .where(ImportItem.import_id == imp.id, ImportItem.status == "pending", ImportCollection.selected.is_(True))
    )).all()
    # likes from the oldest: each downloaded one then lands on top of «Понравившиеся» (`apply.sync_likes`)
    rows = sorted(rows, key=lambda r: (KIND_ORDER.get(r[1].kind, 9), r[1].position,
                                       -r[0].position if r[1].kind == "likes" else r[0].position))
    return [(it, c) for it, c in rows[:RESOLVE_BATCH]]


@handler("import_resolve")
async def handle_resolve(job: Job) -> None:
    import_id = int(job.payload_json["import_id"])
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status != "running":
            return
        batch = await _next_batch(s, imp)
        ids = [(it.id, c.kind) for it, c in batch]
    sem = asyncio.Semaphore(RESOLVE_CONCURRENCY)
    # an album or an artist holds its write transaction open across catalog calls and the queuing of its downloads:
    # several at once keep the SQLite file locked for everyone, so they go one at a time (tracks stay parallel)
    extras = asyncio.Semaphore(1)
    down = False

    async def match_one(item_id: int, kind: str) -> None:
        async with session_scope() as s:
            it = await s.get(ImportItem, item_id)
            if it is None or it.status != "pending":
                return
            raw = it.raw_json or {}
            if kind == "albums":
                r = await resolve_album(s, imp.source, it.external_id, raw)
            elif kind == "artists":
                r = await resolve_artist(s, imp.source, it.external_id, raw)
            elif it.match == "owned" and it.track_id:
                return await apply.decide(s, imp, it, kind, None)
            else:
                r = await resolve_track(s, imp.source, it.external_id, raw)
            # resolving wrote the source map: end that transaction now. `decide` calls the catalog again (the tracks of
            # an album) and must not wait for it while this session holds the write lock of the whole SQLite file
            await s.commit()
            await apply.decide(s, imp, it, kind, r)

    async def one(item_id: int, kind: str) -> None:
        nonlocal down
        async with sem, (extras if kind in ("albums", "artists") else contextlib.nullcontext()):
            for attempt in range(1, LOCK_TRIES + 1):
                if down:
                    return
                try:
                    return await match_one(item_id, kind)
                except (CatalogDown, OperationalError) as e:
                    if _locked(e) and attempt < LOCK_TRIES:
                        # several rows write at once (their matches, the catalog's cache): a new session sees the
                        # other writer's commit, so this row alone is tried again instead of the whole batch
                        await asyncio.sleep(LOCK_RETRY_S * attempt)
                        continue
                    if isinstance(e, CatalogDown) and not _locked(e):
                        log.warning("import %s: catalog down: %s", import_id, e)
                        down = True
                        return
                    raise

    await asyncio.gather(*(one(i, k) for i, k in ids))
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status != "running":
            return
        await enqueue_once(s, "import_playlists", import_id, delay_s=0 if not down else PLAYLISTS_DELAY_S)
        if down:
            await enqueue(s, "import_resolve", {"import_id": import_id}, delay_s=60, priority=PRIORITY_IMPORT)
        elif await _next_batch(s, imp):
            await enqueue(s, "import_resolve", {"import_id": import_id}, priority=PRIORITY_IMPORT)
        else:
            await enqueue_once(s, "import_finish", import_id)
    notify()
    publish(imp)


@handler("import_playlists")
async def handle_playlists(job: Job) -> None:
    import_id = int(job.payload_json["import_id"])
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status in ("failed", "preview"):  # a stopped import keeps what it has done in order
            return
        await apply.refresh_items(s, imp)
        await apply.sync_playlists(s, imp)
        await apply.sync_likes(s, imp)
    publish(imp)


@handler("import_finish")
async def handle_finish(job: Job) -> None:
    import_id = int(job.payload_json["import_id"])
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status != "running":
            return
        await apply.refresh_items(s, imp)
        active = await apply.active_downloads(s, imp)
    if active:
        raise RetryLater(FINISH_RECHECK_S, f"{active} downloads under way")
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None or imp.status != "running":
            return
        await apply.sync_playlists(s, imp)
        await apply.sync_likes(s, imp)
        await apply.star_albums_artists(s, imp)
        imp.status = "done"
        imp.finished_at = utcnow()
        if not imp.keep_token:
            imp.token_enc = None
    log.info("import %s done", import_id)
    publish(imp)


# --- the user's decisions -----------------------------------------------------------------------------------


async def download_all(s: AsyncSession, imp: Import) -> int:
    """Economy → full: what waits for a listen is downloaded now."""
    imp.mode = "full"
    rows = (await s.execute(
        select(ImportItem, ImportCollection).join(ImportCollection, ImportCollection.id == ImportItem.collection_id)
        .where(ImportItem.import_id == imp.id, ImportItem.status == "waiting"))).all()
    n = 0
    for it, coll in rows:
        if coll.kind == "albums":
            if (imp.options_json or {}).get("download_albums"):
                n += await apply.queue_album(s, imp, it)
            continue
        if await apply.queue_track(s, imp, it, coll.kind):
            n += 1
    if imp.status == "done" and n:
        imp.status, imp.finished_at = "running", None
        await enqueue_once(s, "import_finish", imp.id)
    return n


async def listened(user_id: int, ref: str) -> None:
    """A track of an economy import was listened to: download it (the user's own work, not background)."""
    async with session_scope() as s:
        rows = (await s.execute(
            select(ImportItem, ImportCollection, Import)
            .join(ImportCollection, ImportCollection.id == ImportItem.collection_id)
            .join(Import, Import.id == ImportItem.import_id)
            .where(Import.user_id == user_id, ImportItem.ref == ref, ImportItem.status == "waiting")
        )).all()
        for it, coll, imp in rows:
            await apply.queue_track(s, imp, it, coll.kind, priority=0)
    if rows:
        notify()


async def downloads_done(s: AsyncSession, items: list[DownloadItem]) -> None:
    """Downloads of imports have finished: their playlists are rewritten soon (one rewrite for many)."""
    ids = {int(imp["id"]) for i in items if (imp := (i.query_json or {}).get("import"))}
    for import_id in ids:
        await enqueue_once(s, "import_playlists", import_id, delay_s=PLAYLISTS_DELAY_S)


async def remove(s: AsyncSession, imp: Import) -> None:
    """The import and its rows go; the music, the ♥ and the playlists stay. Its queued downloads are cancelled."""
    await apply.cancel_downloads(s, imp)
    await s.execute(delete(ImportItem).where(ImportItem.import_id == imp.id))
    await s.execute(delete(ImportCollection).where(ImportCollection.import_id == imp.id))
    await s.delete(imp)


__all__ = ["ACTIVE", "create", "download_all", "downloads_done", "get_import", "import_out", "item_out", "listened",
           "remove", "start"]
