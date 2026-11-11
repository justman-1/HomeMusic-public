# Architecture and data flows

## Service boundaries

| Component | Owns |
|---|---|
| Navidrome | Shared library index and Subsonic API, accounts, playlists, starred mirror, scrobbles, and playback for native clients. `hub/app/library/navidrome.py` is the Hub client. |
| Hub | FastAPI routes and PWA assets. Tracks file provenance and library edits, queues work, stores user events and favorite decisions, and builds recommendations in SQLite. |
| Web | React and TypeScript PWA built by Vite into `hub/app/static/`. `web/src/App.tsx` declares hash routes. `web/src/api/client.ts` and `types.ts` define the common API boundary. |
| Embedder | Internal FastAPI worker with its own SQLite queue and vector store. `hub/app/mixes/embedder.py` wraps the calls. |
| Gateway | Caddy on the VPS terminates HTTPS and uses the loopback listeners opened by the reverse SSH tunnel. See [operations](operations.md). |

`hub/app/main.py` creates directories, upgrades Alembic, initializes the async SQLite engine and clients, and starts an in-process worker pool and periodic tasks. It also mounts the built PWA when `index.html` exists. `DISABLE_BACKGROUND=true` skips the worker pool and periodic tasks. Settings in `hub/app/config.py` come from the process environment and optional YAML loaded from `CONFIG_FILE`. `hub/app/services.py` initializes shared catalog clients and download providers.

Navidrome validates login credentials. The Hub stores a hashed session token in SQLite and returns the `hm_session` cookie. User-scoped Subsonic calls use `user_navidrome()`. Admin routes use `admin_user()`. `db.session_scope()` commits on success and rolls back on failure. FastAPI routes use `Depends(get_session, scope="function")` so the transaction can commit before the response. SSE at `/api/events` delivers live per-user updates through a process-local bus. Re-read the relevant API state after reconnecting.

## Catalog, files, and playback

1. Search routes resolve metadata through `catalog/` and concrete file candidates through `providers/`. `matching/normalize.py`, `score.py`, and `select.py` handle identity, ranking, and selection. Keep Hub database IDs, catalog refs (`dz:…`, `hm:…`, `file:…`), and Navidrome song IDs distinct.
2. `jobs/downloads.py` expands a request into items, checks existing ownership, ranks and fetches a candidate, then probes and tags the file. `library/importer.py` moves it into `library/` and records it. A coalesced `nd_scan` links Navidrome IDs by path, completes download items, and applies deferred playlist or favorite operations. Uploads also use the library importer. `library/editor.py` handles updates, moves, and trash.
3. `jobs/queue.py` persists work in SQLite, claims jobs atomically, and requeues interrupted `running` jobs on startup. Enqueue in the transaction that creates the related state. Notify workers after commit. Handlers can run again after partial completion. Preserve background priority and `net.py` rate limits so imports and prefetch yield to interactive work.
4. `/api/listen` resolves a source. `/api/listen/stream` serves a library file, a cached or growing server copy, or a preview if allowed. Preserve byte ranges, `full=1`, and `q=mobile`. The light variant may initially serve the original while the encode finishes. Preserve its response headers and cache identity when changing playback. Prefetch uses lower priority and is distinct from a play event.

Stable file tags (`HOMEMUSIC_TRACK_ID`, `HOMEMUSIC_ALBUM_ID`) feed Navidrome persistent IDs through `navidrome/navidrome.toml`. Hub paths are relative to the library and sanitized for Windows by `library/layout.py`. A path, tagging, or scanner change can affect the link between the two databases.

## Listening, favorites, and recommendations

- `plays.py` ingests per-user events idempotently by `event_id` and links catalog and Navidrome IDs where possible. `listening_summary.py` aggregates cumulative checkpoints by playback attempt and separates measured time from scrobble and legacy evidence. The periodic scrobble import handles overlap with PWA and native events.
- `favorites.py` folds typed track, album, and artist decisions across verified aliases. It records import provenance and exact decision reversals. Navidrome reconciliation uses a complete starred snapshot and respects newer Hub decisions and pending likes. Keep this history separate from the recommendation scoring window.
- `mixes/taste.py` selects the profile path. `taste_snapshot.py` and `taste_math.py` collect and score v2 inputs. `mixes.taste_model` defaults to `v1` and `mixes.taste_shadow` enables diagnostics for daily builds. Persisted wave and smart shuffle sessions keep their saved model, with unversioned state read as v1.
- `mixes/build.py` shares candidate generators, filters, and scoring. `mixes/core.py` schedules and materializes daily mixes. `mixes/wave.py` serves and reranks small batches using feedback. `mixes/smart.py` inserts recommendations into a playback plan for a playlist or likes while preserving the source playlist. Admin listening and recommendation diagnostics live in `insights/`.
- The embedder reads a library clip or remote preview, or embeds text. `embedder/app/worker.py` manages lazy model loading and batch processing. `geometry.py` implements versioned centered geometry for profile, similarity, and projection. Hub recommendation flows can continue when sound data is unavailable. See [the internal v2 contract](../hub/API.md#internal-embedder-geometry-v2-stage-e-2026-10-26).

## Imports and assistant

- `imports/core.py` orchestrates Yandex authorization, collection snapshots, matching, apply jobs, and cleanup. `resolve.py` finds matches and `apply.py` updates playlists, stars, and downloads. Economy imports can defer downloads until a play. Full imports queue missing tracks in the background. Preserve collection ordering and resumability.
- `ai/pipeline.py` turns a screenshot, text, or a link into a reviewed download task. `ai/agent.py` runs chat tools against the signed-in user's account. It records applied actions and presents irreversible actions for explicit confirmation. The share target is handled by `api/system.py`.

## Web and native compatibility

`web/src/lib/embedded.ts` identifies WebView mode. In a browser the PWA renders the library, full player, and navigation. In WebView mode Android owns those screens. `web/src/lib/player.ts` checks bridge methods at runtime and chooses the supported playback path. `web/src/lib/capabilities.ts` handles advertised Hub features. When touching contracts, check `api/schemas.py`, PWA types and call sites, the Vite mock, and `/healthz`. Native Android source is in a separate repository.
