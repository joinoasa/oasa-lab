"""Put the pieces together: one ASGI app with the connector, sign-in, REST API and job queue."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse

from .api import build_routes
from .audit import AuditLog, AuditMiddleware
from .auth import Authenticator
from .checkout import Checkouts
from .config import Config
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

    def __init__(self, cfg: Config, inventory: Inventory | None = None, http=None):
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.audit = AuditLog(cfg.state_dir / "audit.jsonl")
        self.provider = LabOAuthProvider(cfg, self.audit, http)
        self.inventory = inventory or make_inventory(cfg)
        pol = cfg.policy   # systemd mode: the git mirror belongs to root's timer; we only read it
        self.checkouts = Checkouts(cfg.repo, cfg.branch, cfg.state_dir / "git", cfg.min_commit,
                                  mirror=Path(pol.mirror) if pol else None, git_config=pol.git_config if pol else None)
        self.jobs = JobManager(cfg, self.checkouts, self.inventory, self.audit)
        self.checkouts.refresh = getattr(self.jobs.sandbox, "refresh_mirror", None)
        self.auth = Authenticator(cfg, self.provider.lookup)
        self.mcp = build_mcp(cfg, self.jobs, self.inventory, self.audit, self.provider)

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
                self.audit.write(kind="service", event="start", version=_version(), rf_setup=cfg.rf_setup,
                                 paused=bool(self.jobs.paused))
                try:
                    yield
                finally:
                    await self.jobs.shutdown()
                    self.audit.write(kind="service", event="stop")

        app.router.lifespan_context = lifespan
        app.add_middleware(HostGuard, hosts=hosts)
        app.add_middleware(AuditMiddleware, audit=self.audit)  # outermost: sees refused requests too
        return app


def _version() -> str:
    from .. import __version__

    return __version__
