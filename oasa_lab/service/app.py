"""Put the pieces together: one ASGI app with the connector, sign-in, REST API and job queue."""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse

from .api import build_routes
from .audit import AuditLog, AuditMiddleware
from .auth import Authenticator
from .checkout import Checkouts
from ..policy import installed_commit
from .config import Config, ConfigError, load_config
from .inventory import Inventory, make_inventory
from .jobs import JobManager
from .mcp_server import build_mcp
from .oauth import LabOAuthProvider


class HostGuard:
    """Refuse requests for a hostname that is not ours (DNS rebinding). Applies to every route."""

    def __init__(self, app, hosts: set[str]):
        self.app, self.hosts = app, hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1").lower()
            if host not in self.hosts:
                await PlainTextResponse("unknown host", 421)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def allowed_hosts(cfg: Config) -> set[str]:
    hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", f"[::1]:{cfg.port}"}
    for u in (cfg.public_url, cfg.api_public_url):
        if u:
            hosts.add(urlparse(u).netloc.lower())
    hosts.update(h.lower() for h in cfg.allowed_hosts)
    return hosts


class Lab:
    """Everything the app is built from. Tests reach into it."""

    def __init__(self, cfg: Config, inventory: Inventory | None = None, http=None, config_path: Path | str | None = None):
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.config_path = config_path
        self.audit = AuditLog(cfg.state_dir / "audit.jsonl")
        self.provider = LabOAuthProvider(cfg, self.audit, http)
        self.inventory = inventory or make_inventory(cfg)
        pol = cfg.policy   # systemd mode: the git mirror belongs to root's timer; we only read it
        self.checkouts = Checkouts(cfg.repo, cfg.branch, cfg.state_dir / "git", cfg.min_commit or installed_commit() or "",
                                  mirror=Path(pol.mirror) if pol else None, git_config=pol.git_config if pol else None)
        self.jobs = JobManager(cfg, self.checkouts, self.inventory, self.audit)
        self.checkouts.refresh = getattr(self.jobs.sandbox, "refresh_mirror", None)
        self.auth = Authenticator(cfg, self.provider.lookup, self.audit)
        self.mcp = build_mcp(cfg, self.jobs, self.inventory, self.audit, self.provider)

    # Fields `reload` takes from the file. Everything else needs a restart (it is checked, and the audit line names it).
    RELOADED = ("users", "tokens")

    def reload(self) -> bool:
        """Re-read [[users]] and [[tokens]] from the config file (SIGHUP, `systemctl reload oasa-lab`). The queue, the
        running job and OAuth sessions stay; sessions of a user who is no longer listed are revoked. A config that
        does not load changes nothing: the old users and tokens stay, and the error goes to the audit log."""
        if not self.config_path:
            self.audit.write(kind="service", event="reload_failed", error="no config file to reload from")
            return False
        try:
            new = load_config(self.config_path)
        except (ConfigError, OSError) as exc:   # our own messages: they name keys and entries, never secrets
            return self._reload_failed(str(exc))
        except Exception as exc:   # e.g. int("abc") from a typo; its text may quote a value, so give only the type
            return self._reload_failed(f"{type(exc).__name__} while reading the config (a value of the wrong type?)")
        cfg = self.cfg
        restart_needed = sorted(f.name for f in fields(Config)
                                if f.name not in self.RELOADED and getattr(new, f.name) != getattr(cfg, f.name))
        # A user is "gone" when the name is removed, or kept but now names another person (GitHub id or email
        # changed): sessions signed in as the old identity must not carry over to the new one.
        same = {(u.name, u.github_id, (u.email or "").lower()) for u in new.users}
        gone = {u.name for u in cfg.users if (u.name, u.github_id, (u.email or "").lower()) not in same}
        # Every module holds this same Config object, so swapping its lists is the whole reload. A removed user is
        # dropped first: a request in between sees either the old pair or the new one, never a wider one.
        cfg.users = [u for u in cfg.users if u.name not in gone]
        cfg.tokens = new.tokens
        cfg.users = new.users
        revoked = self.provider.revoke_users(gone) if gone else 0
        self.audit.write(kind="service", event="reload", users=len(cfg.users), credentials=[t.name for t in cfg.tokens],
                         removed_users=sorted(gone), oauth_revoked=revoked, needs_restart=restart_needed)
        if restart_needed:
            print(f"oasa-lab: reloaded users and tokens; these changes need a restart: {', '.join(restart_needed)}",
                  file=sys.stderr, flush=True)
        return True

    def _reload_failed(self, error: str) -> bool:
        self.audit.write(kind="service", event="reload_failed", error=error)
        print(f"oasa-lab: reload refused, keeping the running config: {error}", file=sys.stderr, flush=True)
        return False

    def app(self) -> Starlette:
        cfg = self.cfg
        hosts = allowed_hosts(cfg)
        app = self.mcp.streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True, json_response=True, host=cfg.host,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                         allowed_hosts=sorted(hosts), allowed_origins=[]),
        )
        app.router.routes.extend(self.provider.routes() + build_routes(cfg, self.jobs, self.inventory, self.auth))
        inner = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(a):
            async with inner(a):
                await self.jobs.start()
                loop = asyncio.get_running_loop()
                try:   # only in the main thread (`oasa-lab serve`); tests call reload() directly
                    loop.add_signal_handler(signal.SIGHUP, self.reload)
                    hup = True
                except (RuntimeError, ValueError, NotImplementedError, AttributeError):
                    hup = False
                self.audit.write(kind="service", event="start", version=_version(), rf_setup=cfg.rf_setup,
                                 paused=bool(self.jobs.paused))
                try:
                    yield
                finally:
                    if hup:
                        loop.remove_signal_handler(signal.SIGHUP)
                    await self.jobs.shutdown()
                    self.audit.write(kind="service", event="stop")

        app.router.lifespan_context = lifespan
        app.add_middleware(HostGuard, hosts=hosts)
        app.add_middleware(AuditMiddleware, audit=self.audit)  # outermost: sees refused requests too
        return app


def _version() -> str:
    from .. import __version__

    return __version__
