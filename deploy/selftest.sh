#!/usr/bin/env bash
# Does the job path work on THIS machine? Run as root. Changes nothing that lasts (one empty results folder is
# cleared by the next job). Uses the real systemd, polkit and users, so it is the test that the unit tests cannot be.
#   1. the service user can start a job unit, and the unit runs the installed job-run as the job user
#   2. the service user cannot start or stop anything else
#   3. systemd really loaded the hardening the unit file promises
set -uo pipefail
. "$(dirname "$0")/lib.sh"
ok=0; bad=0
pass() { printf '  ok    %s\n' "$*"; ok=$((ok+1)); }
fail() { printf '  FAIL  %s\n' "$*"; bad=$((bad+1)); }
need_root
asservice() { runuser -u oasalab -- "$@"; }
ID=00000000000000000000000000005e1f           # 32 hex, no request in the spool: job-run must refuse it, cleanly
UNIT="oasa-lab-job@${ID}.service"
RES=/var/lib/oasa-lab/results
ERR="$(mktemp)"; trap 'rm -f "$ERR"' EXIT

echo "The service user and the job unit:"
if asservice systemctl --no-ask-password start --no-block "$UNIT" 2>"$ERR"; then pass "oasalab may start $UNIT"; else fail "oasalab could not start the job unit: $(cat "$ERR")"; fi
for _ in $(seq 1 60); do [ -s "$RES/$ID/exit.json" ] && break; sleep 0.5; done
if [ -s "$RES/$ID/exit.json" ]; then pass "the job wrote results/$ID/exit.json"; else fail "no exit.json after 30 s (journalctl -u '$UNIT')"; fi
[ "$(stat -c '%U' "$RES/$ID" 2>/dev/null)" = oasalab-job ] && pass "the results folder belongs to oasalab-job" || fail "results/$ID is not owned by oasalab-job"
asservice test -r "$RES/$ID/exit.json" && pass "the service can read the result" || fail "the service cannot read the result"
asservice test -w "$RES" && fail "the service can write the results folder" || pass "the service cannot write the results folder"
grep -q '"error"' "$RES/$ID/exit.json" && grep -qi 'request' "$RES/$ID/exit.json" && pass "a job without a request was refused" || fail "exit.json does not say the empty request was refused: $(cat "$RES/$ID/exit.json" 2>/dev/null)"

echo "The service user may not do anything else:"
for args in "start ssh.service" "restart oasa-lab-job@${ID}.service" "start oasa-lab-job@NOTHEX.service" "start oasa-lab-job@${ID}0.service" \
            "start oasa-lab-job@ABCDEF00000000000000000000000000.service" "stop cron.service" "kill oasa-lab-job@${ID}.service" "reset-failed"; do
  # shellcheck disable=SC2086
  if asservice systemctl --no-ask-password $args >/dev/null 2>&1; then fail "oasalab was allowed: systemctl $args"; else pass "refused: systemctl $args"; fi
done
if asservice systemd-run --no-ask-password --collect --unit=oasa-selftest-x true >/dev/null 2>&1; then fail "oasalab could start a transient unit"; else pass "refused: systemd-run (transient unit)"; fi

echo "What the installed unit says (systemctl show, so this is what systemd really loaded):"
for want in User=oasalab-job NoNewPrivileges=yes ProtectSystem=strict ProtectHome=yes PrivateTmp=yes ProtectProc=invisible \
            DevicePolicy=closed CapabilityBoundingSet= RemoveIPC=yes RestrictSUIDSGID=yes LockPersonality=yes KillMode=control-group; do
  key="${want%%=*}"; val="${want#*=}"
  got="$(systemctl show -p "$key" --value "$UNIT" 2>/dev/null)"
  [ "$got" = "$val" ] && pass "$key=$val" || fail "$key is '$got', expected '$val'"
done
systemctl show -p ExecStart --value "$UNIT" 2>/dev/null | grep -q 'path=/opt/oasa-lab/venv/bin/oasa-lab ' && pass "ExecStart is the root-owned installed program" || fail "ExecStart is not /opt/oasa-lab/venv/bin/oasa-lab"
for p in /opt/oasa-lab/venv/bin/oasa-lab /opt/oasa-lab/venv/lib; do
  [ "$(stat -c '%U' "$p")" = root ] && pass "$p is root-owned" || fail "$p is not root-owned"
done
for key in MemoryMax TasksMax RuntimeMaxSec; do
  got="$(systemctl show -p "$key" --value "$UNIT" 2>/dev/null)"
  case "$got" in ""|infinity|0|"[not set]") fail "$key is not limited ('$got')";; *) pass "$key=$got";; esac
done
if command -v systemd-analyze >/dev/null; then
  note "systemd-analyze security: $(systemd-analyze security "$UNIT" --no-pager 2>/dev/null | tail -1)"
fi

echo; echo "$ok ok, $bad failed"; [ "$bad" -eq 0 ]
