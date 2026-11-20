#!/usr/bin/env bash
set -Eeuo pipefail

# First-time Ubuntu 24.04 VPS setup for a standalone HomeMusic deployment.
# Run as root. This script intentionally leaves the firewall unchanged to avoid
# locking out SSH sessions; configure UFW only after confirming SSH access.

APP_DIR=/srv/homemusic-app
MUSIC_ROOT=/srv/homemusic
SWAPFILE=/swapfile

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "Run this script as root (sudo -i first)."
[[ -r /etc/os-release ]] || fail "Cannot identify operating system."
# shellcheck disable=SC1091
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail "This bootstrap supports Ubuntu 24.04 only. Found ${PRETTY_NAME:-unknown}."

printf '%s\n' 'HomeMusic standalone VPS bootstrap (Ubuntu 24.04)' \
  'This installs Docker, Caddy, application files, and the HomeMusic containers.' \
  'It does not change UFW/firewall rules or upload any music.'
read -r -p 'Continue? [y/N] ' answer
[[ $answer =~ ^[Yy]$ ]] || exit 0

read -r -p 'HTTPS music hostname (e.g. music.example.com): ' MUSIC_HOST
read -r -p 'HTTPS Hub hostname (e.g. hub.example.com): ' HUB_HOST
[[ $MUSIC_HOST =~ ^[A-Za-z0-9.-]+$ && $MUSIC_HOST == *.* ]] || fail "Invalid music hostname."
[[ $HUB_HOST =~ ^[A-Za-z0-9.-]+$ && $HUB_HOST == *.* ]] || fail "Invalid Hub hostname."
[[ $MUSIC_HOST != "$HUB_HOST" ]] || fail "Use separate hostnames for Navidrome and Hub."
read -r -p 'HTTPS Git URL of your HomeMusic fork: ' REPO_URL
[[ $REPO_URL =~ ^https://github\.com/[^[:space:]]+\.git$ ]] || fail "Use an HTTPS GitHub URL ending in .git."

for host in "$MUSIC_HOST" "$HUB_HOST"; do
  getent ahostsv4 "$host" >/dev/null || fail "DNS does not resolve $host yet. Create its A record to this VPS and retry."
done

if [[ ! -d $APP_DIR/.git ]]; then
  [[ ! -e $APP_DIR ]] || fail "$APP_DIR exists but is not a Git checkout. Move it aside before retrying."
fi

printf '\n%s\n' 'Updating Ubuntu packages. If apt reports stale mirror/404 errors, stop and repair the Ubuntu mirror first.'
export DEBIAN_FRONTEND=noninteractive
apt-get update || fail "apt-get update failed. Check the configured Ubuntu mirror; no packages were installed by this script."
apt-get upgrade -y
apt-get install -y ca-certificates curl git gnupg openssl ufw python3 debian-keyring debian-archive-keyring apt-transport-https

# Docker's official Ubuntu repository.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
arch=$(dpkg --print-architecture)
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
  "$arch" "$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.list

# Official Caddy packages.
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  -o /etc/apt/sources.list.d/caddy-stable.list

apt-get update || fail "apt-get update failed after adding Docker/Caddy repositories. Check apt sources and retry."
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin caddy
systemctl enable --now docker
systemctl enable --now caddy

if ! swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then
  if [[ ! -e $SWAPFILE ]]; then
    fallocate -l 4G "$SWAPFILE" || fail "Could not allocate a 4 GiB swap file. Check available disk space."
    chmod 600 "$SWAPFILE"
    mkswap "$SWAPFILE" >/dev/null
  fi
  swapon "$SWAPFILE"
fi
grep -qF "$SWAPFILE none swap sw 0 0" /etc/fstab || printf '%s\n' "$SWAPFILE none swap sw 0 0" >> /etc/fstab
printf '%s\n' 'vm.swappiness=10' > /etc/sysctl.d/99-homemusic.conf
sysctl --system >/dev/null

if [[ ! -d $APP_DIR/.git ]]; then
  install -d -m 0755 "$(dirname "$APP_DIR")"
  git clone "$REPO_URL" "$APP_DIR"
else
  printf 'Using existing checkout at %s\n' "$APP_DIR"
fi
cd "$APP_DIR"
[[ -f docker-compose.prod.yml && -f .env.vps.example && -f config.example.yaml ]] || fail "Fork is missing Linux production files. Push the deployment files before running this script."

install -d -m 0755 "$MUSIC_ROOT"/{library,staging,.trash,.backups}

if [[ ! -e .env ]]; then
  cp .env.vps.example .env
  chmod 600 .env
  sed -i \
    -e "s|^PUBLIC_MUSIC_URL=.*|PUBLIC_MUSIC_URL=https://${MUSIC_HOST}|" \
    -e "s|^PUBLIC_HUB_URL=.*|PUBLIC_HUB_URL=https://${HUB_HOST}|" \
    -e "s|^ND_PASSWORDENCRYPTIONKEY=.*|ND_PASSWORDENCRYPTIONKEY=$(openssl rand -hex 32)|" \
    -e "s|^HUB_SECRET_KEY=.*|HUB_SECRET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || true)|" \
    .env
  # The host may not have cryptography installed. Generate a valid Fernet key
  # with Python's stdlib: 32 random bytes encoded as URL-safe base64.
  if grep -q '^HUB_SECRET_KEY=$' .env || grep -q '^HUB_SECRET_KEY=CHANGE_ME' .env; then
    hub_key=$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
    sed -i "s|^HUB_SECRET_KEY=.*|HUB_SECRET_KEY=${hub_key}|" .env
  fi
  admin_password=$(openssl rand -hex 24)
  sed -i \
    -e 's|^NAVIDROME_ADMIN_USER=.*|NAVIDROME_ADMIN_USER=admin|' \
    -e "s|^NAVIDROME_ADMIN_PASSWORD=.*|NAVIDROME_ADMIN_PASSWORD=${admin_password}|" \
    .env
  printf '\nGenerated Navidrome admin password (save it now): %s\n' "$admin_password"
  unset admin_password hub_key
else
  chmod 600 .env
  printf '%s\n' 'Existing .env found; it was preserved. Verify its secrets and public URLs manually.'
fi

[[ -f config.yaml ]] || cp config.example.yaml config.yaml

# Install the repository config when there is no existing HomeMusic proxy.
# Preserve an operator-managed Caddyfile that already contains the HomeMusic upstreams.
if [[ ! -e /etc/caddy/Caddyfile ]] || ! grep -qF 'reverse_proxy 127.0.0.1:4533' /etc/caddy/Caddyfile; then
  if [[ -e /etc/caddy/Caddyfile ]]; then
    cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.backup.$(date +%s)"
  fi
  sed -e "s/music\.example\.com/${MUSIC_HOST}/g" -e "s/hub\.example\.com/${HUB_HOST}/g" \
    "$APP_DIR/Caddyfile" > /etc/caddy/Caddyfile
else
  printf '%s\n' 'Existing HomeMusic Caddyfile preserved.'
fi

caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy

# Build the large CPU embedder and start the complete stack.
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml up -d --build

# Install the unit only if there is no existing operator-managed unit.
if [[ ! -e /etc/systemd/system/homemusic.service ]]; then
  install -m 0644 homemusic.service /etc/systemd/system/homemusic.service
  systemctl daemon-reload
  systemctl enable homemusic.service
fi
systemctl start homemusic.service

printf '\nSetup finished. Check DNS/TLS and service status:\n'
printf '  https://%s\n  https://%s\n' "$HUB_HOST" "$MUSIC_HOST"
printf '  cd %s && docker compose -f docker-compose.prod.yml ps\n' "$APP_DIR"
printf '\nFirewall was not changed. Configure SSH/HTTP/HTTPS ingress in your provider firewall or UFW after verifying SSH access.\n'
printf 'Back up /srv/homemusic and Docker volumes off-host. Do not commit .env.\n'
