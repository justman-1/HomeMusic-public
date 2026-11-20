# Linux VPS deployment

This guide runs HomeMusic on one Linux server. The Windows/WSL2 reverse-SSH topology remains documented in the main README; this deployment does not use `gateway/tunnel` and does not require a home PC.

## Requirements

- Ubuntu 24.04 LTS
- Docker Engine and the Compose plugin
- 2 vCPU, 8 GiB RAM, and at least 80 GiB disk for a small library with the CPU audio embedder
- 4 GiB swap for the CPU embedder
- Two DNS A records pointing to the VPS:
  - `music.example.com` for Navidrome/Subsonic clients
  - `hub.example.com` for the PWA/Hub
- TCP ports 22, 80, and 443 available. Do not publish 4533 or 8080 publicly.

The embedder is optional. Without it, mixes, taste, history, statistics, and the wave still work, but audio-similarity scoring and sound facets are unavailable. On a 4 GiB VPS set `EMBEDDER_URL=` and use the stack without the model.

## Automated setup

The bootstrap is deliberately interactive. It does not alter UFW rules, overwrite an existing `.env`, or replace an operator-managed Caddyfile. Run it as root after DNS has propagated:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/REPOSITORY/main/scripts/bootstrap-vps.sh -o /root/bootstrap-vps.sh
chmod 700 /root/bootstrap-vps.sh
/root/bootstrap-vps.sh
```

The script asks for the two hostnames and the HTTPS URL of the fork. It then installs Docker and Caddy, creates `/srv/homemusic-app` and `/srv/homemusic`, creates a 4 GiB swapfile if needed, generates fresh secrets, validates Compose, builds the images, and enables `homemusic.service`.

Do not execute a downloaded script without reviewing or pinning its source. For a fork, replace `OWNER/REPOSITORY` and use a release tag or commit URL when possible.

After it finishes:

```bash
cd /srv/homemusic-app
docker compose -f docker-compose.prod.yml ps
curl -f http://127.0.0.1:8080/healthz
systemctl is-enabled homemusic.service
```

The script prints the generated Navidrome admin password once. Save it securely. The first Navidrome administrator is still created through the Navidrome web UI; use the same username and password as `NAVIDROME_ADMIN_USER` and `NAVIDROME_ADMIN_PASSWORD` in `.env`.

Configure firewall rules only after confirming that a second SSH session can connect:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose
```

## Manual setup

Clone the fork into the path expected by the systemd unit and create the data directories:

```bash
mkdir -p /srv/homemusic-app
mkdir -p /srv/homemusic/{library,staging,.trash,.backups}
git clone https://github.com/OWNER/REPOSITORY.git /srv/homemusic-app
cd /srv/homemusic-app
cp .env.vps.example .env
cp config.example.yaml config.yaml
chmod 600 .env
```

Edit `.env` before starting:

```env
MUSIC_ROOT=/srv/homemusic
PUBLIC_MUSIC_URL=https://music.example.com
PUBLIC_HUB_URL=https://hub.example.com
ND_PASSWORDENCRYPTIONKEY=<stable random value>
NAVIDROME_ADMIN_USER=admin
NAVIDROME_ADMIN_PASSWORD=<strong password>
HUB_SECRET_KEY=<Fernet key from Fernet.generate_key()>
LASTFM_API_KEY=<optional Last.fm API key>
EMBEDDER_DEVICE=cpu
EMBEDDER_CPU_THREADS=2
EMBEDDER_DOWNLOADS=2
EMBEDDER_CLIP_BATCH=2
EMBEDDER_IDLE_UNLOAD_S=300
```

`HUB_SECRET_KEY` must be a valid Fernet key, not an arbitrary password:

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

The LLM variables may stay empty. LLM is not used for taste profiles, mixes, the wave, statistics, or audio similarity. `LASTFM_API_KEY` is optional but improves catalog-based recommendations. Navidrome scrobbling additionally uses `ND_LASTFM_APIKEY` and `ND_LASTFM_SECRET`, which are already wired into `docker-compose.prod.yml`.

Validate and start the stack:

```bash
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Install Caddy after replacing the example hostnames in [`Caddyfile`](../Caddyfile):

```bash
cp Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

Install the boot unit:

```bash
cp homemusic.service /etc/systemd/system/homemusic.service
systemctl daemon-reload
systemctl enable --now homemusic.service
```

The containers bind their host ports to loopback only. Caddy serves HTTPS and obtains certificates automatically when DNS and ports 80/443 are correct.

## First login and library

1. Open the music hostname and create the first Navidrome administrator.
2. Use the same credentials in Hub at the Hub hostname.
3. Upload music through Hub or copy it to `/srv/homemusic/library` using SFTP/SCP.
4. Navidrome scans hourly; Hub also requests scans after imports.
5. The CPU embedder loads lazily. On a 2-vCPU VPS, keep imports, light-audio transcoding, and initial embedding from running at the same time.

Check the embedder without exposing its port:

```bash
docker compose -f docker-compose.prod.yml exec embedder python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/healthz').read().decode())"
```

`loaded: false` while idle is expected. A loaded CPU model reports `device: cpu` and `dim: 512`. The repository has no benchmark result for a particular VPS CPU; measure before promising processing time.

## Updates and backups

```bash
cd /srv/homemusic-app
git pull
docker compose -f docker-compose.prod.yml up -d --build
docker image prune -f
```

Back up both the bind-mounted music data and Docker volumes. The Navidrome and Hub local backups are not protection against VPS disk loss. Store copies on another server or object storage. Never commit `.env`, music, cookies, or API keys.

The `embedder-models` volume contains downloaded model weights and can consume several gigabytes. Monitor disk and RAM:

```bash
df -h /
free -h
docker system df
docker compose -f docker-compose.prod.yml ps
```

## Troubleshooting

- `apt` returns 404: refresh or replace a stale Ubuntu mirror before installing Docker.
- Caddy certificate errors: verify both A records, TCP 80/443, and that no other service owns those ports.
- Hub login returns a Fernet error: regenerate `HUB_SECRET_KEY` with the command above and recreate Hub.
- Wave says there is nothing to start from: listen to tracks or choose seed artists; imported files alone do not create taste events.
- YouTube/Yandex warnings are provider/IP limitations. They are not required for local library playback, and may require a proxy or alternative source.
- CPU embedder causes memory pressure: set `EMBEDDER_URL=` and recreate the Hub, or move to a VPS with more RAM/GPU.
