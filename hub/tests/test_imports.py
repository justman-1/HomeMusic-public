"""Imports through the HTTP API: preview → matching → ♥ and playlists in the service's order → downloads below the
user's work. The service is a ready snapshot (its parsing: test_import_yandex.py), the catalog is FakeDeezer,
Navidrome a mock that keeps playlists and the order of stars. Downloads are not run (no ffmpeg needed): the tests
look at what is queued and play its end by hand.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.support import FakeDeezer, FakeProvider, app_client, cand


def track(artist: str, title: str, duration_s: int | None = 200, **extra) -> dict:
    return {"artist": artist, "artists": [artist], "title": title, "duration_s": duration_s, **extra}


OSEN = track("ДДТ", "Осень", 260)  # in the library (ndB)
KROVI = track("Кино", "Группа крови", 245)  # in the catalog (dz:100)
GOROD = track("Аквариум", "Город золотой", 190)  # in the library (ndA)
NONE = track("Неизвестный", "Нет такого", 180)  # nowhere


def snapshot():
    from app.imports.sources.base import SnapCollection, SnapItem, Snapshot

    return Snapshot(account="ivan", collections=[
        # newest like first, as Yandex lists them: Город золотой is the oldest
        SnapCollection("likes", "likes", "Мне нравится", [
            SnapItem("2", OSEN), SnapItem("1", KROVI), SnapItem("3", GOROD)]),
        SnapCollection("playlist", "77:1001", "Дорога", [
            SnapItem("1", KROVI), SnapItem("3", GOROD), SnapItem("9", NONE), SnapItem("2", OSEN)]),
        SnapCollection("albums", "albums", "Альбомы", [SnapItem("500", {"artist": "Кино", "title": "Группа крови"})]),
        SnapCollection("artists", "artists", "Исполнители", [SnapItem("600", {"name": "Кино"})]),
        SnapCollection("dislikes", "dislikes", "Не нравится", [SnapItem("8", track("Кто-то", "Что-то"))]),
    ])


@pytest.mark.parametrize("suffix", ["", " (из фильма «Город»)", " (prod. by Producer)"],
                         ids=["latin-only", "soundtrack", "producer"])
async def test_import_matches_hitmoz_after_deezer_miss(monkeypatch, suffix):
    from app.imports.resolve import catalog_track
    from app.services import svc

    def search(q):
        if q.title == "Midnight":
            candidate = cand("7", "https://audio.test/7", title="Midnight")
            return [candidate.model_copy(update={"artist": "Example", "duration_s": 200})]
        return []

    deezer = FakeDeezer()
    hitmoz = FakeProvider(cands=search)
    monkeypatch.setattr(svc, "deezer", deezer)
    monkeypatch.setattr(svc, "providers", {"hitmoz": hitmoz})

    result = await catalog_track(track("Example", f"Midnight{suffix}"))

    assert deezer.searched == (2 if suffix else 1)
    assert hitmoz.searched == 1
    assert (result.match, result.ref) == ("probable", "hm:7")


@pytest.fixture
async def env(hub_env, monkeypatch):
    from app.imports.sources import yandex
    from app.main import create_app
    from app.services import svc

    state: dict = {"playlists": {}, "stars": [], "star_log": [], "fetches": []}

    def nd_handler(request: httpx.Request) -> httpx.Response:
        ep = request.url.path.rsplit("/", 1)[-1]
        p = request.url.params
        ok = {"status": "ok", "version": "1.16.1"}

        def reply(**body) -> httpx.Response:
            return httpx.Response(200, json={"subsonic-response": {**ok, **body}})

        if ep == "getUser":
            return reply(user={"adminRole": True})
        if ep == "createPlaylist":
            pid = p.get("playlistId") or f"pl{len(state['playlists']) + 1}"
            name = p.get("name") or state["playlists"].get(pid, {}).get("name")
            state["playlists"][pid] = {"name": name, "songs": p.get_list("songId")}  # with an id: replaces
            return reply(playlist={"id": pid, "name": name})
        if ep == "updatePlaylist":
            state["playlists"][p["playlistId"]]["songs"] += p.get_list("songIdToAdd")
            return reply()
        if ep in ("star", "unstar"):
            ids = p.get_list("id")
            state["star_log"].append((ep, ids))
            state["stars"] = [x for x in state["stars"] if x not in ids] + (ids if ep == "star" else [])
            return reply()
        if ep == "getStarred2":
            return reply(starred2={"song": [{"id": i} for i in state["stars"]]})
        return reply()

    def fake_fetch(token, login):
        state["fetches"].append((token, login))
        return state.get("snapshot") or snapshot()

    monkeypatch.setattr(yandex, "fetch", fake_fetch)
    app = create_app()
    async with app.router.lifespan_context(app):
        svc.providers = {"hitmoz": FakeProvider("hitmoz", [])}
        svc.deezer = FakeDeezer()
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd_handler)
            async with app_client(app) as client:
                await add_library_track(GOROD, "ndA")
                await add_library_track(OSEN, "ndB")
                yield {"client": client, "state": state, "mock": mock}


async def add_library_track(raw: dict, nd_song_id: str) -> int:
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    async with session_scope() as s:
        t = Track(uid=str(uuid.uuid4()), path=f"{raw['artist']}/{raw['title']}.mp3", nd_song_id=nd_song_id,
                  artist=raw["artist"], title=raw["title"], duration_s=raw["duration_s"],
                  fingerprint=fingerprint(raw["artist"], raw["title"]))
        s.add(t)
        await s.flush()
        return t.id


async def run_imports(max_jobs: int = 50) -> None:
    """Runs the queued import jobs (not downloads), delays ignored; a job put back to wait (`RetryLater`) stops it."""
    from app.db import session_scope
    from app.jobs.queue import run_job
    from app.models import Job

    seen: set[int] = set()
    for _ in range(max_jobs):
        async with session_scope() as s:
            job = (await s.execute(select(Job).where(Job.status == "queued", Job.kind.like("import_%"))
                                   .order_by(Job.id).limit(1))).scalar_one_or_none()
            if job is None or job.id in seen:
                return
            seen.add(job.id)
            job.status, job.attempts = "running", job.attempts + 1
        await run_job(job)


async def jobs(kind: str) -> list:
    from app.db import session_scope
    from app.models import Job

    async with session_scope() as s:
        return list((await s.execute(select(Job).where(Job.kind == kind, Job.status == "queued"))).scalars())


async def prepared(c: httpx.AsyncClient) -> dict:
    r = await c.post("/api/imports", json={"source": "yandex", "method": "token", "token": "secret-token"})
    assert r.status_code == 200, r.text
    await run_imports()
    r = await c.get(f"/api/imports/{r.json()['id']}")
    assert r.json()["status"] == "preview", r.json()
    return r.json()


async def test_concurrent_import_matches_share_the_source_mapping(env, monkeypatch):
    from app.db import session_scope
    from app.imports.resolve import resolve_track
    from app.models import SourceMap
    from app.services import svc

    # Both collections miss the cache before the catalog answers; no timing sleeps.
    both_searching = asyncio.Event()
    calls = 0
    search = svc.deezer.search_tracks

    async def together(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_searching.set()
        await both_searching.wait()
        return await search(*args, **kwargs)

    monkeypatch.setattr(svc.deezer, "search_tracks", together)

    async def match():
        async with session_scope() as s:
            return await resolve_track(s, "yandex", "same-track", KROVI)

    results = await asyncio.wait_for(asyncio.gather(match(), match()), timeout=1)
    assert [(r.match, r.ref) for r in results] == [("exact", "dz:100"), ("exact", "dz:100")]
    async with session_scope() as s:
        rows = list((await s.execute(select(SourceMap))).scalars())
        assert [(r.source, r.kind, r.external_id, r.ref) for r in rows] == [
            ("yandex", "track", "same-track", "dz:100")
        ]


async def items(c: httpx.AsyncClient, imp: dict, **params) -> list[dict]:
    r = await c.get(f"/api/imports/{imp['id']}/items", params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_spotify_export_preview_and_economy_import_preserve_playlist_order(env):
    def track(raw, key):
        return {"track": {"artistName": raw["artist"], "trackName": raw["title"],
                          "trackUri": f"spotify:track:{key}"}}

    import json

    playlists = {"playlists": [{"name": "Spotify mix", "items": [
        track(OSEN, "AB1"), {"episode": {"episodeName": "Podcast"}}, track(GOROD, "AB2"), track(OSEN, "AB1"),
    ]}]}
    library = {"tracks": [{"artist": GOROD["artist"], "track": GOROD["title"], "uri": "spotify:track:AB2"}]}
    client = env["client"]
    response = await client.post("/api/imports/spotify", files=[
        ("files", ("Playlist1.json", json.dumps(playlists), "application/json")),
        ("files", ("YourLibrary.json", json.dumps(library), "application/json")),
    ])
    assert response.status_code == 200, response.text
    imp = response.json()
    assert (imp["source"], imp["method"], imp["status"]) == ("spotify", "file", "preview")
    assert imp["stats"]["tracks"] == imp["stats"]["owned"] == 2
    response = await client.post(f"/api/imports/{imp['id']}/start", json={
        "mode": "economy", "collection_ids": [c["id"] for c in imp["collections"]],
    })
    assert response.status_code == 200, response.text
    await run_imports()
    assert any(p["name"] == "Spotify mix" and p["songs"] == ["ndB", "ndA"]
               for p in env["state"]["playlists"].values())
    assert "ndA" in env["state"]["stars"]
    assert (await client.post(f"/api/imports/{imp['id']}/sync")).status_code == 409


async def test_preview_counts_what_the_library_has(env):
    c = env["client"]
    imp = await prepared(c)
    assert "secret-token" not in str(imp) and imp["has_token"] and imp["account"] == "ivan"
    assert env["state"]["fetches"] == [("secret-token", None)]
    assert [(x["kind"], x["title"], x["count"], x["owned"]) for x in imp["collections"]] == [
        ("likes", "Мне нравится", 3, 2), ("playlist", "Дорога", 4, 2), ("albums", "Альбомы", 1, 0),
        ("artists", "Исполнители", 1, 0), ("dislikes", "Не нравится", 1, 0)]
    st = imp["stats"]
    # distinct tracks of likes and playlists: Осень and Город золотой are owned, the other two would be downloaded
    assert (st["tracks"], st["owned"], st["to_download"]) == (4, 2, 2)
    assert st["est_bytes"] == (245 + 180) * 40_000 and st["free_bytes"] > 0
    from app.services import svc

    assert svc.deezer.searched == 0  # nothing is matched against the catalog before the user starts


async def test_economy_import_likes_and_playlists_in_order(env):
    from app.db import session_scope
    from app.jobs.downloads import imports_done
    from app.models import DownloadItem, Import

    c, state = env["client"], env["state"]
    imp = await prepared(c)
    r = await c.post(f"/api/imports/{imp['id']}/start",
                     json={"collection_ids": [x["id"] for x in imp["collections"]], "mode": "economy"})
    assert r.status_code == 200, r.text
    await run_imports()
    imp = (await c.get(f"/api/imports/{imp['id']}")).json()
    assert imp["status"] == "done" and not imp["has_token"]  # nothing to download: done; the sign-in is gone

    rows = {(x["collection_id"], x["title"]): x for x in await items(c, imp)}
    likes, playlist = imp["collections"][0]["id"], imp["collections"][1]["id"]
    assert (rows[likes, "Группа крови"]["status"], rows[likes, "Группа крови"]["match"]) == ("waiting", "exact")
    assert rows[likes, "Группа крови"]["ref"] == "dz:100"
    assert (rows[playlist, "Нет такого"]["status"], rows[playlist, "Нет такого"]["match"]) == ("not_found", "not_found")
    assert rows[playlist, "Город золотой"]["nd_song_id"] == "ndA"
    assert rows[imp["collections"][4]["id"], "Что-то"]["status"] == "skipped"  # a dislike is only kept

    # the playlist holds what the library has, in Yandex's order; ♥ — again one by one from the oldest like
    assert state["playlists"] == {"pl1": {"name": "Дорога", "songs": ["ndA", "ndB"]}}
    assert state["star_log"][-2:] == [("star", ["ndA"]), ("star", ["ndB"])]
    assert state["stars"] == ["ndA", "ndB"]
    # the liked track the library lacks shows ♥ by its catalog ref
    stars = (await c.get("/api/starred")).json()
    assert {k: stars[k] for k in ("song_ids", "album_ids", "artist_ids", "refs")} == {
        "song_ids": ["ndA", "ndB"], "album_ids": [], "artist_ids": [], "refs": ["dz:100"]}
    assert len(stars["subjects"]) == 3 and all(x["provenance"] == "import" for x in stars["subjects"])
    assert await jobs("download_item") == []

    # listened to: downloaded now (the user's own work), quietly, ♥ by the download. The app's `start` of it
    # counts (it may play the file from its precache, which asked the stream as `X-HomeMusic-Prefetch`)
    r = await c.post("/api/plays/events", json={"events": [{
        "event_id": "s1", "ts": "2026-10-23T10:00:00Z", "kind": "start", "ref": "dz:100", "client": "app/1.6.0"}]})
    assert r.json()["accepted"] == 1
    [job] = await jobs("download_item")
    assert job.priority == 0
    [dl] = (await c.get("/api/downloads")).json()
    assert (dl["star"], dl["import_id"], dl["track_ref"]) == (True, imp["id"], "dz:100")
    assert {x["status"] for x in await items(c, imp) if x["title"] == "Группа крови" and x["kind"] == "track"} == {"queued"}

    # the download is over: the playlist is rewritten in order with it
    track_id = await add_library_track(KROVI, "ndC")
    async with session_scope() as s:
        item = await s.get(DownloadItem, dl["id"])
        item.status, item.track_id = "done", track_id
        await imports_done(s, [item])
    await run_imports()
    assert state["playlists"]["pl1"]["songs"] == ["ndC", "ndA", "ndB"]
    assert {x["status"] for x in await items(c, imp) if x["title"] == "Группа крови" and x["kind"] == "track"} == {"done"}
    # ♥ in Yandex's order: Группа крови was liked after Город золотой and before Осень, so Осень is starred again after it
    assert state["star_log"][-3:] == [("unstar", ["ndC", "ndB"]), ("star", ["ndC"]), ("star", ["ndB"])]
    assert state["stars"] == ["ndA", "ndC", "ndB"]

    # the user takes the ♥ off Город золотой in the app: the import leaves it alone from then on
    from app.imports.apply import sync_likes

    state["stars"].remove("ndA")
    async with session_scope() as s:
        await sync_likes(s, await s.get(Import, imp["id"]))
    assert state["stars"] == ["ndC", "ndB"]
    assert {x["status"] for x in await items(c, imp, collection_id=likes) if x["title"] == "Город золотой"} == {"skipped"}


async def test_likes_are_downloaded_from_the_oldest(env):
    from app.catalog.models import CatalogTrack
    from app.imports.sources.base import SnapCollection, SnapItem, Snapshot
    from app.services import svc

    svc.deezer.more = [CatalogTrack(ref="dz:300", source="dz", id="300", title="Звезда по имени Солнце",
                                    artist="Кино", duration_s=225)]
    env["state"]["snapshot"] = Snapshot(account="ivan", collections=[SnapCollection("likes", "likes", "Мне нравится", [
        SnapItem("5", track("Кино", "Звезда по имени Солнце", 225)), SnapItem("1", KROVI)])])
    c = env["client"]
    imp = await prepared(c)
    await c.post(f"/api/imports/{imp['id']}/start", json={"collection_ids": [imp["collections"][0]["id"]], "mode": "full"})
    await run_imports()
    # a downloaded like lands on top of «Понравившиеся»: so the oldest goes first
    assert [d["title"] for d in reversed((await c.get("/api/downloads")).json())] == ["Группа крови", "Звезда по имени Солнце"]


async def test_full_import_downloads_in_the_background(env):
    c = env["client"]
    imp = await prepared(c)
    r = await c.post(f"/api/imports/{imp['id']}/start", json={
        "collection_ids": [x["id"] for x in imp["collections"]], "mode": "full", "keep_token": True})
    assert r.status_code == 200, r.text
    await run_imports()
    imp = (await c.get(f"/api/imports/{imp['id']}")).json()
    assert imp["status"] == "running" and imp["has_token"]  # waits for the download; the sign-in is kept

    # Группа крови is in likes and in the playlist: one download, below the user's work, not shown as a download
    [job] = await jobs("download_item")
    assert job.priority < 0
    [dl] = (await c.get("/api/downloads")).json()
    assert dl["star"] and dl["import_id"] == imp["id"]
    assert {x["download_item_id"] for x in await items(c, imp) if x["title"] == "Группа крови" and x["kind"] == "track"} == {dl["id"]}
    assert imp["stats"]["downloading"] == 1
    # the liked album is only matched (download_albums is off), the playlist exists already
    [album] = await items(c, imp, collection_id=imp["collections"][2]["id"])
    assert (album["status"], album["ref"]) == ("waiting", "dz:10")

    # the user asks for the same track: no second download, it goes first in the queue and shows as a download
    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"]})
    assert r.status_code == 200, r.text
    [job] = await jobs("download_item")
    assert job.priority == 0
    [dl] = (await c.get("/api/downloads")).json()
    assert not dl["star"]


async def test_second_import_reuses_the_matches(env):  # two whole imports: ~2x a scenario
    from app.services import svc

    c = env["client"]
    searched = []
    for _ in range(2):
        imp = await prepared(c)
        await c.post(f"/api/imports/{imp['id']}/start",
                     json={"collection_ids": [x["id"] for x in imp["collections"]]})
        await run_imports()
        searched.append(svc.deezer.searched)
    assert searched[0] > 0 and searched[1] == searched[0]  # the next import of the same songs does not search again
    assert len((await c.get("/api/imports")).json()) == 2


async def test_not_found_track_is_picked_by_hand(env):
    c = env["client"]
    imp = await prepared(c)
    await c.post(f"/api/imports/{imp['id']}/start", json={"collection_ids": [imp["collections"][1]["id"]]})
    await run_imports()
    [row] = await items(c, imp, match="not_found")
    assert row["title"] == "Нет такого"
    r = await c.post(f"/api/imports/items/{row['id']}/pick")
    assert r.status_code == 200, r.text
    dl = (await c.get(f"/api/downloads/{r.json()['download_item_id']}")).json()
    assert (dl["title"], dl["star"], dl["import_id"]) == ("Нет такого", False, imp["id"])  # a visible download
    assert (await jobs("download_item"))[0].priority == 0


async def test_unliking_a_waiting_track(env):
    c = env["client"]
    imp = await prepared(c)
    await c.post(f"/api/imports/{imp['id']}/start", json={"collection_ids": [imp["collections"][0]["id"]]})
    await run_imports()
    assert (await c.get("/api/starred")).json()["refs"] == ["dz:100"]
    r = await c.post("/api/star", json={"refs": ["dz:100"], "unstar": True})
    assert r.status_code == 200, r.text
    assert (await c.get("/api/starred")).json()["refs"] == []


async def test_sign_in_by_code(env):
    c, mock = env["client"], env["mock"]
    mock.post("https://oauth.yandex.ru/device/code").mock(return_value=httpx.Response(200, json={
        "device_code": "dc", "user_code": "abcd1234", "verification_url": "https://ya.ru/device", "interval": 5,
        "expires_in": 300}))
    token = mock.post("https://oauth.yandex.ru/token")
    token.mock(return_value=httpx.Response(400, json={"error": "authorization_pending"}))
    r = await c.post("/api/imports", json={"method": "device"})
    assert r.status_code == 200, r.text
    imp = r.json()
    assert imp["status"] == "auth" and imp["auth"]["user_code"] == "abcd1234"
    assert set(imp["auth"]) == {"user_code", "verification_url", "expires_at"}  # the device code stays in the Hub
    await run_imports()
    assert (await c.get(f"/api/imports/{imp['id']}")).json()["status"] == "auth"

    token.mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 31536000}))
    await run_imports()
    imp = (await c.get(f"/api/imports/{imp['id']}")).json()
    assert imp["status"] == "preview" and imp["auth"] is None and imp["has_token"]
    assert env["state"]["fetches"] == [("tok", None)]


async def test_profile_link(env):
    c = env["client"]
    r = await c.post("/api/imports", json={"method": "profile", "url": "https://music.yandex.ru/album/1"})
    assert r.status_code == 400 and "профиль" in r.json()["detail"]
    r = await c.post("/api/imports", json={"method": "profile",
                                           "url": "https://music.yandex.ru/users/ivan/playlists/3"})
    assert r.status_code == 200, r.text
    await run_imports()
    assert env["state"]["fetches"] == [(None, "ivan")]


async def test_matching_verdicts(env):
    from app.catalog.models import CatalogTrack
    from app.db import session_scope
    from app.imports.resolve import CatalogDown, resolve_track
    from app.services import svc

    svc.deezer.more = [CatalogTrack(ref="dz:300", source="dz", id="300", title="Звезда по имени Солнце",
                                    artist="Кино", duration_s=225)]
    svc.providers["hitmoz"].cands = [cand("55", "https://cdn.test/x.mp3", title="Последний герой")]
    cases = {
        "exact": track("Кино", "Группа крови", 246),
        "probable": track("Кино", "Звезда по имени Солнце", 255),  # 30 s longer than the catalog's
        "owned": GOROD,
        "hitmoz": track("Кино", "Последний герой", 245),  # Cyrillic, Deezer lacks it
        "nothing": track("Кто-то", "Ничего общего", 100),
    }
    async with session_scope() as s:
        got = {k: await resolve_track(s, "yandex", k, raw) for k, raw in cases.items()}
    assert (got["exact"].match, got["exact"].ref) == ("exact", "dz:100")
    assert (got["probable"].match, got["probable"].ref) == ("probable", "dz:300")
    assert got["owned"].match == "owned" and got["owned"].track is not None
    assert (got["hitmoz"].match, got["hitmoz"].ref) == ("probable", "hm:55")
    assert got["nothing"].match == "not_found"

    svc.deezer.down = True
    async with session_scope() as s:
        assert (await resolve_track(s, "yandex", "exact", cases["exact"])).ref == "dz:100"  # remembered
        with pytest.raises(CatalogDown):
            await resolve_track(s, "yandex", "new", track("Кино", "Кукушка"))


@pytest.mark.parametrize(("starred", "want", "again"), [
    ([], ["a", "b"], ["a", "b"]),
    (["a", "b"], ["a", "b", "c"], ["c"]),
    (["a", "c"], ["a", "b", "c"], ["b", "c"]),
    (["a", "b"], ["a", "b"], []),
    (["b", "a"], ["a", "b"], ["a", "b"]),
], ids=["first", "newer-like", "older-like", "nothing-new", "reordered"])
def test_restar_plan(starred, want, again):
    from app.imports.apply import restar_plan

    assert restar_plan(starred, want) == again


def test_playlist_and_like_order():
    from app.imports.apply import like_order, playlist_order
    from app.models import ImportItem

    rows = [ImportItem(id=i, position=pos, status=st) for i, pos, st in
            [(1, 2, "done"), (2, 0, "done"), (3, 1, "waiting"), (4, 3, "done"), (5, 4, "skipped")]]
    songs = {1: "c", 2: "a", 4: "a", 5: "e"}  # 3 is not downloaded yet; 2 and 4 are the same song
    assert playlist_order(rows, songs) == ["a", "c", "a"]  # the service's order, a repeat kept, a removed row not
    assert like_order(rows, songs) == ["a", "c"]  # oldest like (the last position) first, each song once


async def test_dislikes_of_the_service_become_the_users_dislikes(env):
    from app.imports.sources.base import SnapCollection, SnapItem, Snapshot

    c, state = env["client"], env["state"]
    state["snapshot"] = Snapshot(account="ivan", collections=[SnapCollection("dislikes", "dislikes", "Не нравится", [
        SnapItem("1", KROVI), SnapItem("3", GOROD), SnapItem("9", NONE)])])
    imp = await prepared(c)
    r = await c.post(f"/api/imports/{imp['id']}/start",
                     json={"collection_ids": [x["id"] for x in imp["collections"]], "mode": "full"})
    assert r.status_code == 200, r.text
    await run_imports()
    # a catalog track by its ref, an owned one by its Navidrome song too; one not found is no dislike
    rows = {x["title"]: x for x in (await c.get("/api/dislikes")).json()}
    assert sorted(rows) == ["Город золотой", "Группа крови"]
    assert (rows["Группа крови"]["ref"], rows["Группа крови"]["source"]) == ("dz:100", "yandex_import")
    assert (rows["Город золотой"]["nd_song_id"], rows["Город золотой"]["source"]) == ("ndA", "yandex_import")
    assert await jobs("download_item") == [] and state["stars"] == []  # still never downloaded nor ♥
