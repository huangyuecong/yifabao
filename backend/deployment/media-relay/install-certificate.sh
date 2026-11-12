#!/usr/bin/env bash
# Explicitly invoked certificate setup. No Nginx vhost or firewall edits.
set -Eeuo pipefail
umask 077
export LC_ALL=C
readonly PUBLIC_IP="${MEDIA_RELAY_PUBLIC_IP:?Set MEDIA_RELAY_PUBLIC_IP to this server public IPv4 address}"
readonly VENV=/opt/yifabao-media-certbot
readonly CERT_ROOT=/etc/yifabao-media-certbot
readonly NGINX=/www/server/nginx/sbin/nginx
readonly MARKER=yifabao-media-certbot-managed-v1
accept_terms=0
webroot=''
email=''
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
    printf 'Usage: bash install-certificate.sh --webroot /existing/http/site/root --accept-ca-terms [--email admin@example.com]\n'
    printf 'The flag explicitly accepts the current Let\047s Encrypt subscriber agreement: https://letsencrypt.org/repository/\n'
    printf 'Only ACME challenge files are created in the selected website. No vhost is edited.\n'
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --accept-ca-terms) accept_terms=1; shift ;;
        --webroot) [[ $# -ge 2 ]] || die '--webroot needs a directory.'; webroot="$2"; shift 2 ;;
        --email) [[ $# -ge 2 ]] || die '--email needs a value.'; email="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage; die "Unknown argument: $1" ;;
    esac
done
[[ "$accept_terms" -eq 1 ]] || { usage; die 'CA terms have not been explicitly accepted. No installation was performed.'; }
[[ ${EUID} -eq 0 ]] || die 'Run as root.'
[[ "$webroot" == /* && -d "$webroot" ]] || die 'Provide the existing absolute HTTP website root with --webroot.'
[[ "$webroot" != *$'\n'* && "$webroot" != *$'\r'* ]] || die 'Invalid webroot.'
[[ -x "$NGINX" ]] || die 'Baota Nginx was not found.'
command -v python3 >/dev/null || die 'python3 is missing.'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die 'Python 3.10+ is required.'
python3 -c 'import venv, ensurepip' || die 'The Python venv/ensurepip modules are missing. Install matching OS packages, then rerun.'
command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || die 'systemd is required.'
for managed_dir in "$VENV" "$CERT_ROOT"; do
    [[ ! -L "$managed_dir" ]] || die "Refusing symlink: $managed_dir"
    if [[ -e "$managed_dir" ]]; then
        [[ -d "$managed_dir" && -f "$managed_dir/.managed-by-yifabao" && ! -L "$managed_dir/.managed-by-yifabao" ]] || die "Refusing unmanaged directory: $managed_dir"
        [[ "$(cat "$managed_dir/.managed-by-yifabao")" == "$MARKER" ]] || die "Unexpected management marker: $managed_dir"
    fi
done
for unit in /etc/systemd/system/yifabao-media-certbot-renew.service /etc/systemd/system/yifabao-media-certbot-renew.timer; do
    [[ ! -L "$unit" ]] || die "Refusing symlink: $unit"
    if [[ -e "$unit" ]] && ! head -n 1 "$unit" | grep -Fxq "# $MARKER"; then
        die "Refusing unmanaged unit: $unit"
    fi
done
# Challenge files are public random probes, not credentials. Test exact HTTP
# response without following redirects. Remove only our own exclusive file.
python3 - "$webroot" "$PUBLIC_IP" <<'PY'
import os
from pathlib import Path
import secrets
import sys
from urllib.error import URLError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

root = Path(sys.argv[1]).resolve()
challenge = root / ".well-known" / "acme-challenge"
for component in (root / ".well-known", challenge):
    if component.is_symlink():
        raise SystemExit("ACME challenge directory is a symlink; review its ownership before continuing.")
    if not component.exists():
        component.mkdir(mode=0o755)
        component.chmod(0o755)
name = "yifabao-relay-preflight-" + secrets.token_hex(16)
path = challenge / name
body = secrets.token_hex(24).encode("ascii")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
os.fchmod(fd, 0o644)
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    url = f"http://{sys.argv[2]}/.well-known/acme-challenge/{name}"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(url, timeout=15) as response:
            if response.status != 200 or response.read(1024) != body:
                raise SystemExit("HTTP-01 probe did not return the expected file. Check the selected webroot and routing.")
    except (OSError, URLError):
        raise SystemExit("HTTP-01 probe failed. Check public port 80, IP routing, redirects/login, and .well-known rules. No vhost was changed.") from None
    print("Public-IP HTTP probe passed from this server. The CA still needs independent external reachability.")
finally:
    path.unlink(missing_ok=True)
PY
for managed_dir in "$VENV" "$CERT_ROOT"; do
    install -d -o root -g root -m 0700 "$managed_dir"
    if [[ ! -e "$managed_dir/.managed-by-yifabao" ]]; then
        printf '%s\n' "$MARKER" > "$managed_dir/.managed-by-yifabao"
    fi
done
for managed_path in "$CERT_ROOT/config" "$CERT_ROOT/work" "$CERT_ROOT/logs" "$CERT_ROOT/reload-nginx.sh"; do
    [[ ! -L "$managed_path" ]] || die "Refusing symlink: $managed_path"
done
python3 -m venv "$VENV"
# This changes only the isolated environment. No global pip/site packages.
PIP_CONFIG_FILE=/dev/null "$VENV/bin/python" -m pip --isolated install --index-url https://pypi.org/simple 'certbot==5.4.0'
install -d -o root -g root -m 0700 "$CERT_ROOT/config" "$CERT_ROOT/work" "$CERT_ROOT/logs"
contact_args=(--register-unsafely-without-email)
if [[ -n "$email" ]]; then
    contact_args=(--email "$email")
fi
(umask 022; "$VENV/bin/certbot" certonly --non-interactive --agree-tos \
    "${contact_args[@]}" --webroot --webroot-path "$webroot" \
    --ip-address "$PUBLIC_IP" --preferred-profile shortlived \
    --cert-name "yifabao-media-$PUBLIC_IP" \
    --config-dir "$CERT_ROOT/config" --work-dir "$CERT_ROOT/work" --logs-dir "$CERT_ROOT/logs")
# Install the renewal hook after issuance. Initial issuance does not reload
# the existing shared Nginx; the administrator applies the new 8443 vhost.
cat > "$CERT_ROOT/reload-nginx.sh" <<'HOOK'
#!/usr/bin/env bash
set -Eeuo pipefail
NGINX=/www/server/nginx/sbin/nginx
"$NGINX" -t
"$NGINX" -s reload
HOOK
chmod 0700 "$CERT_ROOT/reload-nginx.sh"
cat > /etc/systemd/system/yifabao-media-certbot-renew.service <<UNIT
# $MARKER
[Unit]
Description=Renew the Yifabao relay IP certificate
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$VENV/bin/certbot renew --non-interactive --quiet --config-dir $CERT_ROOT/config --work-dir $CERT_ROOT/work --logs-dir $CERT_ROOT/logs --deploy-hook $CERT_ROOT/reload-nginx.sh
Nice=10
CPUQuota=50%
MemoryMax=512M
UNIT
cat > /etc/systemd/system/yifabao-media-certbot-renew.timer <<UNIT
# $MARKER
[Unit]
Description=Check the Yifabao IP certificate twice daily

[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=30m
Persistent=true
Unit=yifabao-media-certbot-renew.service

[Install]
WantedBy=timers.target
UNIT
chmod 0644 /etc/systemd/system/yifabao-media-certbot-renew.service /etc/systemd/system/yifabao-media-certbot-renew.timer
systemctl daemon-reload
systemctl enable --now yifabao-media-certbot-renew.timer
printf '\nCertificate issued and twice-daily renewal checks enabled.\n'
printf 'No existing vhost was changed, and initial issuance did not reload Nginx.\n'
printf 'Next: review/apply nginx-https.conf.template on the independent port 8443.\n'
