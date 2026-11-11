"""Moving a collection over from another music service (app/imports)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ImportIn, ImportItemsOut, ImportOut, ImportPickOut, ImportStartIn
from app.auth import current_user
from app.db import get_session
from app.imports import apply, core
from app.imports.sources import spotify
from app.imports.sources.base import SourceError
from app.jobs.queue import PRIORITY_IMPORT, PermanentError, enqueue, notify
from app.models import DownloadItem, Import, ImportCollection, ImportItem, Track, User

router = APIRouter(prefix="/api/imports", tags=["imports"])


async def _own(s: AsyncSession, user: User, import_id: int) -> Import:
    imp = await s.get(Import, import_id)
    if imp is None or imp.user_id != user.id:
        raise HTTPException(404, "Импорт не найден")
    return imp


@router.get("", response_model=list[ImportOut])
async def list_imports(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    rows = (await s.execute(select(Import).where(Import.user_id == user.id).order_by(Import.id.desc()))).scalars()
    return [await core.import_out(s, imp) for imp in rows]


@router.post("", response_model=ImportOut)
async def create(body: ImportIn, user: User = Depends(current_user),
                 s: AsyncSession = Depends(get_session, scope="function")):
    try:
        imp = await core.create(s, user.id, source=body.source, method=body.method, token=body.token, url=body.url)
    except SourceError as e:
        raise HTTPException(400, str(e)) from e
    await s.flush()
    out = await core.import_out(s, imp)
    await s.commit()
    notify()
    return out


@router.post("/spotify", response_model=ImportOut)
async def import_spotify(files: list[UploadFile] = File(), user: User = Depends(current_user),
                         s: AsyncSession = Depends(get_session, scope="function")):
    if not 1 <= len(files) <= spotify.MAX_FILES:
        raise HTTPException(400, f"Выберите от 1 до {spotify.MAX_FILES} файлов JSON")
    remaining = spotify.MAX_BYTES
    payloads = []
    for file in files:
        data = await file.read(remaining + 1)
        remaining -= len(data)
        if remaining < 0:
            raise HTTPException(413, "Файлы JSON превышают 8 МБ")
        payloads.append(data)
    try:
        snapshot = spotify.parse(payloads)
    except SourceError as e:
        raise HTTPException(400, str(e)) from e
    imp = Import(user_id=user.id, source="spotify", method="file", status="preview")
    s.add(imp)
    await s.flush()
    await core.store_snapshot(s, imp, snapshot)
    return await core.import_out(s, imp)


@router.get("/{import_id}", response_model=ImportOut)
async def get(import_id: int, user: User = Depends(current_user),
              s: AsyncSession = Depends(get_session, scope="function")):
    return await core.import_out(s, await _own(s, user, import_id))


@router.get("/{import_id}/items", response_model=ImportItemsOut)
async def items(import_id: int, collection_id: int | None = None, status: str | None = None,
                match: str | None = None, offset: int = 0, limit: int = Query(200, le=1000),
                user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    """Rows in the service's order; `status`/`match` — comma-separated filters (the report: `match=not_found`)."""
    imp = await _own(s, user, import_id)
    q = (select(ImportItem, ImportCollection.kind)
         .join(ImportCollection, ImportCollection.id == ImportItem.collection_id)
         .where(ImportItem.import_id == imp.id))
    if collection_id is not None:
        q = q.where(ImportItem.collection_id == collection_id)
    if status:
        q = q.where(ImportItem.status.in_([x.strip() for x in status.split(",") if x.strip()]))
    if match:
        q = q.where(ImportItem.match.in_([x.strip() for x in match.split(",") if x.strip()]))
    total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await s.execute(q.order_by(ImportCollection.position, ImportItem.position)
                            .offset(offset).limit(limit))).all()
    track_ids = {it.track_id for it, _ in rows if it.track_id}
    songs = dict((await s.execute(select(Track.id, Track.nd_song_id).where(Track.id.in_(track_ids)))).all()) \
        if track_ids else {}
    dl_ids = {it.download_item_id for it, _ in rows if it.download_item_id}
    dls = {d.id: d for d in (await s.execute(select(DownloadItem).where(DownloadItem.id.in_(dl_ids)))).scalars()} \
        if dl_ids else {}
    return ImportItemsOut(total=total, items=[
        core.item_out(it, kind, songs.get(it.track_id), dls.get(it.download_item_id)) for it, kind in rows])


@router.post("/{import_id}/start", response_model=ImportOut)
async def start(import_id: int, body: ImportStartIn, user: User = Depends(current_user),
                s: AsyncSession = Depends(get_session, scope="function")):
    imp = await _own(s, user, import_id)
    try:
        await core.start(s, imp, collection_ids=body.collection_ids, mode=body.mode,
                         download_albums=body.download_albums, keep_token=body.keep_token)
    except PermanentError as e:
        raise HTTPException(409, str(e)) from e
    await s.flush()
    out = await core.import_out(s, imp)
    await s.commit()
    notify()
    core.publish(imp)
    return out


@router.post("/{import_id}/download_all", response_model=ImportOut)
async def download_all(import_id: int, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    """Economy → full: what waits for a listen is downloaded now, in the background."""
    imp = await _own(s, user, import_id)
    if imp.status not in ("running", "done"):
        raise HTTPException(409, "Импорт ещё не запущен")
    await core.download_all(s, imp)
    await s.flush()
    out = await core.import_out(s, imp)
    await s.commit()
    notify()
    return out


@router.post("/{import_id}/sync", response_model=ImportOut)
async def sync(import_id: int, user: User = Depends(current_user),
               s: AsyncSession = Depends(get_session, scope="function")):
    """«Обновить»: read the collection again; only what is new is matched and acted on."""
    imp = await _own(s, user, import_id)
    if imp.status in core.ACTIVE:
        raise HTTPException(409, "Импорт ещё идёт")
    if imp.started_at is None:
        raise HTTPException(409, "Импорт ещё не запущен")
    if imp.method == "file":
        raise HTTPException(409, "Загрузите новый экспорт Spotify")
    if imp.method != "profile" and not imp.token_enc:
        raise HTTPException(409, "Вход в сервис удалён — начните новый перенос")
    imp.status = "fetching"
    await enqueue(s, "import_fetch", {"import_id": imp.id}, priority=PRIORITY_IMPORT)
    await s.flush()
    out = await core.import_out(s, imp)
    await s.commit()
    notify()
    return out


@router.delete("/{import_id}/token", response_model=ImportOut)
async def forget_token(import_id: int, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    """Remove the stored sign-in of the service (the import stays)."""
    imp = await _own(s, user, import_id)
    imp.token_enc = None
    imp.keep_token = False
    await s.flush()
    return await core.import_out(s, imp)


@router.post("/{import_id}/cancel", response_model=ImportOut)
async def cancel(import_id: int, user: User = Depends(current_user),
                 s: AsyncSession = Depends(get_session, scope="function")):
    """Stop: no more matching, the import's downloads that have not started are cancelled; done work stays."""
    imp = await _own(s, user, import_id)
    if imp.status in ("done", "cancelled", "failed"):
        raise HTTPException(409, "Импорт уже закончен")
    await apply.cancel_downloads(s, imp)
    imp.status = "cancelled"
    imp.token_enc = None if not imp.keep_token else imp.token_enc
    await s.flush()
    out = await core.import_out(s, imp)
    core.publish(imp)
    return out


@router.delete("/{import_id}")
async def remove(import_id: int, user: User = Depends(current_user),
                 s: AsyncSession = Depends(get_session, scope="function")):
    """Forget the import (and its sign-in). The music, the ♥ and the playlists it made stay."""
    await core.remove(s, await _own(s, user, import_id))
    return {"ok": True}


async def _own_item(s: AsyncSession, user: User, item_id: int) -> tuple[ImportItem, ImportCollection, Import]:
    it = await s.get(ImportItem, item_id)
    imp = await s.get(Import, it.import_id) if it else None
    if it is None or imp is None or imp.user_id != user.id:
        raise HTTPException(404, "Строка импорта не найдена")
    coll = await s.get(ImportCollection, it.collection_id)
    assert coll is not None
    return it, coll, imp


@router.post("/items/{item_id}/pick", response_model=ImportPickOut)
async def pick(item_id: int, user: User = Depends(current_user),
               s: AsyncSession = Depends(get_session, scope="function")):
    """A row that was not found (or found wrong): a visible download by its names, without the catalog match. The
    user picks the file on its page (`/api/downloads/{id}`), as for any download that needs review."""
    it, coll, imp = await _own_item(s, user, item_id)
    if coll.kind not in ("likes", "playlist"):
        raise HTTPException(400, "Выбрать файл можно только для трека")
    raw = it.raw_json or {}
    qj = {"query": apply.track_query(raw).model_dump(), "meta": {}, "import": {"id": imp.id, "item": it.id},
          "requested": True}  # a like gets its ♥ from the import, in order (`apply.sync_likes`)
    dl = await apply.new_download(s, imp, qj, 0)
    it.download_item_id, it.status, it.ref = dl.id, "queued", None
    await s.commit()
    notify()
    return ImportPickOut(download_item_id=dl.id)


@router.post("/items/{item_id}/skip")
async def skip(item_id: int, user: User = Depends(current_user),
               s: AsyncSession = Depends(get_session, scope="function")):
    """Leave this row out (it is not moved)."""
    it, _, _ = await _own_item(s, user, item_id)
    it.status = "skipped"
    return {"ok": True}
