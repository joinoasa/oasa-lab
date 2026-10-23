#!/usr/bin/env bash
# Install `oasa-lab serve` as a systemd service under its own unprivileged user, with jobs running as a second
# unprivileged user through a small root helper. Run again to upgrade (set LAB_REF to the new commit first).
. "$(dirname "$0")/lib.sh"
need_root; check_os

SHA="$(resolve_ref "$LAB_REF")"
say "Installing oasa-lab commit $SHA"
here_sha="$(git -C "$HERE" rev-parse HEAD 2>/dev/null || true)"
if [ -n "$here_sha" ] && [ "$here_sha" != "$SHA" ]; then
  note "warning: these scripts are from commit ${here_sha:0:12} but LAB_REF is ${SHA:0:12}."
  note "The dependency lists (requirements.lock) come from this checkout. Check out $SHA first: git -C '$HERE' checkout $SHA"
fi

say "Users and folders"
# oasalab runs the service (state, tokens, the job queue). oasalab-job runs the tests and may touch the radios.
# The two never share files: the job user cannot read the service's secrets or other jobs, and cannot edit evidence.
if ! id oasalab >/dev/null 2>&1; then
  run useradd --system --user-group --home-dir /var/lib/oasa-lab --shell /usr/sbin/nologin oasalab
fi
if ! id oasalab-job >/dev/null 2>&1; then
  run useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin oasalab-job
fi
for g in plugdev dialout; do
  getent group "$g" >/dev/null && run usermod -aG "$g" oasalab-job
done
run install -d -m 0750 -o oasalab -g oasalab /var/lib/oasa-lab
run install -d -m 0750 -o root -g oasalab /etc/oasa-lab
run install -d -m 0755 -o root -g root /etc/oasa-lab.d
run install -d -m 0755 -o root -g root /opt/oasa-lab

say "Program (a virtual environment owned by root, so neither user can change the code)"
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
# The device file the jobs read: serials, ports, cable losses. Not secret; it lives outside /etc/oasa-lab, which jobs cannot see.
[ -f /etc/oasa-lab.d/lab.toml ] || run install -m 0644 -o root -g root "$HERE/../lab.example.toml" /etc/oasa-lab.d/lab.toml
# The env file holds the GitHub client secret. systemd reads it as root, so the service user need not.
[ -f /etc/oasa-lab/env ] || run install -m 0600 -o root -g root "$HERE/.env.example" /etc/oasa-lab/env

say "systemd units (the service, and the helper that starts jobs as the job user)"
write_file /etc/systemd/system/oasa-lab.service 0644 root:root < "$HERE/systemd/oasa-lab.service"
write_file /etc/systemd/system/oasa-lab-jobrun.socket 0644 root:root < "$HERE/systemd/oasa-lab-jobrun.socket"
write_file '/etc/systemd/system/oasa-lab-jobrun@.service' 0644 root:root < "$HERE/systemd/oasa-lab-jobrun@.service"
run systemctl daemon-reload
run systemctl enable oasa-lab oasa-lab-jobrun.socket
run systemctl start oasa-lab-jobrun.socket

if [ "$DRY_RUN" = 1 ]; then exit 0; fi
if grep -q 'replace-me' /etc/oasa-lab/env || grep -q 'example.com' /etc/oasa-lab/service.toml || grep -q 'replace-with' /etc/oasa-lab/service.toml; then
  say "Not started yet: the config still has placeholders"
  owner "Edit /etc/oasa-lab/service.toml (your hostnames, GitHub client id, USB ids), /etc/oasa-lab.d/lab.toml (serials, ports, cabling losses) and /etc/oasa-lab/env (GitHub client secret). Then:  sudo systemctl restart oasa-lab"
  exit 0
fi
sudo -u oasalab -g oasalab "$VENV/bin/oasa-lab" serve --check --config /etc/oasa-lab/service.toml
say "Does the job sandbox work here? (runs a harmless command as the job user in its own unit)"
if "$VENV/bin/python" -I -m oasa_lab.service.jobrun selftest; then note "sandbox self-test passed"; else die "sandbox self-test failed: see above. The service was not restarted."; fi
run systemctl restart oasa-lab
say "Started"
note "Logs: journalctl -u oasa-lab -f      Audit log: /var/lib/oasa-lab/audit.jsonl"
note "Pause the lab from this PC, whatever the service says:  sudo touch /etc/oasa-lab/PAUSE   (remove the file to resume)"
note "Next: sudo ./06-cloudflared.sh"
