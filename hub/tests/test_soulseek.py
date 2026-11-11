from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.config import MatchingCfg, SoulseekCfg
from app.matching.select import rank_candidates
from app.matching.soulseek import identify
from app.providers.base import ProviderError, TrackQuery
from app.providers.soulseek import SoulseekProvider

QUERY = TrackQuery(artist="Кино", title="Группа крови", duration_s=240)
FILENAME = r"@@music\1988 - Kino\01_Gruppa-Krovi--Kino [FLAC 24bit 96kHz].flac"


@pytest.mark.parametrize(("filename", "artist", "title"), [
    (FILENAME, "Кино", "Группа крови"),
    (r"Music\Kino\Album\1-01 - Gruppa.Krovi.flac", "Кино", "Группа крови"),
    (r"Music/Кино—Группа_крови.MP3", "Кино", "Группа крови"),
    (r"Music/Tsoi-Pachka-Sigaret.mp3", "Цой", "Пачка сигарет"),
    (r"Music/50 Cent - In Da Club.flac", "50 Cent", "In Da Club"),
    (r"Music/2-Chainz - 06 - 4 AM [24-96].flac", "2 Chainz", "4 AM"),
    (r"Music/02 - In the End - Linkin Park.mp3", "Linkin Park", "In the End"),
])
def test_filename_identity(filename, artist, title):
    identity = identify(filename, TrackQuery(artist=artist, title=title), artist_min=0.9)
    assert identity is not None
    assert (identity.artist, identity.title, identity.confidence) == (artist, title, 1)


@pytest.mark.parametrize("filename", [
    r"Other\01 Gruppa Krovi.flac",
    r"Kino\01 Zvezda po imeni Solntse.flac",
    r"Kino\01 Gruppa Krovi (Cover).flac",
    r"Kino\01 Gruppa Krovi--Kino--Live.flac",
    r"Kino\01 Gruppa Krovi_Kino_Remix.flac",
])
def test_filename_rejects_wrong_recording(filename):
    assert identify(filename, QUERY, artist_min=0.9) is None


def test_requested_live_version_and_fuzzy_title():
    live = identify(r"Kino\01 Grupa-Krovi--Live.mp3", QUERY.model_copy(update={"version": "live"}), artist_min=0.9)
    assert live and live.title == "Grupa-Krovi (live)" and live.confidence < 1
    reversed_live = r"Music\Gruppa-Krovi-Live-Kino.mp3"
    live = identify(reversed_live, QUERY.model_copy(update={"version": "live"}), artist_min=0.9)
    assert live and live.title == "Группа крови (live)"
    assert identify(reversed_live, QUERY, artist_min=0.9) is None
    assert identify(r"Kino\01 Gruppa Krovi.mp3", QUERY.model_copy(update={"version": "live"}), artist_min=0.9) is None


def test_artist_directory_does_not_override_filename_credit():
    query = TrackQuery(artist="Kino", title="A Long Song With Several Words")
    assert identify(r"Music\Kino\Other - A Long Song With Several Words.flac", query, artist_min=0.9) is None


async def test_search_variants_rank_quality_and_clean_up(tmp_path):
    requests = []
    valid = {"filename": FILENAME, "size": 50_000, "bitDepth": 24, "sampleRate": 96_000, "length": 240}

    def handler(request):
        assert request.headers["x-api-key"] == "test-key"
        route = request.url.path.removeprefix("/api/v0/")
        if request.method == "POST":
            body = json.loads(request.content)
            requests.append((body["id"], body["searchText"]))
            return httpx.Response(200, json={"id": body["id"], "isComplete": False})
        assert route.split("/")[1] == requests[-1][0]
        if request.method == "DELETE":
            requests.append(("deleted", route.split("/")[1]))
            return httpx.Response(204)
        if route.endswith("/responses"):
            files = [valid, valid, {**valid, "filename": "other/" + FILENAME, "length": 300},
                     {**valid, "filename": "another/" + FILENAME, "bitDepth": 16},
                     {**valid, "filename": FILENAME.replace(".flac", ".mp3"), "bitRate": 320},
                     {**valid, "filename": "../" + FILENAME}]
            return httpx.Response(200, json=[{"username": "peer", "files": files}] if len(requests) > 1 else [])
        return httpx.Response(200, json={"isComplete": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        matching = MatchingCfg()
        cfg = SoulseekCfg(format="flac", min_flac_bit_depth=24, min_flac_sample_rate_hz=96_000)
        provider = SoulseekProvider("http://slskd.test", "test-key", tmp_path, cfg, matching, client)
        found = await provider.search(QUERY)
        assert len(found) == 1 and found[0].extra["filename"] == FILENAME
        assert rank_candidates(QUERY, found, matching)[0].score == 1
        assert requests[0][1] == "кино группа крови" and requests[2][1] == "kino gruppa krovi"
        assert requests[1] == ("deleted", requests[0][0]) and requests[3] == ("deleted", requests[2][0])


async def test_reuses_existing_transfer_and_rejects_forged_quality(tmp_path, monkeypatch):
    from app.providers import soulseek

    monkeypatch.setattr(soulseek, "_POLL_S", 0)
    batch, transfer_id = str(uuid4()), str(uuid4())
    transfer = {"id": transfer_id, "batchId": batch, "filename": FILENAME, "size": 4, "state": "InProgress"}
    calls = []
    directory = tmp_path / "homemusic" / batch
    directory.mkdir(parents=True)
    (directory / "peer's sanitized name.flac").write_bytes(b"test")

    def handler(request):
        assert request.method == "GET"
        calls.append(request.url.path)
        if request.url.path.endswith("/peer"):
            return httpx.Response(200, json={"directories": [{"files": [transfer]}]})
        assert request.url.path.endswith(f"/peer/{transfer_id}")
        return httpx.Response(200, json={**transfer, "state": "Completed, Succeeded"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SoulseekProvider("http://slskd.test", "key", tmp_path, SoulseekCfg(), MatchingCfg(), client)
        found = provider._candidates(QUERY, [{"username": "peer", "files": [{"filename": FILENAME, "size": 4}]}])
        with pytest.raises(ProviderError, match="не соответствует формату"):
            await provider.fetch(found[0], tmp_path)
    assert len(calls) == 2 and not list(tmp_path.glob("soulseek_*.flac"))
