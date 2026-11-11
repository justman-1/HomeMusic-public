# Deployment, storage, and troubleshooting

## Runtime topology

The reference deployment runs on Windows with Docker Desktop and WSL2. `docker-compose.yml` starts `navidrome` (`4533`), `hub` (`8080`), the internal `youtube-pot` (`4416`) and `embedder` (`8090`) services, and `tunnel`. The VPS's Caddy terminates HTTPS and forwards to loopback ports opened by `gateway/tunnel/entrypoint.sh`. `gateway/vps/setup-vps.sh` provisions the VPS side. See [setup and rollback](../README.md) for host-specific steps.

Copy `.env.example` and `config.example.yaml` to `.env` and `config.yaml` for deployment. Compose passes environment variables into the containers and mounts the YAML into the Hub. Keep Navidrome credentials, `HUB_SECRET_KEY`, provider tokens, cookies, and tunnel keys in local configuration. `HUB_SECRET_KEY` is required to decrypt existing Hub credentials after a restore.

| State | Location and ownership |
|---|---|
| Audio files | `MUSIC_ROOT/library/` is writable by the Hub and mounted read-only by Navidrome and embedder. |
| Transient and deleted files | `MUSIC_ROOT/staging/` holds work files and playback caches. `MUSIC_ROOT/.trash/` holds deleted library files until cleanup (30 days by default). |
| Hub and Navidrome databases | Compose named volumes `hub-data` and `navidrome-data`. Keep live SQLite databases on the Docker filesystem, where WAL and locking work. |
| Database backups | `MUSIC_ROOT/.backups/hub/` and `.backups/navidrome/`. The two services produce their own daily backups and retain seven copies by default. Hub backup uses SQLite's backup API, then copies the snapshot to the music mount. |
| Vector state | `embedder-data` holds `embedder.sqlite3`. `embedder-models` holds model weights and caches. |

## First checks

Run from the repository root on the Compose host.

```sh
docker compose ps
docker compose logs --tail 100 hub navidrome tunnel
curl -fsS http://localhost:8080/healthz
```

| Symptom | Check |
|---|---|
| Hub responds but library or login fails | Inspect `navidrome` in Hub `/healthz`, then check Navidrome logs and the Hub's connectivity and credentials. Hub health can return HTTP 200 with `navidrome: false`. |
| Local works but the public address fails | Check tunnel logs and Caddy on the VPS. Compare Hub and Navidrome separately so a failure on one route can be localized. |
| A file is imported but the item stays `importing` | Inspect Hub logs for `nd_scan`, then check the file's library-relative path against the Navidrome scan and ID mapping. |
| Catalog works but audio fails | Check provider selection and postchecks in Hub logs. Use `/api/system/health` as an admin when a live provider probe is needed. For YouTube, check the internal `youtube-pot` service and the [provider diagnostics](../README.md#youtube-просит-проверку-не-робот). Keep the provider plugin and server versions in sync. |
| Recommendations are missing audio similarity | Check `EMBEDDER_URL` and the optional shared token. Read embedder `/healthz` from inside the Compose network and inspect `state`, `error`, `device`, and queue counts. `idle` is normal before work. GPU pressure and first-time model downloads can delay jobs. |
| The web UI looks stale after a build | Compare `web_build` from `/healthz` with the build ID served in `index.html` and `sw.js`. Check that the Hub image contains the new PWA bundle. |

## Releases and recovery

- For a Hub-only update, build and recreate the Hub after checking active jobs and the relevant tests. The worker pool requeues interrupted jobs at startup. Rebuild other images when the change touches their code or a shared contract.
- Before changes to SQLite schemas, tags, or Navidrome persistent IDs, verify the relevant backup and rehearse against a disposable copy. Recover from a pre-upgrade snapshot when rolling back to code that cannot read the migrated schema. Preserve the matching Hub secret and keep the music files, tags, and database records consistent.
- Keep data volumes during normal restarts and image changes. Recover database and media state together if a change moved or deleted files. Hub and Navidrome database backups do not include the audio tree.
- `scripts/release-android.ps1` works with the separate Android checkout. The Hub serves `/app/latest.json`, `/app/latest.apk`, and `/app/artifacts/{name}`. Use the [Android release instructions](../README.md#android-приложение-homemusic-форк-tempus) for rehearsals, publication, and client compatibility.

## Soulseek

1. In `.env`, set `SLSKD_URL=http://slskd:5030`, `SLSKD_SLSK_USERNAME`, `SLSKD_SLSK_PASSWORD`, and a random `SLSKD_API_KEY` of at least 16 characters. Keep the API key unprefixed in `.env`. Compose sets the daemon's `ReadWrite` role.
2. Start the optional daemon and rebuild the Hub with `docker compose --profile soulseek up -d --build slskd hub`. Set `COMPOSE_PROFILES=soulseek` in `.env` to include the daemon in regular Compose starts. The slskd HTTP API stays on the Compose network. Keep its dedicated completed-download volume mounted read-only into the Hub and leave the library outside slskd's mounts.
3. Use the Files search or `GET /api/search/providers?provider=soulseek&q=Artist%20-%20Title`. For automatic selection, add `soulseek` to the desired `sources.ru`, `sources.default`, or `sources.download_fallback` list in `config.yaml`. Source order still determines when the Hub tries Soulseek. Restart the Hub after changing YAML.

Use `soulseek.format: any|flac|mp3` to restrict formats. Set `min_mp3_bitrate_kbps` for MP3 and `min_flac_bit_depth` or `min_flac_sample_rate_hz` for FLAC. Zero disables a threshold. An active threshold requires the peer to report the corresponding metadata. The Hub checks the actual audio format and quality after transfer, then runs the normal audio postcheck and library import. The Hub can reuse a matching slskd transfer or a retained complete batch. slskd manages the retention of completed files in its volume.

With Soulseek enabled, Mix of the Day, Wave and favorite downloads prefer Soulseek FLAC, then Soulseek MP3, Hitmoz and YouTube. The configured format and recording checks still apply. Catalog playback prefers the library, then Hitmoz and other HTTP sources. Playing a specific file continues to use that file's provider. Wave warms up to ten upcoming originals in the temporary audio cache. A Wave favorite can queue a FLAC upgrade for a lossy library track. The old file remains available for existing queues and playlists.

`jobs.workers` limits concurrent Hub jobs and keeps capacity for interactive work. `soulseek.parallel_transfers` limits concurrent transfers and reserves one slot for foreground work. With the defaults of six workers and six transfer slots, up to five background Soulseek transfers can run at once.

Use `docker compose --profile soulseek logs --tail 100 slskd hub` to distinguish login, search, and transfer failures. A valid search result can still be queued at a peer or become unavailable. `transfer_timeout_s` limits the wait and allows the Hub to move on to the next candidate. See [slskd configuration](https://github.com/slskd/slskd/blob/0.26.2/docs/config.md).

In the Hub, an admin can open «Ещё» → «Проверить Soulseek» to verify the daemon's login and run a search without downloading. With `soulseek.wait_when_unavailable: true`, automatic quality downloads wait when Soulseek is unavailable and retry after a minute. A successful search without a matching file still allows fallback. Set this option to `false` to allow immediate fallback during an outage.

If slskd repeatedly times out during login, try `SLSKD_SLSK_PORT=2242` and `SLSKD_SLSK_CONNECTION_TIMEOUT=15000` in `.env`, then recreate just the daemon with `docker compose --profile soulseek up -d slskd`. These change its outbound connection settings.

## Spotify import

1. Request **Account data** from Spotify's privacy settings and unpack the export. Use the playlist JSON files (`Playlist*.json`) and/or `YourLibrary.json`. [Spotify export instructions](https://support.spotify.com/uk/article/data-rights-and-privacy-settings/)
2. In the Hub, go to «Ещё» → «Перенести музыку» → «Из Spotify». Upload the selected JSON files, review the collections and start in economy mode or choose to download everything. An upload accepts up to 32 files, 8 MiB in total, and 10,000 music entries.
3. The Hub matches playlists and saved music against its library and catalog. Playlist order follows the export, with repeated track IDs kept once per list. Podcast and local-only rows are skipped. Audio comes from the Hub's configured sources. Upload a fresh export to start another transfer.

The «Проблемы» filter in Downloads has a separate «Очистить» action. It dismisses failed and review-needed jobs from the list while preserving links from mixes and import reports. Retrying from a linked detail restores the item to Downloads.
