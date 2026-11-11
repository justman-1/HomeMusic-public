# Local development and checks

Run each block from the repository root. Both Python services use Python 3.12 and `uv`. The Hub container builds the PWA with Node 22.20.0. Use the checked-in lockfiles for installs.

## Hub

```sh
cd hub
uv sync --frozen
uv run --no-sync pytest -q tests/test_listen.py
uv run --no-sync ruff check app tests
```

Replace the sample test with the closest tests in the [feature map](README.md). Use `uv run --no-sync pytest -q` for the full suite. `tests/conftest.py` prepares a migrated SQLite database, a temporary music tree, and isolated settings. Keep the existing `respx` fixtures for HTTP dependencies. Media tests need `ffmpeg` and `ffprobe` on `PATH`. The Hub image includes both. Tests marked `network` require explicit `HUB_NETWORK_TESTS=1` where applicable.

For a local Hub process on a POSIX shell, use a disposable data tree and point at a local Navidrome instance if the flow needs one. Supply test credentials and a valid Fernet `HUB_SECRET_KEY` through the process environment for authenticated flows. The Hub reads environment variables directly, so copy-and-editing `.env` alone applies to Compose, while a direct `uvicorn` launch needs exported values.

```sh
cd hub
scratch=$(mktemp -d)
MUSIC_ROOT="$scratch/music" DATA_DIR="$scratch/data" CONFIG_FILE="$scratch/config.yaml" \
  NAVIDROME_URL=http://127.0.0.1:4533 EMBEDDER_URL='' DISABLE_BACKGROUND=true \
  uv run --no-sync uvicorn app.main:app --reload --port 8080
```

Enable background work with `DISABLE_BACKGROUND=false` when verifying downloads, imports, scheduled jobs, or other worker-dependent flows in the disposable setup. API docs are at `/api/docs`.

## Web

```sh
cd web
npm ci
npm run dev:mock
```

The mock uses `web/mock/plugin.ts` and `web/mock/insights.ts` to serve local fixtures on port 5173. Use `npm run dev` to proxy Hub requests to port 8080. Check UI changes in standalone mode and with `?embedded=1` when the affected view runs in both. Use a native app for checks that require the actual WebView bridge.

`npm run typecheck` checks the app and the Vite configuration. `npm run build` also bundles the PWA and writes generated files to `hub/app/static/`. Vite stamps one build ID into the bundle, `index.html`, and `sw.js`. `web/src/lib/update.ts` reads the ID from Hub `/healthz` and reloads when the server build changes.

## Embedder

The locked environment targets Linux x86-64. Run the default fake-model tests from `embedder/` on that platform with `uv sync --frozen` followed by `uv run --no-sync pytest -q`. Install `ffmpeg` for media tests. For the container workflow and real GPU tests, see [embedder in the main README](../README.md#сервис-embedder-звуковые-векторы-для-миксов). The real-model suite is opt-in via `EMBEDDER_REAL_MODEL_TESTS=1` and requires the `gpu` extra and model weights.

Run Hub and embedder tests from their own directories because both packages are named `app`. For changes to the internal API, check the adapter tests in `hub/tests/test_embedder_geometry.py` and the HTTP tests in `embedder/tests/`.
