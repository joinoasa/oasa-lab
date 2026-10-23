"""`oasa-lab serve` and `oasa-lab token`."""

from __future__ import annotations

import argparse
import os
import sys


def serve(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="oasa-lab serve", description="Run the lab's remote job service (localhost only).")
    p.add_argument("--config", default=os.environ.get("OASA_LAB_CONFIG", "lab.toml"), help="config file (default lab.toml)")
    p.add_argument("--check", action="store_true", help="check the config and exit")
    a = p.parse_args(argv)
    try:
        import uvicorn

        from .app import Lab
        from .config import ConfigError, load_config
    except ImportError as exc:
        print(f"oasa-lab: the service needs its extra packages: pip install 'oasa-lab[service]' ({exc})", file=sys.stderr)
        return 2
    try:
        cfg = load_config(a.config)
    except ConfigError as exc:
        print(f"oasa-lab: {exc}", file=sys.stderr)
        return 2
    if a.check:
        print(f"oasa-lab: config ok ({len(cfg.users)} user(s), rf_setup={cfg.rf_setup}, listening on {cfg.host}:{cfg.port})")
        return 0
    if cfg.rf_setup not in ("cabled", "shielded"):
        print(f"oasa-lab: rf_setup is {cfg.rf_setup!r}: suites that transmit will be refused.", file=sys.stderr)
    lab = Lab(cfg)
    uvicorn.run(lab.app(), host=cfg.host, port=cfg.port, log_level="warning", proxy_headers=False,
                server_header=False, timeout_graceful_shutdown=15)
    return 0


def token(argv: list[str]) -> int:
    """Make an API token. The token is shown once; the config gets only its hash."""
    from .auth import new_api_token

    p = argparse.ArgumentParser(prog="oasa-lab token", description="Make an API token for a user in lab.toml.")
    p.add_argument("name", help="a label for the token, e.g. owner-laptop")
    p.add_argument("--user", required=True, help="the user (name in lab.toml) the token acts as")
    a = p.parse_args(argv)
    tok, digest = new_api_token()
    print(f"Token (shown once, keep it secret):\n  {tok}\n\nAdd to lab.toml:\n\n[[tokens]]\nname   = \"{a.name}\"\nuser   = \"{a.user}\"\nsha256 = \"{digest}\"")
    return 0
