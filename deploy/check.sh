#!/usr/bin/env bash
# Read-only checks of the lab PC. Run any time; fails loudly. No changes are made.
set -uo pipefail
ok=0; bad=0
pass() { printf '  ok    %s\n' "$*"; ok=$((ok+1)); }
fail() { printf '  FAIL  %s\n' "$*"; bad=$((bad+1)); }

echo "Listening sockets that are not loopback:"
exposed="$(ss -H -ltnu 2>/dev/null | awk '{print $5}' | grep -Ev '^(127\.|\[::1\]|::1|\[::ffff:127\.)' | grep -Ev '^(\*|0\.0\.0\.0|\[::\]):(68|51820)$' || true)"
if [ -z "$exposed" ]; then pass "nothing listens on a network address (except DHCP client and WireGuard)"; else fail "listening beyond loopback: $(echo "$exposed" | tr '\n' ' ')"; fi

echo "SDR control ports (must be absent or loopback-only):"
for p in 30431 1234 55132; do
  if ss -H -ltn "sport = :$p" 2>/dev/null | awk '{print $4}' | grep -Evq '^(127\.|\[::1\])' ; then fail "port $p is open to the network"; else pass "port $p not exposed"; fi
done

echo "Job service:"
systemctl is-active --quiet oasa-lab && pass "oasa-lab is running" || fail "oasa-lab is not running"
ss -H -ltn "sport = :8742" 2>/dev/null | awk '{print $4}' | grep -q '^127\.0\.0\.1:8742$' && pass "listens on 127.0.0.1:8742 only" || fail "not listening on 127.0.0.1:8742"
[ "$(ps -o user= -C oasa-lab 2>/dev/null | sort -u | tr -d ' ')" = "oasalab" ] && pass "runs as the oasalab user" || fail "not running as oasalab"
curl -fsS --max-time 3 http://127.0.0.1:8742/healthz >/dev/null && pass "healthz answers" || fail "healthz does not answer"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8742/api/lab)"
[ "$code" = 401 ] && pass "API without a token is refused (401)" || fail "API without a token returned $code"

echo "Job sandbox:"
id oasalab-job >/dev/null 2>&1 && pass "the job user oasalab-job exists" || fail "no oasalab-job user (run 05-service.sh)"
[ "$(id -u oasalab-job 2>/dev/null)" != "$(id -u oasalab 2>/dev/null)" ] && pass "jobs and the service are different users" || fail "jobs run as the service user"
systemctl is-active --quiet oasa-lab-jobrun.socket && pass "the job helper socket is listening" || fail "oasa-lab-jobrun.socket is not active"
[ "$(stat -c '%U:%a' /run/oasa-lab/jobrun.sock 2>/dev/null)" = "oasalab:600" ] && pass "only oasalab can open the helper socket" || fail "helper socket permissions are not oasalab:600"
[ "$(stat -c '%U:%G:%a' /etc/oasa-lab 2>/dev/null)" = "root:oasalab:750" ] && pass "/etc/oasa-lab is root-owned (the service cannot edit its own config)" || fail "/etc/oasa-lab is not root:oasalab 750"
[ "$(stat -c '%U' /opt/oasa-lab/venv/bin/python 2>/dev/null)" = "root" ] && pass "the program is root-owned" || fail "the program is not root-owned"
grep -q '^mode *= *"systemd"' /etc/oasa-lab/service.toml && pass "sandbox mode is systemd" || fail "sandbox mode is not systemd"
/opt/oasa-lab/venv/bin/python -I -m oasa_lab.service.jobrun selftest >/dev/null 2>&1 && pass "sandbox self-test passed (other user, no access to the service's files, read-only code)" || fail "sandbox self-test failed: run it by hand to see why: sudo /opt/oasa-lab/venv/bin/python -I -m oasa_lab.service.jobrun selftest"
[ -e /etc/oasa-lab/PAUSE ] && note_paused="  (note: the lab is paused by /etc/oasa-lab/PAUSE)" || note_paused=""
echo "    pause file: /etc/oasa-lab/PAUSE${note_paused}"

echo "Firewall:"
nft list ruleset 2>/dev/null | grep -q 'policy drop' && pass "nftables input policy is drop" || fail "no default-deny firewall loaded"
[ "$(sysctl -n net.ipv4.ip_forward)" = 0 ] && pass "IP forwarding is off" || fail "IP forwarding is on"

echo "Tunnel:"
systemctl is-active --quiet cloudflared && pass "cloudflared is running" || fail "cloudflared is not running"
if [ -f /etc/cloudflared/config.yml ]; then
  grep -Eq '(30431|1234|55132)' /etc/cloudflared/config.yml && fail "tunnel config mentions an SDR port" || pass "tunnel config mentions no SDR port"
fi

echo; echo "$ok ok, $bad failed"; [ "$bad" -eq 0 ]
