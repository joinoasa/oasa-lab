#!/usr/bin/env bash
# One-time setup for the dev container. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
# pyserial: l0-lab tests the real serial driver against a pseudo-terminal.
# [service]: the job service tests. The hardware extra (hw) is for the lab PC only.
.venv/bin/pip install --quiet -e '.[service]' pyserial

# The oasa.toml tool: python3 .oasa/oasa_toml.py run --markdown
mkdir -p .oasa
if ! curl -fsSL https://raw.githubusercontent.com/joinoasa/website/main/tools/oasa-files/oasa_toml.py -o .oasa/oasa_toml.py; then
  echo "warning: could not download oasa_toml.py; get it later (see https://joinoasa.org/docs/oasa-files/#the-tool)" >&2
fi

echo "Ready. Run the checks: python3 .oasa/oasa_toml.py run --markdown"
