#!/usr/bin/env bash
# Read-only checks of the lab PC. Run any time; fails loudly. No changes are made.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/setup.env" ] && . "$HERE/setup.env"
LAB_BRANCH="${LAB_BRANCH:-main}"
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

echo "Job path (no OASA program runs as root):"
for u in oasalab oasalab-job oasalab-mirror; do id "$u" >/dev/null 2>&1 && pass "user $u exists" || fail "no user $u (run 05-service.sh)"; done
[ "$(id -u oasalab-job 2>/dev/null)" != "$(id -u oasalab 2>/dev/null)" ] && pass "jobs and the service are different users" || fail "jobs run as the service user"
[ "$(stat -c '%U:%a' /var/lib/oasa-lab 2>/dev/null)" = "root:755" ] && pass "/var/lib/oasa-lab is root-owned (the service cannot swap the folders below it)" || fail "/var/lib/oasa-lab is not root:755"
[ "$(stat -c '%U:%G:%a' /var/lib/oasa-lab/spool 2>/dev/null)" = "oasalab:oasalab-job:2750" ] && pass "spool: service writes, job user reads" || fail "spool is not oasalab:oasalab-job 2750"
[ "$(stat -c '%U:%G:%a' /var/lib/oasa-lab/results 2>/dev/null)" = "oasalab-job:oasalab:2750" ] && pass "results: job user writes, service reads" || fail "results is not oasalab-job:oasalab 2750"
[ "$(stat -c '%U' /var/lib/oasa-lab/mirror.git 2>/dev/null)" = "oasalab-mirror" ] && pass "the git mirror belongs to oasalab-mirror (not the service, not the job user)" || fail "the git mirror is not owned by oasalab-mirror"
systemctl is-active --quiet oasa-lab-mirror.timer && pass "the mirror timer is active" || fail "oasa-lab-mirror.timer is not active"
git --git-dir=/var/lib/oasa-lab/mirror.git rev-parse --verify -q "refs/heads/$LAB_BRANCH" >/dev/null 2>&1 && pass "the mirror has $LAB_BRANCH" || fail "the mirror has no $LAB_BRANCH yet (sudo systemctl start oasa-lab-mirror.service)"
[ -f '/etc/systemd/system/oasa-lab-job@.service' ] && pass "the job unit is installed" || fail "no oasa-lab-job@.service"
[ -f /etc/polkit-1/rules.d/50-oasa-lab.rules ] && cmp -s /etc/polkit-1/rules.d/50-oasa-lab.rules "$HERE/polkit/50-oasa-lab.rules" && pass "the polkit rule is installed and matches this checkout" || fail "the polkit rule is missing or differs from deploy/polkit/50-oasa-lab.rules"
[ ! -e /run/oasa-lab/jobrun.sock ] && pass "no root helper socket" || fail "the old root helper socket is still there (sudo systemctl disable --now oasa-lab-jobrun.socket)"
[ "$(stat -c '%U:%G:%a' /etc/oasa-lab/lab-policy.toml 2>/dev/null)" = "root:root:644" ] && pass "the lab policy is root-owned and not writable by others" || fail "/etc/oasa-lab/lab-policy.toml is not root:root 644"
[ "$(stat -c '%U:%G:%a' /etc/oasa-lab/service.toml 2>/dev/null)" = "root:oasalab:640" ] && pass "service.toml is root-owned (the service cannot edit its own config)" || fail "/etc/oasa-lab/service.toml is not root:oasalab 640"
[ "$(stat -c '%U:%a' /etc/oasa-lab/env 2>/dev/null)" = "root:600" ] && pass "the secrets file is root-only" || fail "/etc/oasa-lab/env is not root:600"
[ "$(stat -c '%U' /opt/oasa-lab/venv/bin/python 2>/dev/null)" = "root" ] && pass "the program is root-owned" || fail "the program is not root-owned"
grep -q '^mode *= *"systemd"' /etc/oasa-lab/service.toml && pass "sandbox mode is systemd" || fail "sandbox mode is not systemd"
"$HERE/selftest.sh" >/dev/null 2>&1 && pass "job self-test passed (polkit allows only job units, the unit is hardened as promised)" || fail "job self-test failed: run it by hand to see why: sudo $HERE/selftest.sh"
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
