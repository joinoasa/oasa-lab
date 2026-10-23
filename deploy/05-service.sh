#!/usr/bin/env bash
# Install `oasa-lab serve` as a systemd service under its own unprivileged user. Run again to upgrade.
. "$(dirname "$0")/lib.sh"
need_root; check_os

say "User and folders"
if ! id oasalab >/dev/null 2>&1; then
  run useradd --system --user-group --home-dir /var/lib/oasa-lab --shell /usr/sbin/nologin oasalab
fi
for g in plugdev dialout; do
  getent group "$g" >/dev/null && run usermod -aG "$g" oasalab
done
run install -d -m 0750 -o oasalab -g oasalab /var/lib/oasa-lab
run install -d -m 0750 -o root -g oasalab /etc/oasa-lab
run install -d -m 0755 -o root -g root /opt/oasa-lab

say "Program (a virtual environment owned by root, so the service cannot change its own code)"
[ -x /opt/oasa-lab/venv/bin/python ] || run python3 -m venv /opt/oasa-lab/venv
run /opt/oasa-lab/venv/bin/pip install --quiet --upgrade pip
run /opt/oasa-lab/venv/bin/pip install --quiet --upgrade "oasa-lab[service] @ git+${LAB_REPO_URL}@${LAB_REF}"

say "Config (kept if it already exists)"
[ -f /etc/oasa-lab/service.toml ] || run install -m 0640 -o root -g oasalab "$HERE/service.toml.example" /etc/oasa-lab/service.toml
# The device file is the hardware suites' lab.toml (serials, ports, cabling). Jobs read it through OASA_LAB_INVENTORY.
[ -f /etc/oasa-lab/lab.toml ] || run install -m 0640 -o root -g oasalab "$HERE/../lab.example.toml" /etc/oasa-lab/lab.toml
# The env file holds the GitHub client secret. systemd reads it as root, so the service user need not.
[ -f /etc/oasa-lab/env ] || run install -m 0600 -o root -g root "$HERE/.env.example" /etc/oasa-lab/env

say "systemd unit"
write_file /etc/systemd/system/oasa-lab.service 0644 root:root < "$HERE/systemd/oasa-lab.service"
run systemctl daemon-reload
run systemctl enable oasa-lab

if [ "$DRY_RUN" = 1 ]; then exit 0; fi
if grep -q 'replace-me' /etc/oasa-lab/env || grep -q 'example.com' /etc/oasa-lab/service.toml || grep -q 'replace-with' /etc/oasa-lab/service.toml; then
  say "Not started yet: the config still has placeholders"
  owner "Edit /etc/oasa-lab/service.toml (your hostnames, GitHub client id, USB ids), /etc/oasa-lab/lab.toml (serials, ports, cabling losses) and /etc/oasa-lab/env (GitHub client secret). Then:  sudo systemctl restart oasa-lab"
  exit 0
fi
sudo -u oasalab -g oasalab /opt/oasa-lab/venv/bin/oasa-lab serve --check --config /etc/oasa-lab/service.toml
run systemctl restart oasa-lab
say "Started"
note "Logs: journalctl -u oasa-lab -f      Audit log: /var/lib/oasa-lab/audit.jsonl"
note "Next: sudo ./06-cloudflared.sh"
