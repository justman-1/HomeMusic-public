"""Read playlists and saved music from a Spotify account-data export."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from app.imports.sources.base import SnapCollection, SnapItem, Snapshot, SourceError

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 32
MAX_ITEMS = 10_000
_URI = re.compile(r"spotify:(track|album|artist):([A-Za-z0-9]{1,64})")


def _rows(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        yield from (row for row in value if isinstance(row, dict))


def _text(row: dict, *fields: str) -> str:
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    return ""


def _track(row: dict) -> SnapItem | None:
    uri = _URI.fullmatch(_text(row, "trackUri", "uri"))
    artist, title = _text(row, "artistName", "artist"), _text(row, "trackName", "track", "name")
    if not uri or uri[1] != "track" or not artist or not title:
        return None
    return SnapItem(uri[2], {"artist": artist, "title": title, "album": _text(row, "albumName", "album")})


def _saved(row: dict, kind: str) -> SnapItem | None:
    uri = _URI.fullmatch(_text(row, "uri"))
    name = _text(row, "album", "name") if kind == "album" else _text(row, "artist", "name")
    if not uri or uri[1] != kind or not name:
        return None
    artist = _text(row, "artist")
    if kind == "album" and not artist:
        return None
    return SnapItem(uri[2], {"artist": artist, "title": name} if kind == "album" else {"name": name})


def _unique(items: Iterable[SnapItem | None]) -> list[SnapItem]:
    found: dict[str, SnapItem] = {}
    for item in items:
        if item:
            found.setdefault(item.external_id, item)
    return list(found.values())


def parse(files: list[bytes]) -> Snapshot:
    playlists: list[SnapCollection] = []
    likes: list[SnapItem] = []
    albums: list[SnapItem] = []
    artists: list[SnapItem] = []
    for file in files:
        try:
            data = json.loads(file)
        except (ValueError, UnicodeError, RecursionError) as e:
            raise SourceError("Не удалось прочитать JSON Spotify") from e
        if not isinstance(data, dict) or not any(isinstance(data.get(k), list) for k in
                                                 ("playlists", "tracks", "albums", "artists")):
            raise SourceError("Выберите Playlist*.json и/или YourLibrary.json из экспорта Spotify")
        for row in _rows(data.get("playlists")):
            title = _text(row, "name") or "Плейлист Spotify"
            items = _unique(_track(item["track"]) for item in _rows(row.get("items"))
                            if isinstance(item.get("track"), dict))
            if items:
                key = hashlib.sha256(f"{len(playlists)}:{title}".encode()).hexdigest()[:24]
                playlists.append(SnapCollection("playlist", f"playlist:{key}", title, items))
        likes.extend(_unique(_track(row) for row in _rows(data.get("tracks"))))
        albums.extend(_unique(_saved(row, "album") for row in _rows(data.get("albums"))))
        artists.extend(_unique(_saved(row, "artist") for row in _rows(data.get("artists"))))
    collections = [
        SnapCollection("likes", "likes", "Понравившиеся в Spotify", _unique(likes)),
        *playlists,
        SnapCollection("albums", "albums", "Сохранённые альбомы", _unique(albums)),
        SnapCollection("artists", "artists", "Любимые исполнители", _unique(artists)),
    ]
    collections = [c for c in collections if c.items]
    if not collections:
        raise SourceError("В экспорте Spotify не найдено музыки")
    if sum(len(c.items) for c in collections) > MAX_ITEMS:
        raise SourceError(f"Выберите файлы не более чем с {MAX_ITEMS} музыкальными записями за раз")
    return Snapshot(account=None, collections=collections)
