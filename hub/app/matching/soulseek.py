"""Interpret Soulseek paths using the requested track as evidence, without trusting the peer's tags."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from unidecode import unidecode

from app.matching.normalize import (
    Norm,
    _prep,
    explicit_marker,
    has_cyrillic,
    normalize_artist,
    normalize_title,
    split_version,
    version_kind,
)
from app.matching.score import artist_sim, title_sim
from app.providers.base import TrackQuery

_TRACK_NUMBER = re.compile(r"^(?:(\d{1,3})(?:-\d{1,3})?\s*[-._)]\s*|(0\d)\s+)(?=\D)")
_SEPARATOR = re.compile(r"[\s._-]+")
_QUALITY = r"(?:flac|mp3|lossless|(?:16|24|32)\s*bit|\d{2,3}(?:[.,]\d)?\s*k(?:hz|bps))"
_QUALITY_SUFFIX = re.compile(
    rf"\s*[\[(]\s*(?:{_QUALITY}(?:[\s/,;-]+{_QUALITY})*|(?:16|24|32)[-_/](?:44[.,]1|48|88[.,]2|96|192))\s*[\])]\s*$",
    re.I,
)
_VERSION_SUFFIX = re.compile(
    r"\b(?:live|concert|лайв|концерт|demo|демо|remix|ремикс|acoustic|акустика|instrumental|"
    r"инструментал|karaoke|караоке|cover|кавер|remaster(?:ed)?(?:\s+\d{4})?|радио\s+версия|radio\s+edit|"
    r"slowed|sped\s+up|nightcore|extended|clean|explicit)$", re.I,
)


@dataclass(frozen=True)
class FileIdentity:
    artist: str
    title: str
    confidence: float
    explicit: bool | None = None


def remote_parts(filename: str) -> list[str]:
    return re.split(r"[/\\]", filename)


def _text(value: str) -> str:
    value = re.sub(r"(?<=\w)\.(?=\w)", " ", _prep(value)).replace("_", " ")
    return " ".join(value.split())


def _stem(filename: str, query: TrackQuery) -> str:
    stem = _prep(PurePosixPath(filename).stem)
    if prefix := _TRACK_NUMBER.match(stem):
        number = prefix[1] or prefix[2]
        if not any(re.match(rf"{number}\b", field.strip()) for field in (query.artist, query.title)):
            stem = stem[prefix.end():]
    while (clean := _QUALITY_SUFFIX.sub("", stem)) != stem:
        stem = clean
    return stem.strip()


def _translit_key(value: str) -> str:
    # Common informal spellings, applied only across scripts and compared as whole strings.
    value = re.sub(r"ia|ya", "ya", value)
    value = re.sub(r"iu|yu", "yu", value)
    value = re.sub(r"(?:ts|c)(?=[aeiouy])", "ts", value)
    return re.sub(r"[iy]\b", "y", value.replace("w", "v"))


def _similarity(a: Norm, b: Norm, *, artist: bool = False) -> float:
    sim = (artist_sim if artist else title_sim)(a, b)
    if len(a.latin) >= 4 and a.latin.replace(" ", "") == b.latin.replace(" ", ""):
        return 1.0
    if (len(a.latin) >= 4 and has_cyrillic(a.text) != has_cyrillic(b.text)
            and _translit_key(a.latin) == _translit_key(b.latin)):
        return 1.0
    return sim


def _plain_version(stem: str, query: TrackQuery) -> str:
    # Version words in bracketed and spaced-dash suffixes are already understood by the shared matcher.
    stem = _text(stem)
    if split_version(stem)[1]:
        return stem
    spaced = stem.replace("-", " ")
    match = _VERSION_SUFFIX.search(spaced)
    if not match or match.start() == 0:
        return stem
    suffix = " ".join(match.group().split())
    if normalize_title(query.title).text.endswith(suffix.casefold()) and not query.version:
        return stem  # e.g. a song actually named "We Live"
    return f"{stem[:match.start()].rstrip(' -._')} ({suffix})"


def identify(filename: str, query: TrackQuery, *, artist_min: float) -> FileIdentity | None:
    parts = remote_parts(filename)
    stem = _stem(parts[-1], query)
    if not stem:
        return None
    if not query.title:
        fields = re.split(r"\s+-\s+", _text(stem), maxsplit=1)
        if len(fields) == 2:
            return FileIdentity(*fields, 0.0)
        return FileIdentity("", _text(stem), 0.0)

    stem = _plain_version(stem, query)
    file_explicit = explicit_marker(stem)
    # Pull trailing qualifiers out before testing both field orders. A live suffix can follow the artist.
    body, version = split_version(stem)
    explicit_kind = version_kind(query.version or normalize_title(query.title).version)
    if (file_kind := version_kind(version)) and file_kind != explicit_kind:
        return None

    qa, qt = normalize_artist(query.artist), normalize_title(query.title)
    title_min = 0.88 if len(qt.latin.replace(" ", "")) >= 8 else 1.0
    pairs = []
    for boundary in list(_SEPARATOR.finditer(body))[:32]:
        left, right = body[:boundary.start()], body[boundary.end():]
        pairs.extend(((left, right, title_min), (right, left, title_min)))
    # A folder cannot override an explicit credit unless the whole basename matches the requested title.
    folder_min = 1.0 if " - " in body else title_min
    for folder in parts[-5:-1]:
        folder = _text(folder)
        # A directory can be the artist, or an explicitly separated "artist - album" folder.
        pairs.append((folder, body, folder_min))
        left, dash, right = folder.partition(" - ")
        if dash:
            pairs.extend(((left, body, folder_min), (right, body, folder_min)))

    best = None
    for raw_artist, raw_title, minimum in pairs:
        raw_artist = _text(raw_artist)
        artist_ratio = _similarity(qa, normalize_artist(raw_artist), artist=True)
        if artist_ratio < artist_min:
            continue
        raw_title = _plain_version(raw_title, query)
        explicit = file_explicit if file_explicit is not None else explicit_marker(raw_title)
        if explicit is not None and query.explicit is not None and explicit != query.explicit:
            continue
        raw_title, local_version = split_version(raw_title)
        versions = " ".join(filter(None, (version, local_version)))
        if version_kind(versions) != explicit_kind:
            continue
        title_ratio = _similarity(qt, normalize_title(raw_title))
        if title_ratio < minimum and (plain_title := _TRACK_NUMBER.sub("", raw_title)) != raw_title:
            raw_title = plain_title
            title_ratio = _similarity(qt, normalize_title(raw_title))
        if title_ratio < minimum:
            continue
        confidence = 0.4 * artist_ratio + 0.6 * title_ratio
        if best is None or confidence > best.confidence:
            artist = query.artist if artist_ratio == 1 else raw_artist
            title = split_version(query.title)[0] if title_ratio == 1 else raw_title
            if versions:
                title = f"{title} ({versions})"
            best = FileIdentity(artist, title, confidence, explicit)
    return best


def search_terms(query: TrackQuery) -> list[str]:
    def roman(name: Norm) -> str:
        return name.latin if has_cyrillic(name.text) else unidecode(name.text)

    if not query.title:
        raw = normalize_title(_text(query.artist))
        return list(dict.fromkeys(term for term in (raw.text, roman(raw)) if term))
    artist, title = normalize_artist(query.artist), normalize_title(query.title)
    terms = [f"{artist.text} {title.text}", f"{roman(artist)} {roman(title)}"]
    for words, name in ((title.text.split(), artist.text), (roman(title).split(), roman(artist))):
        anchors = [word for word in words if len(word) >= 4]
        if len(words) > 1 and anchors:
            # A shorter network query can find a misspelled title. Full title/artist checks still apply locally.
            terms.append(f"{name} {anchors[-1]}")
        elif words and len(words[0]) >= 8:
            terms.append(words[0])
    return list(dict.fromkeys(term.strip() for term in terms if term.strip()))


def split_search_query(query: TrackQuery) -> TrackQuery:
    if not query.title:
        artist, _, title = _prep(query.artist).partition(" - ")
        if artist and title:
            return query.model_copy(update={"artist": artist, "title": title})
    return query
