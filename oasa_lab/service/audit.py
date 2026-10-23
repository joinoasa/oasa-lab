"""Audit log: one JSON line per request. Who, what, when, result. Never a secret."""

from __future__ import annotations

import contextvars
import json
import threading
import time
from pathlib import Path
from typing import Any

from ..jsonl import now_rfc3339

# Handlers add facts about the request here (job id, suite...). The middleware writes them out.
_extra: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("audit_extra", default=None)
SECRET_KEYS = ("token", "secret", "password", "authorization", "cookie", "assertion", "api_key")


def note(**fields: Any) -> None:
    d = _extra.get()
    if d is not None:
        d.update(fields)


def scrub(value: Any) -> Any:
    """Drop anything that looks like a secret, whatever the caller passed."""
    if isinstance(value, dict):
        return {k: ("[removed]" if (k.lower() == "code" or any(s in k.lower() for s in SECRET_KEYS)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, **fields: Any) -> None:
        rec = {"ts": now_rfc3339(), **scrub(fields)}
        line = json.dumps(rec, separators=(",", ":"), default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]


class AuditMiddleware:
    """Pure ASGI middleware around the whole app: logs every HTTP request after it is answered."""

    def __init__(self, app, audit: AuditLog):
        self.app = app
        self.audit = audit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra: dict[str, Any] = {}
        tok = _extra.set(extra)
        status = {"code": 0}
        started = time.monotonic()

        async def send_wrap(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        error = None
        try:
            await self.app(scope, receive, send_wrap)
        except BaseException as exc:  # still log, then let it go
            error = type(exc).__name__
            raise
        finally:
            _extra.reset(tok)
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            client = (scope.get("client") or ("", 0))[0]
            # cloudflared connects from localhost; only then is Cf-Connecting-Ip believable.
            remote = headers.get("cf-connecting-ip") if client in ("127.0.0.1", "::1") and headers.get("cf-connecting-ip") else client
            user = scope.get("user")
            tok = getattr(user, "access_token", None)  # set by the MCP SDK's bearer middleware
            actor = extra.pop("actor", None) or (tok.subject if tok else None)
            self.audit.write(
                kind="http", method=scope["method"], path=scope["path"], status=status["code"] or None,
                actor=actor, via=extra.pop("via", None), remote=remote,
                ms=round((time.monotonic() - started) * 1000), error=error, **extra,
            )
