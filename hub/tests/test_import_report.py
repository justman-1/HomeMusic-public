"""The report of an import counts every selected track once, by what it is (owned, exact, probable, not found) and,
separately, by where it is (downloaded, downloading, waiting, failed): finished downloads stay in the report, albums
and artists do not inflate the track counts."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import dispose, init_engine, session_scope
from app.imports import core
from app.models import (
    DownloadBatch,
    DownloadItem,
    Import,
    ImportCollection,
    ImportItem,
    User,
)


@pytest.fixture
async def db(hub_env, db_file: Path):
    init_engine(db_file)
    yield
    await dispose()


async def _import() -> tuple[Import, list[ImportCollection]]:
    async with session_scope() as s:
        user = User(nd_username="reporter", nd_password_enc="unused")
        s.add(user)
        await s.flush()
        imp = Import(user_id=user.id, source="yandex", method="token", status="running", mode="full")
        s.add(imp)
        await s.flush()
        batch = DownloadBatch(user_id=user.id, origin="import", title="t")
        s.add(batch)
        await s.flush()

        def coll(kind: str, selected: bool = True) -> ImportCollection:
            c = ImportCollection(import_id=imp.id, kind=kind, external_id=kind, title=kind, selected=selected,
                                 position=0)
            s.add(c)
            return c

        likes, playlist, albums, artists, dislikes = coll("likes"), coll("playlist"), coll("albums"), coll(
            "artists"), coll("dislikes", selected=False)
        await s.flush()

        def item(c: ImportCollection, ext: str, match: str | None, status: str, **kw) -> ImportItem:
            it = ImportItem(import_id=imp.id, collection_id=c.id, position=0, external_id=ext, match=match,
                            status=status, raw_json={"artist": "a", "title": ext, "duration_s": 200}, **kw)
            s.add(it)
            return it

        item(likes, "t1", "exact", "done")
        item(likes, "t2", "exact", "done")
        item(likes, "t3", "probable", "done")  # downloaded although only probable: it stays «probable» in the report
        item(likes, "t4", "owned", "done")
        item(likes, "t5", "not_found", "not_found")
        item(likes, "t6", "exact", "failed")
        item(likes, "t7", None, "pending")
        item(playlist, "t1", "exact", "done")  # the same song in a playlist: one track
        item(playlist, "t8", "probable", "queued")
        item(playlist, "t9", "exact", "waiting")
        item(albums, "a1", "exact", "done")
        item(albums, "a2", "exact", "queued")
        item(artists, "r1", "exact", "done")
        item(dislikes, "d1", None, "pending")  # not selected: never worked on, never counted
        for _ in range(5):  # downloads of albums and artists' releases are not tracks of the report
            s.add(DownloadItem(batch_id=batch.id, user_id=user.id, status="done",
                               query_json={"import": {"id": imp.id, "album": 1}}))
        await s.flush()
        return imp, [likes, playlist, albums, artists, dislikes]


async def test_report_counts_every_selected_track_once_including_finished_ones(db):
    imp, colls = await _import()
    async with session_scope() as s:
        st = await core.stats(s, await s.get(Import, imp.id), colls)

    assert st.tracks == 9
    # what each track is: the four verdicts and the not yet matched one add up to the tracks
    assert (st.owned, st.exact, st.probable, st.not_found, st.pending) == (1, 4, 2, 1, 1)
    assert st.owned + st.exact + st.probable + st.not_found + st.pending == st.tracks
    # where each track is: downloaded by the import (t1, t2, t3), under way, waiting for a listen, failed
    assert (st.done, st.downloading, st.waiting, st.failed) == (3, 1, 1, 1)
    # albums and artists are counted on their own and do not touch the track numbers (5 album downloads are done)
    assert (st.extras, st.extras_done) == (3, 2)


async def test_report_does_not_wait_for_collections_nobody_selected(db):
    imp, colls = await _import()
    async with session_scope() as s:
        st = await core.stats(s, await s.get(Import, imp.id), colls)
        pending_unselected = (await s.execute(
            select(ImportItem).where(ImportItem.import_id == imp.id, ImportItem.status == "pending",
                                     ImportItem.collection_id == colls[4].id))).scalars().all()

    assert len(pending_unselected) == 1
    assert st.pending == 1  # only t7 (the selected likes); the dislike is not there


async def test_a_matching_job_that_failed_for_good_is_queued_again(db):
    from app.jobs.queue import enqueue
    from app.models import Job, utcnow

    imp, _ = await _import()  # running
    async with session_scope() as s:
        job = await enqueue(s, "import_resolve", {"import_id": imp.id})
        job.status = "failed"  # it used up its attempts
    await core._rescue(job, RuntimeError("database is locked"))

    async with session_scope() as s:
        queued = (await s.execute(select(Job).where(Job.kind == "import_resolve", Job.status == "queued"))).scalars().all()
    assert len(queued) == 1 and queued[0].payload_json == {"import_id": imp.id}
    assert queued[0].run_after > utcnow()  # later, not at once


async def test_a_stopped_import_is_not_queued_again(db):
    from app.jobs.queue import enqueue
    from app.models import Job

    imp, _ = await _import()
    async with session_scope() as s:
        (await s.get(Import, imp.id)).status = "cancelled"
        job = await enqueue(s, "import_finish", {"import_id": imp.id})
        job.status = "failed"
    await core._rescue(job, RuntimeError("x"))

    async with session_scope() as s:
        assert (await s.execute(select(Job).where(Job.status == "queued"))).scalars().all() == []


async def test_a_running_import_without_a_job_is_started_again_at_start(db):
    from app.models import Job

    imp, _ = await _import()  # running, nothing queued: its chain broke
    assert await core.resume_running() == 1
    assert await core.resume_running() == 0  # now it has a job

    async with session_scope() as s:
        [job] = (await s.execute(select(Job).where(Job.kind == "import_resolve"))).scalars().all()
        assert job.payload_json == {"import_id": imp.id} and job.status == "queued"
        job.status = "running"  # a worker holds it (or it waits for a recheck): nothing to start
    assert await core.resume_running() == 0
