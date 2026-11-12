#!/usr/bin/env bash
# Read-only inspection: does not install software or modify configuration.
set -Eeuo pipefail
export LC_ALL=C
readonly NGINX=/www/server/nginx/sbin/nginx
readonly PUBLIC_IP="${MEDIA_RELAY_PUBLIC_IP:?Set MEDIA_RELAY_PUBLIC_IP to this server public IPv4 address}"
failed=0
printf 'Media relay preflight (read-only)\n'
if [[ -r /etc/os-release ]]; then
    awk -F= '$1 == "PRETTY_NAME" { print "OS: " $2 }' /etc/os-release
else
    printf 'FAIL: /etc/os-release is unavailable. Linux is required.\n'
    failed=1
fi
if [[ ${EUID} -ne 0 ]]; then
    printf 'FAIL: run as root so the existing Nginx configuration can be inspected.\n'
    failed=1
fi
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c 'import sys; print("Python: " + sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        printf 'FAIL: Python 3.10+ is required by the isolated certificate tool.\n'
        failed=1
    fi
else
    printf 'FAIL: python3 is missing.\n'
    failed=1
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
    printf 'FAIL: a running systemd installation is required.\n'
    failed=1
else
    printf 'systemd: available\n'
fi
if [[ ! -x "$NGINX" ]]; then
    printf 'FAIL: Baota Nginx not found at %s\n' "$NGINX"
    failed=1
fi
printf '\nListening TCP ports 80, 443, 8443, 18741 (existing listeners are not changed):\n'
if command -v ss >/dev/null 2>&1; then
    ss -ltnp '( sport = :80 or sport = :443 or sport = :8443 or sport = :18741 )'
else
    printf 'WARN: ss is missing; inspect these ports in the panel manually.\n'
fi
if [[ -x "$NGINX" ]] && command -v python3 >/dev/null 2>&1; then
    if ! python3 - "$NGINX" "$PUBLIC_IP" <<'PY'
import re
import subprocess
import sys

result = subprocess.run([sys.argv[1], "-T"], capture_output=True, text=True)
if result.returncode:
    print("FAIL: Nginx configuration could not be read/validated. Run nginx -t locally to investigate.")
    print("Configuration/error text is intentionally not printed by this preflight.")
    raise SystemExit(1)
source = "<unknown>"
stack = []
directive = []
servers = []
current = None
# Parse only structure, then emit listen/server_name from server blocks.
# Never print the full configuration, headers, credentials, or certificate keys.
for line in result.stdout.splitlines():
    marker = re.match(r"# configuration file (.*):$", line)
    if marker:
        source = marker.group(1)
        stack = []
        directive = []
        current = None
        continue
    tokens = re.findall(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s{};#]+|[{};]|\#.*$''', line)
    for token in tokens:
        if token.startswith("#"):
            break
        if token == "{":
            kind = directive[0] if directive else "?"
            stack.append(kind)
            if kind == "server":
                current = {"source": source, "depth": len(stack), "listen": [], "names": []}
                servers.append(current)
            directive = []
        elif token == "}":
            if current is not None and current["depth"] == len(stack):
                current = None
            if stack:
                stack.pop()
            directive = []
        elif token == ";":
            if current is not None and len(stack) == current["depth"] and directive:
                if directive[0] == "listen":
                    current["listen"].append(" ".join(directive[1:]))
                elif directive[0] == "server_name":
                    current["names"].extend(directive[1:])
            directive = []
        else:
            directive.append(token)
print("\nExisting Nginx virtual hosts (listen/server_name only):")
if not servers:
    print("WARN: no server blocks could be identified. This does not mean the ports are free; review all included files in the panel.")
for item in servers:
    print(f"  {item['source']}: listen={item['listen'] or ['80 (implicit)']}; server_name={item['names']}")
    if any("default_server" in value and re.search(r"(?:^|:)443(?:\s|$)", value) for value in item["listen"]):
        print("    INFO: existing default_server on 443 is preserved; the relay uses 8443.")
    if sys.argv[2] in item["names"]:
        print("    INFO: existing server_name matches the public IP. Preserve its existing 80/443 configuration.")
    if any(re.search(r"(?:^|:)8443(?:\s|$)", value) for value in item["listen"]):
        print("    ACTION: port 8443 already has a vhost. Verify ownership; do not add a duplicate listener.")
print("Choose an existing port-80 website webroot for the HTTP-01 challenge.")
print("Redirects, login rules, or .well-known restrictions can block validation and require administrator review.")
print("The HTTP-only template is only for a server with no existing port-80 listener.")
print("This does not test Alibaba Cloud firewall rules or reachability from the model provider.")
PY
    then
        failed=1
    fi
fi
if [[ "$failed" -ne 0 ]]; then
    printf '\nPreflight failed. Resolve the FAIL items before installing.\n'
    exit 1
fi
printf '\nBase prerequisites passed. Review ACTION/WARN lines before changing any website.\n'
