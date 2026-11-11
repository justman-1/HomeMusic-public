"""Soulseek searches and queued transfers through the internal slskd v0 API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from app.config import MatchingCfg, SoulseekCfg
from app.matching.normalize import uncensored_marker
from app.matching.soulseek import identify, remote_parts, search_terms, split_search_query
from app.net import PrioritySemaphore
from app.providers.base import Candidate, ProviderError, ProviderUnavailable, StreamSource, TrackQuery

_BATCHES = "/transfers/downloads/batches"
_POLL_S = 0.5
_HEALTH_TTL_S = 10


def _positive(value: object) -> int:
    return value if type(value) is int and value > 0 else 0


@dataclass(frozen=True)
class RemoteFile:
    peer: str
    filename: str
    size: int
    format: str

    @classmethod
    def parse(cls, peer: object, filename: object, size: object, cfg: SoulseekCfg) -> RemoteFile | None:
        if (not isinstance(peer, str) or not 0 < len(peer) <= 500 or peer in (".", "..")
                or any(ch in peer for ch in "/\\\x00")):
            return None
        if not isinstance(filename, str) or not 0 < len(filename) <= 4096 or "\x00" in filename:
            return None
        parts = remote_parts(filename)
        if any(part in (".", "..") for part in parts):
            return None
        extension = Path(parts[-1]).suffix.lower().lstrip(".")
        size = _positive(size)
        if extension not in ("flac", "mp3") or not 0 < size <= cfg.max_file_mb * 1024**2:
            return None
        if cfg.format != "any" and extension != cfg.format:
            return None
        return cls(peer, filename, size, extension)

    @property
    def id(self) -> str:
        identity = json.dumps([self.peer, self.filename, self.size], ensure_ascii=True)
        return hashlib.sha256(identity.encode()).hexdigest()[:32]

    @property
    def route(self) -> str:
        return f"/transfers/downloads/{quote(self.peer, safe='')}"

    def matches(self, transfer: dict) -> bool:
        return transfer.get("filename") == self.filename and transfer.get("size") == self.size


def _uuid(value: object) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _states(transfer: dict) -> set[str]:
    return {part.strip().casefold() for part in str(transfer.get("state", "")).split(",")}


class SoulseekProvider:
    name = "soulseek"
    streams = False

    def __init__(self, url: str, api_key: str, root: Path, cfg: SoulseekCfg, matching: MatchingCfg,
                 client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.matching = matching
        self.root = root
        self.api = f"{url.rstrip('/')}/api/v0"
        self.headers = {"X-API-Key": api_key}
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(timeout=8, trust_env=False)
        self.search_slots = PrioritySemaphore(4)
        self.transfer_slots = PrioritySemaphore(cfg.parallel_transfers)
        self.search_start = asyncio.Lock()
        self.transfer_start = asyncio.Lock()
        self.file_locks = [asyncio.Lock() for _ in range(32)]
        self.health_lock = asyncio.Lock()
        self.health_cache: tuple[float, str] | None = None

    async def _api(self, method: str, path: str, *, allow_missing: bool = False,
                   request_timeout: float = 8, **kwargs):
        try:
            r = await self.client.request(method, self.api + path, headers=self.headers,
                                          timeout=request_timeout, **kwargs)
        except httpx.RequestError as exc:
            raise ProviderUnavailable("slskd недоступен") from exc
        if allow_missing and r.status_code == 404:
            return None
        if not r.is_success:
            error = ProviderUnavailable if r.status_code in (401, 403, 409, 429) or r.status_code >= 500 else ProviderError
            raise error(f"slskd: HTTP {r.status_code}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as exc:
            raise ProviderError("slskd вернул неверный JSON") from exc

    async def _delete(self, route: str) -> None:
        try:
            await self._api("DELETE", route, allow_missing=True, request_timeout=1)
        except ProviderError:
            pass  # slskd also expires searches and transfers itself

    async def health(self, *, force: bool = False) -> str:
        async with self.health_lock:
            if not force and self.health_cache and monotonic() - self.health_cache[0] < _HEALTH_TTL_S:
                return self.health_cache[1]
            try:
                data = await self._api("GET", "/application", request_timeout=3)
            except ProviderError as e:
                state = "auth_error" if str(e) in ("slskd: HTTP 401", "slskd: HTTP 403") else "unreachable"
            else:
                server = data.get("server") if isinstance(data, dict) else None
                if not isinstance(server, dict) or not isinstance(server.get("isLoggedIn"), bool):
                    state = "unreachable"
                elif server["isLoggedIn"]:
                    state = "ready"
                elif server.get("isConnecting") or server.get("isLoggingIn"):
                    state = "connecting"
                else:
                    state = "disconnected"
            self.health_cache = (monotonic(), state)
            return state

    async def probe_search(self) -> int:
        responses = await self._search("librivox", window_s=min(self.cfg.search_timeout_s, 5))
        return sum(len(r["files"]) for r in responses if isinstance(r, dict) and isinstance(r.get("files"), list))

    async def _search(self, term: str, *, window_s: float | None = None) -> list[dict]:
        window_s = window_s if window_s is not None else self.cfg.search_timeout_s
        route = f"/searches/{uuid4()}"
        try:
            async with self.search_start:
                await self._api("POST", "/searches", json={
                    "id": route.rsplit("/", 1)[-1], "searchText": term,
                    "searchTimeout": round(window_s * 1000),
                    "fileLimit": 500, "responseLimit": 60,
                })
            deadline = asyncio.get_running_loop().time() + window_s + 0.5
            while asyncio.get_running_loop().time() < deadline:
                state = await self._api("GET", route)
                if isinstance(state, dict) and {"errored", "cancelled"} & _states(state):
                    raise ProviderUnavailable("Поиск Soulseek прервался")
                if isinstance(state, dict) and state.get("isComplete"):
                    break
                await asyncio.sleep(_POLL_S)
            responses = await self._api("GET", f"{route}/responses")
            if not isinstance(responses, list):
                raise ProviderError("slskd вернул неверный список результатов")
            return responses
        finally:
            await self._delete(route)

    def _accept_quality(self, remote: RemoteFile, file: dict) -> bool:
        if remote.format == "mp3":
            return _positive(file.get("bitRate")) >= self.cfg.min_mp3_bitrate_kbps
        return (_positive(file.get("bitDepth")) >= self.cfg.min_flac_bit_depth
                and _positive(file.get("sampleRate")) >= self.cfg.min_flac_sample_rate_hz)

    def _candidates(self, query: TrackQuery, responses: list[dict]) -> list[Candidate]:
        found: dict[str, tuple[tuple, Candidate]] = {}
        for response in responses:
            if not isinstance(response, dict) or not isinstance(response.get("files"), list):
                continue
            for file in response["files"]:
                if not isinstance(file, dict) or file.get("isLocked"):
                    continue
                remote = RemoteFile.parse(response.get("username"), file.get("filename"), file.get("size"), self.cfg)
                if remote is None or remote.id in found or not self._accept_quality(remote, file):
                    continue
                duration = _positive(file.get("length")) or None
                if query.duration_s and duration and abs(query.duration_s - duration) > self.matching.duration_tolerance_s:
                    continue
                identity = identify(remote.filename, query, artist_min=self.matching.artist_min_ratio)
                if identity is None:
                    continue
                bit_rate = _positive(file.get("bitRate")) or None
                free_slot = response.get("hasFreeUploadSlot") is True
                queue = response.get("queueLength")
                order = (-identity.confidence, not free_slot, queue if type(queue) is int and queue >= 0 else 10**6,
                         -_positive(response.get("uploadSpeed")))
                found[remote.id] = (order, Candidate(
                    provider=self.name, provider_id=remote.id, artist=identity.artist, title=identity.title,
                    duration_s=duration, bitrate_kbps=bit_rate, explicit=identity.explicit, enriched=True,
                    page_url=f"soulseek://file/{remote.id}",
                    extra={"peer": remote.peer, "filename": remote.filename, "size": remote.size,
                           "format": remote.format, "bit_depth": _positive(file.get("bitDepth")) or None,
                           "sample_rate_hz": _positive(file.get("sampleRate")) or None,
                           "uncensored": uncensored_marker(remote_parts(remote.filename)[-1])},
                ))
        return [candidate for _, candidate in sorted(found.values(), key=lambda pair: pair[0])]

    async def search(self, q: TrackQuery, limit: int = 10) -> list[Candidate]:
        q = split_search_query(q)
        async with self.search_slots:
            for term in search_terms(q):
                if candidates := self._candidates(q, await self._search(term)):
                    return candidates[:limit]
        return []

    async def search_for_library(self, q: TrackQuery, limit: int) -> list[Candidate]:
        """Keep up to a full tier of each permitted format for download fallback."""
        found: dict[str, Candidate] = {}
        async with self.search_slots:
            for term in search_terms(q):
                for c in self._candidates(q, await self._search(term)):
                    found.setdefault(c.provider_id, c)
                formats = {c.extra["format"] for c in found.values()}
                wanted = {"flac", "mp3"} if self.cfg.format == "any" else {self.cfg.format}
                if wanted <= formats:
                    break
        return [c for fmt in ("flac", "mp3")
                for c in [c for c in found.values() if c.extra["format"] == fmt][:limit]]

    async def enrich(self, c: Candidate) -> Candidate:
        return c

    async def stream(self, c: Candidate) -> StreamSource:
        raise ProviderError("Soulseek передаёт файл целиком")

    def _completed(self, remote: RemoteFile, batch: str) -> Path | None:
        root = self.root.resolve()
        directory = root / "homemusic" / batch
        if not directory.is_dir() or directory.resolve() != directory:
            return None
        # slskd sanitizes the basename and may rename it. Each batch contains exactly one requested file.
        files = [p for p in directory.iterdir() if not p.is_symlink() and p.is_file()
                 and p.suffix.lower() == f".{remote.format}" and p.stat().st_size == remote.size]
        return files[0] if len(files) == 1 else None

    async def _previous(self, remote: RemoteFile) -> tuple[str, str | Path] | None:
        history = await self._api("GET", remote.route, allow_missing=True)
        if history is None:
            return None
        if not isinstance(history, dict) or not isinstance(history.get("directories"), list):
            raise ProviderError("slskd вернул неверный список загрузок")
        for directory in history["directories"]:
            if not isinstance(directory, dict) or not isinstance(directory.get("files"), list):
                continue
            for transfer in directory["files"]:
                if not isinstance(transfer, dict) or not remote.matches(transfer):
                    continue
                batch, transfer_id = _uuid(transfer.get("batchId")), _uuid(transfer.get("id"))
                if not batch or not transfer_id:
                    continue
                state = _states(transfer)
                if "succeeded" in state:
                    if path := await asyncio.to_thread(self._completed, remote, batch):
                        return batch, path
                elif "completed" not in state or transfer.get("nextAttemptAt"):
                    return batch, transfer_id
        return None

    async def _enqueue(self, remote: RemoteFile) -> tuple[str, str]:
        batch = str(uuid4())
        async with self.transfer_start:
            try:
                response = await self._api("POST", _BATCHES, json={
                    "id": batch, "username": remote.peer,
                    "files": [{"filename": remote.filename, "size": remote.size}],
                    "options": {"destination": f"homemusic/{batch}"},
                })
            except ProviderUnavailable:
                # A lost POST response can still have created the batch. Recover by the supplied ID.
                response = {"batch": await self._api("GET", f"{_BATCHES}/{batch}", allow_missing=True)}
        record = response.get("batch") if isinstance(response, dict) else None
        transfers = record.get("transfers") if isinstance(record, dict) else None
        for transfer in transfers if isinstance(transfers, list) else []:
            if isinstance(transfer, dict) and remote.matches(transfer) and (transfer_id := _uuid(transfer.get("id"))):
                return batch, transfer_id
        raise ProviderError("slskd не поставил файл в очередь")

    async def _wait(self, remote: RemoteFile, batch: str, transfer_id: str,
                    on_progress: Callable[[float], None] | None) -> Path:
        route = f"{remote.route}/{transfer_id}"
        try:
            async with asyncio.timeout(self.cfg.transfer_timeout_s):
                while True:
                    transfer = await self._api("GET", route)
                    if not isinstance(transfer, dict) or not remote.matches(transfer):
                        raise ProviderError("slskd вернул другую загрузку")
                    state = _states(transfer)
                    if "succeeded" in state:
                        # slskd records success before moving the file into the completed directory.
                        if path := await asyncio.to_thread(self._completed, remote, batch):
                            return path
                    elif "completed" in state and not transfer.get("nextAttemptAt"):
                        raise ProviderError("Загрузка Soulseek завершилась ошибкой")
                    if on_progress:
                        on_progress(min(0.99, _positive(transfer.get("bytesTransferred")) / remote.size))
                    await asyncio.sleep(_POLL_S)
        except TimeoutError as exc:
            await self._delete(route)
            raise ProviderError("Истекло время ожидания загрузки Soulseek") from exc

    def _check_quality(self, source: Path, format: str) -> None:
        try:
            if format == "mp3":
                ok = round(MP3(source).info.bitrate / 1000) >= self.cfg.min_mp3_bitrate_kbps
            else:
                info = FLAC(source).info
                ok = (info.bits_per_sample >= self.cfg.min_flac_bit_depth
                      and info.sample_rate >= self.cfg.min_flac_sample_rate_hz)
        except MutagenError as exc:
            raise ProviderError("Файл Soulseek не соответствует формату") from exc
        if not ok:
            raise ProviderError("Файл Soulseek не соответствует настройкам качества")

    async def fetch(self, c: Candidate, dest_dir: Path, on_progress: Callable[[float], None] | None = None) -> Path:
        remote = RemoteFile.parse(c.extra.get("peer"), c.extra.get("filename"), c.extra.get("size"), self.cfg)
        if remote is None or remote.id != c.provider_id:
            raise ProviderError("Некорректный файл Soulseek")
        lock = self.file_locks[int(remote.id[:2], 16) % len(self.file_locks)]
        async with lock, self.transfer_slots:
            batch, result = await self._previous(remote) or await self._enqueue(remote)
            source = result if isinstance(result, Path) else await self._wait(remote, batch, result, on_progress)
            dest = dest_dir / f"soulseek_{remote.id}.{remote.format}"
            await asyncio.to_thread(shutil.copyfile, source, dest)
            try:
                await asyncio.to_thread(self._check_quality, dest, remote.format)
            except ProviderError:
                dest.unlink(missing_ok=True)
                raise
        if on_progress:
            on_progress(1)
        return dest

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()
