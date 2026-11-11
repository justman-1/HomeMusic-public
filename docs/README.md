# HomeMusic developer guide

HomeMusic runs a shared audio library through Navidrome and a FastAPI Hub. The Hub handles search, downloads, uploads, imports, recommendations, and the assistant. It serves a React PWA that runs as a standalone player or as the Hub screens in the Android WebView. A separate embedder service supplies audio similarity.

- [Architecture and data flows](architecture.md)
- [Local development and checks](development.md)
- [Deployment, storage, and troubleshooting](operations.md)
- [Hub API and Android-facing contracts](../hub/API.md)
- [Setup and user-facing instructions](../README.md)

Paths in the table are relative to the repository root. Start at the interface and follow the call into the implementation. Use the current router, schemas, and tests for exact behavior. Some comments and the API reference cite historical design files that are absent from this checkout.

| Area | Entry points and implementation | Start with these tests under `hub/tests/` |
|---|---|---|
| Sessions, registration, admin | `hub/app/api/auth.py`, `auth.py` and `accounts.py` under `hub/app/`, `web/src/pages/Login.tsx` | `test_accounts.py`, `test_app_deps.py` |
| Catalog, file search, matching | `hub/app/api/search.py`, `api/catalog.py`, `catalog/`, `providers/`, `matching/` under `hub/app/`, `web/src/pages/Search.tsx` | `test_deezer.py`, `test_hitmoz_catalog.py`, `test_hitmos_fm.py`, `test_normalize.py`, `test_select.py`, `test_score.py` |
| Soulseek search and downloads | `hub/app/providers/soulseek.py`, `hub/app/matching/soulseek.py`, [deployment and filters](operations.md#soulseek) | `test_soulseek.py`, `test_flow.py` |
| Downloads and Navidrome linking | `hub/app/api/downloads.py`, `jobs/downloads.py`, `jobs/queue.py` under `hub/app/`, `web/src/pages/Downloads.tsx`, `DownloadDetail.tsx` | `test_flow.py`, `test_download_playlists.py`, `test_queue_priority.py` |
| Streaming and playback | `hub/app/api/listen.py`, `listen.py`, `light.py` under `hub/app/`, `web/src/lib/listen.ts`, `player.ts`, `waveAudio.ts` | `test_listen.py`, `test_light.py`, `test_net_priority.py` |
| Library, uploads, playlists | `hub/app/api/library.py`, `api/medialib.py`, `api/uploads.py`, `api/playlists.py`, `library/` under `hub/app/`, `web/src/pages/MediaLibrary.tsx`, `Library.tsx`, `LibraryAlbum.tsx` | `test_medialib.py`, `test_layout.py`, `test_probe.py`, `test_tagger.py`, `test_flow.py` |
| Favorites, play events, summaries | `hub/app/api/plays.py`, `plays.py`, `favorites.py`, `listening_summary.py` under `hub/app/`, `web/src/lib/plays.ts`, `stars.ts`, `dislikes.ts` | `test_plays.py`, `test_favorites.py`, `test_migration_favorites.py` |
| Daily mixes, wave, smart shuffle, taste | `hub/app/api/{mixes,wave,smart_shuffle}.py`, `hub/app/mixes/`, `web/src/lib/{wave,smartShuffle}.ts` | `test_mixes.py`, `test_mix_build.py`, `test_wave.py`, `test_smart.py`, `test_taste_math.py`, `test_taste_consumers.py` |
| Admin insights | `hub/app/api/insights.py`, `hub/app/insights/`, `web/src/api/insights.ts`, `web/src/pages/insights/` | `test_insights.py`, `test_insights_map.py`, `test_migration_insights.py` |
| Yandex Music and Spotify imports | `hub/app/api/imports.py`, `hub/app/imports/{core,resolve,apply}.py`, `hub/app/imports/sources/`, `web/src/pages/Import.tsx` | `test_imports.py`, `test_import_yandex.py` |
| Assistant and sharing | `hub/app/api/assistant.py`, `api/system.py`, `ai/` under `hub/app/`, `web/src/pages/Assistant*.tsx`, `Share.tsx` | `test_ai_agent.py`, `test_ai_extract.py`, `test_ai_links.py`, `test_ai_limits.py` |
| Embedding and vector geometry | Hub adapter `hub/app/mixes/embedder.py` and `embedder/app/{main,worker,db,geometry}.py` | `test_embedder_geometry.py` and `embedder/tests/` |
| PWA build and Android updates | `web/vite.config.ts`, `web/public/sw.js`, `web/src/lib/{embedded,update,capabilities}.ts`, `hub/app/api/system.py`, `scripts/release-android.ps1` | `test_web_build.py`, `test_app_updates.py` |
