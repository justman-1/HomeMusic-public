"""«Чарты и жанры» (app/charts.py): the sources, the kept copy, the API and a wave by a chart."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import respx

from app import charts
from app.catalog.models import CatalogTrack
from app.mixes import build, wave
from app.mixes.sources import Hit
from tests.support import FakeDeezer, app_client
from tests.test_mixes import ND, user_id


def tr(n: int, artist: str | None = None, title: str | None = None) -> CatalogTrack:
    a = artist or f"Артист {n}"
    return CatalogTrack(ref=f"dz:{n}", source="dz", id=str(n), title=title or f"Песня {n}", artist=a, artists=[a],
                        artist_ref=f"dz:a-{a}", duration_s=200, cover_url=f"https://cdn.test/{n}.jpg")


class Deezer(FakeDeezer):
    def __init__(self) -> None:
        super().__init__()
        self.playlists: dict[int, list[CatalogTrack]] = {}
        self.genres: dict[int, list[CatalogTrack]] = {}
        self.asked: list[str] = []

    async def playlist_tracks(self, id_, limit=100):
        self.asked.append(f"playlist/{id_}")
        if id_ not in self.playlists:
            raise RuntimeError("Deezer: 500")
        return self.playlists[id_][:limit]

    async def chart_tracks(self, genre_id=0, limit=100):
        self.asked.append(f"chart/{genre_id}")
        return self.genres.get(genre_id, [])[:limit]


@pytest.fixture
async def env(hub_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.main import create_app
    from app.services import svc

    app = create_app()
    async with app.router.lifespan_context(app):
        svc.deezer = dz = Deezer()
        with respx.mock(assert_all_called=False) as mock:
            nd = ND()
            mock.route(host="navidrome.test").mock(side_effect=nd.handler)
            async with app_client(app) as client:
                yield {"client": client, "deezer": dz, "admin": await user_id("admin")}
        await charts.reset()


def test_europe_takes_the_first_places_by_turns_and_a_song_once() -> None:
    gb = [tr(1), tr(2), tr(3)]
    de = [tr(2), tr(4), tr(5, "Артист 1", "Песня 1")]  # dz:2 again; dz:5 is dz:1 by name

    assert [t.ref for t in charts.by_turns([gb, de])] == ["dz:1", "dz:2", "dz:4", "dz:3"]
    assert [t.ref for t in charts.by_turns([gb, de], size=2)] == ["dz:1", "dz:2"]


async def test_a_country_chart_is_kept_and_served_with_its_places(env) -> None:
    dz: Deezer = env["deezer"]
    dz.playlists[1313621735] = [tr(n) for n in range(1, 6)]

    built = await charts.tracks("us")
    assert [t.ref for t in built.tracks] == ["dz:1", "dz:2", "dz:3", "dz:4", "dz:5"]
    assert built.positions == [1, 2, 3, 4, 5]
    await charts.tracks("us")
    assert dz.asked == ["playlist/1313621735"]  # the kept copy answers


async def test_an_old_chart_is_served_while_a_new_one_is_made(env) -> None:
    dz: Deezer = env["deezer"]
    dz.playlists[3155776842] = [tr(1)]
    await charts.tracks("world")
    built = await charts.cached("world")
    assert built is not None
    await charts.cache_set(charts._key("world"), {"at": time.time() - charts.FRESH_S - 1,
                                                 "tracks": [t.model_dump(mode="json") for t in built.tracks]},
                           charts.KEEP_S)
    dz.playlists[3155776842] = [tr(7), tr(8)]

    assert [t.ref for t in (await charts.tracks("world")).tracks] == ["dz:1"]  # the old one at once
    await charts._later["world"]
    assert [t.ref for t in (await charts.tracks("world")).tracks] == ["dz:7", "dz:8"]


async def test_russia_is_yandex_chart_and_apple_music_when_yandex_fails(env, monkeypatch) -> None:
    resolved: list[tuple[str, list[str]]] = []

    async def resolve(source: str, hits: list[Hit]) -> list[CatalogTrack]:
        resolved.append((source, [h.title for h in hits]))
        return [tr(100 + i, h.artist, h.title) for i, h in enumerate(hits)]

    def yandex_down(name: str) -> list[Hit]:
        raise RuntimeError("451 Unavailable For Legal Reasons")

    async def apple(country: str) -> list[Hit]:
        assert country == "ru"
        return [Hit("МАЯК X", "Украсть тебя", source_id="apple:1"), Hit("VILLIAN", "ДИНАСТИЯ", source_id="apple:2")]

    monkeypatch.setattr(charts, "_resolve", resolve)
    monkeypatch.setattr(charts, "_yandex_chart_sync", lambda name: [Hit("Koss", "Земляк", source_id="1")])
    assert [t.title for t in (await charts.tracks("ru")).tracks] == ["Земляк"]

    await charts.cache_set(charts._key("ru"), {}, 1)  # nothing kept
    monkeypatch.setattr(charts, "_yandex_chart_sync", yandex_down)
    monkeypatch.setattr(charts, "_apple", apple)
    assert [t.title for t in (await charts.tracks("ru")).tracks] == ["Украсть тебя", "ДИНАСТИЯ"]
    assert [s for s, _ in resolved] == ["yandex", "apple"]


async def test_the_api_lists_the_charts_and_gives_a_chart_with_its_places(env) -> None:
    c, dz = env["client"], env["deezer"]
    dz.genres[152] = [tr(n) for n in range(1, 4)]

    r = await c.get("/api/charts/rock")
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["chart"]["name"] == "Рок" and page["chart"]["source"] == "Deezer"
    assert [(t["ref"], t["position"]) for t in page["tracks"]] == [("dz:1", 1), ("dz:2", 2), ("dz:3", 3)]
    assert "owned" in page["tracks"][0]

    listed = (await c.get("/api/charts")).json()["charts"]
    assert [x["id"] for x in listed][:4] == ["ru", "us", "eu", "world"]
    rock = next(x for x in listed if x["id"] == "rock")
    assert rock["group"] == "genres" and rock["covers"] == ["https://cdn.test/1.jpg", "https://cdn.test/2.jpg",
                                                            "https://cdn.test/3.jpg"]
    assert next(x for x in listed if x["id"] == "jazz")["covers"] == []  # not made yet

    assert (await c.get("/api/charts/nope")).status_code == 404
    r = await c.get("/api/charts/de")  # Deezer answers 500, nothing kept
    assert r.status_code == 502 and "Чарт недоступен" in r.json()["detail"]


async def test_a_wave_by_a_chart_starts_from_its_top_places(env, monkeypatch) -> None:
    dz: Deezer = env["deezer"]
    dz.playlists[1313621735] = [tr(n) for n in range(1, 31)]
    radios: list[list[str]] = []

    def gens(p, seeds) -> dict[str, Any]:
        radios.append([s.ref for s in seeds])

        async def nothing() -> list[CatalogTrack]:
            return []

        return {"ytm_radio": nothing, "yandex_similar": nothing}

    monkeypatch.setattr(build, "generators", gens)
    r = await env["client"].post("/api/wave/start", json={"seed": {"chart": "us"}, "origin": "chart_page"})
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["title"] == "Волна: Чарт США" and w["seed"] == {"chart": "us", "name": "Чарт США"}
    assert len(w["tracks"]) == 5 and {t["generator"] for t in w["tracks"]} == {wave.CHART}
    assert {t["ref"] for t in w["tracks"]} <= {f"dz:{n}" for n in range(1, 11)}  # the top places first
    assert radios == [["dz:1", "dz:2", "dz:3"]]

    r = await env["client"].post("/api/wave/start", json={"seed": {"chart": "nope"}})
    assert r.status_code == 404
