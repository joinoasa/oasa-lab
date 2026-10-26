#!/usr/bin/env bash
# One-time setup for the dev container. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."


# The oasa.toml tool: python3 .oasa/oasa_toml.py run --markdown
mkdir -p .oasa
if ! curl -fsSL https://raw.githubusercontent.com/joinoasa/website/main/tools/oasa-files/oasa_toml.py -o .oasa/oasa_toml.py; then
  echo "warning: could not download oasa_toml.py; get it later (see https://joinoasa.org/docs/oasa-files/#the-tool)" >&2
fi

# Everything else is [setup].steps in oasa.toml (venv, build, packages): the single source of truth.
if [ -f .oasa/oasa_toml.py ]; then python3 .oasa/oasa_toml.py setup; else echo "warning: run setup later: python3 .oasa/oasa_toml.py setup" >&2; fi
echo "Ready. Run the checks: python3 .oasa/oasa_toml.py run --markdown"
