"""Process-wide singletons: download providers and catalog clients (created in the app lifespan)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.config import get_settings
from app.providers.base import Provider, TrackQuery

if TYPE_CHECKING:
    from app.catalog.deezer import DeezerClient
    from app.catalog.hitmoz_catalog import HitmozCatalog
    from app.catalog.musicbrainz import MusicBrainzClient

log = logging.getLogger(__name__)


class Services:
    def __init__(self) -> None:
        self.providers: dict[str, Provider] = {}
        self.deezer: DeezerClient | None = None
        self.hitmoz_catalog: HitmozCatalog | None = None
        self.musicbrainz: MusicBrainzClient | None = None

    def start(self) -> None:
        from app.catalog.deezer import DeezerClient
        from app.catalog.hitmoz_catalog import HitmozCatalog
        from app.catalog.musicbrainz import MusicBrainzClient
        from app.providers.hitmos_fm import HitmosFmProvider
        from app.providers.hitmoz import HitmozProvider
        from app.providers.reuploads import SoundcloudProvider, YtReuploadProvider
        from app.providers.soulseek import SoulseekProvider
        from app.providers.ytdlp import YoutubeAccess, YtdlpProvider

        settings = get_settings()
        cfg = settings.cfg
        hitmoz = HitmozProvider(cfg.hitmoz)
        youtube = YoutubeAccess(cfg.ytdlp)
        self.providers = {
            "hitmoz": hitmoz,
            "hitmos_fm": HitmosFmProvider(cfg.hitmos_fm),
            "ytdlp": YtdlpProvider(cfg.ytdlp, access=youtube),
            "yt_reupload": YtReuploadProvider(cfg.ytdlp, access=youtube),
            "soundcloud": SoundcloudProvider(cfg.ytdlp),
        }
        if settings.slskd_url and settings.slskd_api_key:
            self.providers["soulseek"] = SoulseekProvider(settings.slskd_url, settings.slskd_api_key,
                                                        settings.slskd_download_dir, cfg.soulseek, cfg.matching)
        self.deezer = DeezerClient(cfg.catalog)
        self.hitmoz_catalog = HitmozCatalog(hitmoz)
        self.musicbrainz = MusicBrainzClient(cfg.catalog)

    async def stop(self) -> None:
        for p in self.providers.values():
            try:
                await p.aclose()
            except Exception:
                log.exception("closing provider %s", p.name)
        for c in (self.deezer, self.musicbrainz):
            if c is not None:
                try:
                    await c.aclose()
                except Exception:
                    log.exception("closing catalog client")

    def provider_order(self, q: TrackQuery, *, for_download: bool = False,
                       for_playback: bool = False) -> list[Provider]:
        from app.matching.normalize import is_russian_query

        sources = get_settings().cfg.sources
        names = sources.ru if is_russian_query(q) else sources.default
        if for_download:
            names = [*names, *sources.download_fallback]
        if for_playback and "soulseek" in self.providers:
            names = ["hitmoz", "hitmos_fm", *(name for name in names if name != "soulseek"), "ytdlp"]
        return [self.providers[n] for n in dict.fromkeys(names) if n in self.providers]

    def library_download_order(self, q: TrackQuery) -> list[Provider]:
        if "soulseek" not in self.providers:
            return self.provider_order(q, for_download=True)
        return [self.providers[name] for name in ("soulseek", "hitmoz", "ytdlp") if name in self.providers]

    def reupload_providers(self) -> list[Provider]:
        """Sources of re-uploads by small channels: only for copies of explicit tracks without censorship."""
        return [self.providers[n] for n in get_settings().cfg.sources.reuploads if n in self.providers]


svc = Services()
