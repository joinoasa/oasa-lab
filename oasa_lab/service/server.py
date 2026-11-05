"""`oasa-lab serve` and `oasa-lab token`."""

from __future__ import annotations

import argparse
import os
import sys


def serve(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="oasa-lab serve", description="Run the lab's remote job service (localhost only).")
    p.add_argument("--config", default=os.environ.get("OASA_LAB_CONFIG", "service.toml"), help="config file (default service.toml)")
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
    import time

    for t in cfg.tokens:   # expired tokens are refused at use; say so here too, so the owner can clean up
        left = (t.expires - time.time()) / 86400
        if left <= 0:
            print(f"oasa-lab: API token {t.name!r} ({t.user}) has expired; delete its [[tokens]] entry", file=sys.stderr)
        elif left < 7:
            print(f"oasa-lab: API token {t.name!r} ({t.user}) expires in {left:.1f} days", file=sys.stderr)
    if a.check:
        print(f"oasa-lab: config ok ({len(cfg.users)} user(s), {len(cfg.tokens)} API token(s), rf_setup={cfg.rf_setup}, "
              f"listening on {cfg.host}:{cfg.port})")
        return 0
    if cfg.rf_setup not in ("cabled", "shielded"):
        print(f"oasa-lab: rf_setup is {cfg.rf_setup!r}: suites that transmit will be refused.", file=sys.stderr)
    lab = Lab(cfg, config_path=a.config)  # SIGHUP re-reads users and tokens from it
    uvicorn.run(lab.app(), host=cfg.host, port=cfg.port, log_level="warning", proxy_headers=False,
                server_header=False, timeout_graceful_shutdown=15)
    return 0


def token(argv: list[str]) -> int:
    """Make an API token. The token is shown once; the config gets only its hash, its expiry and its role."""
    import datetime as dt

    from .auth import new_api_token
    from .config import DEFAULT_TOKEN_DAYS, MAX_TOKEN_DAYS, NAME_RE, ROLES

    p = argparse.ArgumentParser(prog="oasa-lab token", description="Make an API token for a user in service.toml.")
    p.add_argument("name", help="a label for the token, e.g. owner-laptop")
    p.add_argument("--user", required=True, help="the user (name in service.toml) the token acts as")
    p.add_argument("--days", type=int, default=DEFAULT_TOKEN_DAYS,
                   help=f"how long the token works (default {DEFAULT_TOKEN_DAYS}, at most {MAX_TOKEN_DAYS})")
    p.add_argument("--role", choices=ROLES,
                   help="give the token less than its user's role (agents: runner). It can never give more")
    a = p.parse_args(argv)
    if not 1 <= a.days <= MAX_TOKEN_DAYS:
        p.error(f"--days must be from 1 to {MAX_TOKEN_DAYS}")
    for label, v in (("name", a.name), ("--user", a.user)):
        if not NAME_RE.match(v):
            p.error(f"{label}: letters, digits, dots, dashes and underscores only")
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=a.days)).replace(microsecond=0)
    tok, digest = new_api_token()
    role = f"role    = \"{a.role}\"            # lowers the user's role for this token\n" if a.role else ""
    print(f"Token (shown once, keep it secret):\n  {tok}\n\nAdd to service.toml, then: sudo systemctl reload oasa-lab\n\n"
          f"[[tokens]]\nname    = \"{a.name}\"\nuser    = \"{a.user}\"\n{role}"
          f"expires = {expires.strftime('%Y-%m-%dT%H:%M:%SZ')}\nsha256  = \"{digest}\"")
    return 0
