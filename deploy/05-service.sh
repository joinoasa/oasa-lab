#!/usr/bin/env bash
# Install `oasa-lab serve` as a systemd service under its own unprivileged user. Jobs run as a second unprivileged
# user in their own hardened unit, which the service may start and stop (and nothing else) through polkit.
# No OASA program runs as root. Run again to upgrade (set LAB_REF to the new commit first).
. "$(dirname "$0")/lib.sh"
need_root; check_os

SHA="$(resolve_ref "$LAB_REF")"
say "Installing oasa-lab commit $SHA"
here_sha="$(git -C "$HERE" rev-parse HEAD 2>/dev/null || true)"
if [ -n "$here_sha" ] && [ "$here_sha" != "$SHA" ]; then
  note "warning: these scripts are from commit ${here_sha:0:12} but LAB_REF is ${SHA:0:12}."
  note "The dependency lists (requirements.lock) come from this checkout. Check out $SHA first: git -C '$HERE' checkout $SHA"
fi
[[ "$LAB_BRANCH" =~ ^[A-Za-z0-9._/-]{1,100}$ && "$LAB_BRANCH" != -* ]] || die "LAB_BRANCH='$LAB_BRANCH' is not a plain branch name"
command -v pkaction >/dev/null || die "polkit is missing: run 01-packages.sh first"

say "Users and folders"
# oasalab runs the service (state, tokens, the job queue). oasalab-job runs the tests and may touch the radios.
# oasalab-mirror updates the git mirror. No one of them can write what another one relies on.
for u in oasalab oasalab-job oasalab-mirror; do
  if ! id "$u" >/dev/null 2>&1; then
    home=/nonexistent; [ "$u" = oasalab ] && home=/var/lib/oasa-lab/state
    run useradd --system --user-group --no-create-home --home-dir "$home" --shell /usr/sbin/nologin "$u"
  fi
done
# /var/lib/oasa-lab belongs to root, so the service cannot rename or swap the folders below it.
run install -d -m 0755 -o root -g root /var/lib/oasa-lab
run install -d -m 0750 -o oasalab -g oasalab /var/lib/oasa-lab/state
# Upgrade from the first layout (state straight in /var/lib/oasa-lab): move it, once.
if [ "$DRY_RUN" != 1 ] && [ -e /var/lib/oasa-lab/jobs -o -e /var/lib/oasa-lab/oauth.json ] && [ -z "$(ls -A /var/lib/oasa-lab/state)" ]; then
  note "moving the old state into /var/lib/oasa-lab/state"
  for f in jobs git oauth.json audit.jsonl PAUSE LOCAL_PAUSE; do [ -e "/var/lib/oasa-lab/$f" ] && mv "/var/lib/oasa-lab/$f" /var/lib/oasa-lab/state/; done
  chown -R oasalab:oasalab /var/lib/oasa-lab/state
fi
# spool: the service writes requests (owner), the job user reads them (group, setgid so new files get that group).
run install -d -m 2750 -o oasalab -g oasalab-job /var/lib/oasa-lab/spool
# results: the job user writes (owner), the service reads (group, setgid). The service cannot write here.
run install -d -m 2750 -o oasalab-job -g oasalab /var/lib/oasa-lab/results
# mirror: written only by oasalab-mirror; everyone can read.
run install -d -m 0755 -o oasalab-mirror -g oasalab-mirror /var/lib/oasa-lab/mirror.git
# /etc/oasa-lab is listable by all and holds root-only secrets by file mode: the policy (0644) must be readable by the job user.
run install -d -m 0755 -o root -g root /etc/oasa-lab
run install -d -m 0755 -o root -g root /etc/oasa-lab.d
run install -d -m 0755 -o root -g root /opt/oasa-lab

say "Program (a virtual environment owned by root, so no job or service user can change the code)"
VENV=/opt/oasa-lab/venv
[ -x "$VENV/bin/python" ] || run python3 -m venv "$VENV"
# Every dependency is pinned with hashes (requirements.lock, made with: uv pip compile ... --generate-hashes).
# pip refuses anything that does not match. The venv's own pip is left as it is, not upgraded from the internet.
run "$VENV/bin/pip" install --quiet --require-hashes --no-deps -r "$HERE/requirements-build.lock"
run "$VENV/bin/pip" install --quiet --require-hashes --no-deps -r "$HERE/requirements.lock"
# The program itself, at the exact commit. A commit id is its own checksum.
run "$VENV/bin/pip" install --quiet --no-deps --no-build-isolation --force-reinstall "oasa-lab @ git+${LAB_REPO_URL}@${SHA}"
run "$VENV/bin/pip" check

say "Config (kept if it already exists)"
[ -f /etc/oasa-lab/service.toml ] || run install -m 0640 -o root -g oasalab "$HERE/service.toml.example" /etc/oasa-lab/service.toml
# The lab policy: the owner's word on rf_setup, wiring, the device file. Root-owned. The service and the job side both read it.
[ -f /etc/oasa-lab/lab-policy.toml ] || run install -m 0644 -o root -g root "$HERE/lab-policy.toml.example" /etc/oasa-lab/lab-policy.toml
# The device file the jobs read: serials, ports, cable losses. Not secret.
[ -f /etc/oasa-lab.d/lab.toml ] || run install -m 0644 -o root -g root "$HERE/../lab.example.toml" /etc/oasa-lab.d/lab.toml
# The env file holds the GitHub client secret. systemd reads it as root, so the service user need not.
[ -f /etc/oasa-lab/env ] || run install -m 0600 -o root -g root "$HERE/.env.example" /etc/oasa-lab/env
# git trusts the mirror (it is owned by another user) through this root-owned file and nothing else.
write_file /etc/oasa-lab.d/gitconfig 0644 root:root <<'CFG'
[safe]
	directory = /var/lib/oasa-lab/mirror.git
CFG
write_file /etc/oasa-lab.d/mirror.env 0644 root:root <<MENV
LAB_REPO_URL=${LAB_REPO_URL}
LAB_BRANCH=${LAB_BRANCH}
MENV

say "systemd units and the polkit rule"
write_file /etc/systemd/system/oasa-lab.service 0644 root:root < "$HERE/systemd/oasa-lab.service"
write_file '/etc/systemd/system/oasa-lab-job@.service' 0644 root:root < "$HERE/systemd/oasa-lab-job@.service"
write_file /etc/systemd/system/oasa-lab-mirror.service 0644 root:root < "$HERE/systemd/oasa-lab-mirror.service"
write_file /etc/systemd/system/oasa-lab-mirror.timer 0644 root:root < "$HERE/systemd/oasa-lab-mirror.timer"
# The first layout had a root helper behind a socket. It is gone; remove its units if they are still here.
for old in oasa-lab-jobrun.socket 'oasa-lab-jobrun@.service'; do
  if [ -e "/etc/systemd/system/$old" ]; then run systemctl disable --now "$old" || true; run rm -f "/etc/systemd/system/$old"; fi
done
# polkit watches this folder and reads the new rule at once (no restart).
write_file /etc/polkit-1/rules.d/50-oasa-lab.rules 0644 root:root < "$HERE/polkit/50-oasa-lab.rules"
run systemctl daemon-reload
if [ ! -e /var/lib/oasa-lab/mirror.git/HEAD ]; then
  run sudo -u oasalab-mirror git init --bare -q -b "$LAB_BRANCH" /var/lib/oasa-lab/mirror.git
fi
run systemctl enable --now oasa-lab-mirror.timer
run systemctl start oasa-lab-mirror.service
run systemctl enable oasa-lab

if [ "$DRY_RUN" = 1 ]; then exit 0; fi
if grep -q 'replace-me' /etc/oasa-lab/env || grep -q 'example.com' /etc/oasa-lab/service.toml || grep -q 'replace-with' /etc/oasa-lab/service.toml; then
  say "Not started yet: the config still has placeholders"
  owner "Edit /etc/oasa-lab/service.toml (your hostnames, GitHub client id, USB ids), /etc/oasa-lab/lab-policy.toml (rf_setup and wiring, when the bench is ready), /etc/oasa-lab.d/lab.toml (serials, ports, cabling losses) and /etc/oasa-lab/env (GitHub client secret). Then:  sudo ./05-service.sh"
  exit 0
fi
sudo -u oasalab -g oasalab "$VENV/bin/oasa-lab" serve --check --config /etc/oasa-lab/service.toml
say "Does the job path work here? (starts a job unit as the service user and checks what it can and cannot do)"
if "$HERE/selftest.sh"; then note "job self-test passed"; else die "job self-test failed: see above. The service was not restarted."; fi
run systemctl restart oasa-lab
say "Started"
note "Logs: journalctl -u oasa-lab -f      Audit log: /var/lib/oasa-lab/state/audit.jsonl"
note "Pause the lab from this PC, whatever the service says:  sudo touch /etc/oasa-lab/PAUSE   (remove the file to resume)"
note "Next: sudo ./06-cloudflared.sh"
