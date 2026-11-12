#!/usr/bin/env bash
# Installs only the private relay service; does not modify Nginx/firewalls.
set -Eeuo pipefail
umask 077
export LC_ALL=C
readonly PACKAGE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SERVICE_USER=yifabao-media-relay
readonly APP_DIR=/opt/yifabao-media-relay
readonly ETC_DIR=/etc/yifabao-media-relay
readonly DATA_ROOT=/var/lib/yifabao-media-relay
readonly UNIT=/etc/systemd/system/yifabao-media-relay.service
readonly MARKER=yifabao-media-relay-managed-v1
readonly PUBLIC_IP="${MEDIA_RELAY_PUBLIC_IP:?Set MEDIA_RELAY_PUBLIC_IP to this server public IPv4 address}"
export MEDIA_RELAY_PUBLIC_IP="$PUBLIC_IP"
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die 'Run this script as root.'
[[ -f "$PACKAGE_DIR/server.py" ]] || die 'Package is incomplete: server.py is missing.'
command -v python3 >/dev/null || die 'python3 is missing.'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die 'Python 3.10+ is required.'
command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || die 'A running systemd installation is required.'
readonly PYTHON="$(command -v python3)"
[[ "$PYTHON" =~ ^/[A-Za-z0-9_./-]+$ ]] || die 'Unsupported Python executable path.'
for managed_dir in "$APP_DIR" "$ETC_DIR" "$DATA_ROOT"; do
    [[ ! -L "$managed_dir" ]] || die "Refusing symlink: $managed_dir"
    if [[ -e "$managed_dir" ]]; then
        [[ -d "$managed_dir" && -f "$managed_dir/.managed-by-yifabao" && ! -L "$managed_dir/.managed-by-yifabao" ]] || die "Refusing unmanaged directory: $managed_dir"
        [[ "$(cat "$managed_dir/.managed-by-yifabao")" == "$MARKER" ]] || die "Unexpected management marker: $managed_dir"
    fi
done
[[ ! -L "$UNIT" ]] || die "Refusing symlink: $UNIT"
if [[ -e "$UNIT" ]] && ! head -n 1 "$UNIT" | grep -Fxq "# $MARKER"; then
    die "Refusing unmanaged service unit: $UNIT"
fi
if [[ ! -e "$UNIT" ]]; then
    "$PYTHON" - <<'PY'
import socket
with socket.socket() as probe:
    try:
        probe.bind(("127.0.0.1", 18741))
    except OSError:
        raise SystemExit("Port 18741 is already occupied. No existing process was stopped.") from None
PY
fi
if getent passwd "$SERVICE_USER" >/dev/null; then
    [[ -d "$ETC_DIR" && -f "$ETC_DIR/.managed-by-yifabao" ]] || die 'Service account already exists without a managed installation.'
    user_shell="$(getent passwd "$SERVICE_USER" | cut -d: -f7)"
    [[ "$user_shell" == */nologin || "$user_shell" == /bin/false ]] || die 'Existing service account allows login.'
else
    command -v useradd >/dev/null || die 'useradd is missing.'
    nologin_path="$(command -v nologin || true)"
    [[ -n "$nologin_path" ]] || nologin_path=/bin/false
    useradd --system --user-group --home-dir "$DATA_ROOT" --no-create-home --shell "$nologin_path" "$SERVICE_USER"
fi
for managed_dir in "$APP_DIR" "$ETC_DIR" "$DATA_ROOT"; do
    install -d -o root -g "$SERVICE_USER" -m 0750 "$managed_dir"
    if [[ ! -e "$managed_dir/.managed-by-yifabao" ]]; then
        printf '%s\n' "$MARKER" > "$managed_dir/.managed-by-yifabao"
    fi
done
# Reject pre-existing symlinks before writing any managed files/subdirectories.
for managed_path in "$APP_DIR/server.py" "$ETC_DIR/relay.env" "$ETC_DIR/upload-token" "$ETC_DIR/signing-secret" "$ETC_DIR/client-connection.json" "$DATA_ROOT/objects" "$DATA_ROOT/acme"; do
    [[ ! -L "$managed_path" ]] || die "Refusing symlink: $managed_path"
done
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_ROOT/objects"
# The public challenge root contains challenge files only, never uploaded media.
chmod 0755 "$DATA_ROOT"
install -d -o root -g root -m 0755 "$DATA_ROOT/acme"
install -o root -g root -m 0644 "$PACKAGE_DIR/server.py" "$APP_DIR/server.py"
"$PYTHON" - "$ETC_DIR" <<'PY'
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

root = Path(sys.argv[1])
for name in ("upload-token", "signing-secret"):
    path = root / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(48) + "\n")
    value = path.read_text().strip()
    if len(value) < 32 or not all(c.isalnum() or c in "-_" for c in value):
        raise SystemExit("Existing relay secret has an invalid format; it was not replaced.")
connection = {
    "base_url": f"https://{os.environ['MEDIA_RELAY_PUBLIC_IP']}:8443",
    "token": (root / "upload-token").read_text().strip(),
    "url_ttl_seconds": 1800,
}
fd, temporary_name = tempfile.mkstemp(prefix=".client-connection-", dir=root)
temporary = Path(temporary_name)
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(connection, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, root / "client-connection.json")
finally:
    temporary.unlink(missing_ok=True)
PY
chown root:"$SERVICE_USER" "$ETC_DIR/upload-token" "$ETC_DIR/signing-secret"
chmod 0640 "$ETC_DIR/upload-token" "$ETC_DIR/signing-secret"
chown root:root "$ETC_DIR/client-connection.json"
chmod 0600 "$ETC_DIR/client-connection.json"
cat > "$ETC_DIR/relay.env" <<'ENV'
MEDIA_RELAY_HOST=127.0.0.1
MEDIA_RELAY_PORT=18741
MEDIA_RELAY_DATA_DIR=/var/lib/yifabao-media-relay/objects
MEDIA_RELAY_UPLOAD_TOKEN_FILE=/etc/yifabao-media-relay/upload-token
MEDIA_RELAY_SIGNING_SECRET_FILE=/etc/yifabao-media-relay/signing-secret
MEDIA_RELAY_URL_TTL_SECONDS=1800
MEDIA_RELAY_OBJECT_TTL_SECONDS=86400
MEDIA_RELAY_MAX_STORAGE_BYTES=5368709120
ENV
printf 'MEDIA_RELAY_PUBLIC_BASE_URL=https://%s:8443\n' "$PUBLIC_IP" >> "$ETC_DIR/relay.env"
chmod 0600 "$ETC_DIR/relay.env"
cat > "$UNIT" <<UNIT
# $MARKER
[Unit]
Description=Yifabao private reference-video relay
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ETC_DIR/relay.env
ExecStart=$PYTHON $APP_DIR/server.py
WorkingDirectory=$APP_DIR
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_ROOT/objects
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CPUQuota=50%
MemoryMax=256M
TasksMax=64
LimitNOFILE=1024
IOWeight=50
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$UNIT"
systemctl daemon-reload
systemctl enable yifabao-media-relay.service
systemctl restart yifabao-media-relay.service
"$PYTHON" - <<'PY'
import json
import time
from urllib.request import urlopen

for attempt in range(20):
    try:
        with urlopen("http://127.0.0.1:18741/healthz", timeout=2) as response:
            health = json.load(response)
            if response.status == 200 and health == {"status": "ok", "service": "yifabao-media-relay", "api_version": 1}:
                print("Local relay health check passed.")
                break
            raise SystemExit("Unexpected service on port 18741; health identity/version check failed.")
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit("Relay did not become healthy. Inspect: journalctl -u yifabao-media-relay -n 30")
PY
printf '\nService installed. Nginx and firewalls were not modified.\n'
printf 'Download this root-only connection file privately for local import:\n%s/client-connection.json\n' "$ETC_DIR"
printf 'Do not paste its contents into chat or put it under a public website directory.\n'
