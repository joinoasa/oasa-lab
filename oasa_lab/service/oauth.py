"""Sign-in for Claude's custom connector: this service is the OAuth server, GitHub proves who you are.

Claude.ai needs OAuth 2.x with discovery and either Dynamic Client Registration or CIMD (sources in
docs/remote-lab.md). The MCP SDK provides the endpoints (/.well-known/*, /register, /authorize, /token,
/revoke, PKCE, token checks). This file supplies what the SDK leaves to us:

  - which clients may register: only Claude's own callback URLs (blocks the "attacker registers a client
    and sends the owner a link" attack on a proxy like this),
  - a consent page, so a link someone sends you never signs you in silently,
  - the GitHub step: only a configured user (by numeric GitHub id) gets a code. Nobody else gets a token.
  - token storage (hashes only) and API-token passthrough for other MCP clients.

GitHub's own access token is used once to read the account id and then thrown away.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx2 as httpx
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError,
    TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import audit
from .config import Config, User

SCOPE = "lab"
CODE_TTL = 300
FLOW_TTL = 600
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
MAX_CLIENTS = 200
MAX_FLOWS = 100
SAME_SITE_OK = ('same-origin', 'none')
COOKIE = "oasa_lab_flow"
CSRF_COOKIE = "oasa_lab_csrf"
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")


def now_t() -> float:
    return time.time()


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_allowed(uri: str, allowed: tuple[str, ...]) -> bool:
    """Exact match, except loopback URIs ignore the port (RFC 8252 section 7.3)."""
    u = urlparse(uri)
    for a in allowed:
        p = urlparse(a)
        if u.scheme != p.scheme or u.path != p.path or u.query or u.fragment or u.username or u.password:
            continue
        if p.hostname in LOOPBACK:
            if u.hostname == p.hostname:
                return True
        elif u.netloc == p.netloc:
            return True
    return False


@dataclass
class _Flow:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created: float
    approved: bool = False


class LabAuthCode(AuthorizationCode):
    pass


class LabOAuthProvider:
    """Implements the MCP SDK's OAuthAuthorizationServerProvider."""

    def __init__(self, cfg: Config, audit_log, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.audit = audit_log
        self.http = http
        self.path = cfg.state_dir / "oauth.json"
        self.clients: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.codes: dict[str, LabAuthCode] = {}
        self.flows: dict[str, _Flow] = {}
        self.client_meta: dict[str, dict] = {}   # client id -> {"created": t, "used": bool}
        self.rotated: dict[str, dict] = {}       # hash of a spent refresh token -> {"family", "expires_at"}
        self._csrf_key = secrets.token_bytes(32)
        self._load()

    # --- storage: clients and hashed tokens in one 0600 file ----------------------------------------
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
            self.clients, self.access, self.refresh = d["clients"], d["access"], d["refresh"]
            self.client_meta, self.rotated = d.get("client_meta", {}), d.get("rotated", {})
        except (OSError, ValueError, KeyError):
            pass

    def _save(self) -> None:
        now = time.time()
        self.access = {k: v for k, v in self.access.items() if v["expires_at"] > now}
        self.refresh = {k: v for k, v in self.refresh.items() if v["expires_at"] > now}
        self.rotated = {k: v for k, v in self.rotated.items() if v["expires_at"] > now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"clients": self.clients, "access": self.access, "refresh": self.refresh,
                       "client_meta": self.client_meta, "rotated": self.rotated}, fh)
        os.replace(tmp, self.path)

    # --- client registration -------------------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(redirect_allowed(u, self.cfg.redirect_uris) for u in uris):
            self.audit.write(kind="oauth", event="register_refused", redirect_uris=uris)
            raise RegistrationError("invalid_redirect_uri", "this lab only accepts Claude's callback URLs")
        if len(self.clients) >= MAX_CLIENTS:
            # Claude registers anew on each connect. Only a client that never signed anyone in may be dropped,
            # so a flood of registrations cannot push out the owner's working connector.
            unused = [c for c in self.clients if not self.client_meta.get(c, {}).get("used")]
            if not unused:
                self.audit.write(kind="oauth", event="register_refused", reason="client table full")
                raise RegistrationError("invalid_client_metadata", "too many clients registered")
            del self.clients[min(unused, key=lambda c: self.client_meta.get(c, {}).get("created", 0))]
            self.client_meta.pop(min(unused, key=lambda c: self.client_meta.get(c, {}).get("created", 0)), None)
        cid = client_info.client_id or ""
        self.clients[cid] = json.loads(client_info.model_dump_json())
        self.client_meta[cid] = {"created": time.time(), "used": False}
        self._save()
        self.audit.write(kind="oauth", event="registered", client_name=client_info.client_name,
                         redirect_uris=uris)

    # --- step 1: /authorize -> our consent page -----------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.cfg.mcp_url:
            raise AuthorizeError("invalid_target", "unknown resource")
        now = time.time()
        self.flows = {k: v for k, v in self.flows.items() if now - v.created < FLOW_TTL}
        while len(self.flows) >= MAX_FLOWS:  # drop the oldest unfinished sign-in; never refuse a new one
            del self.flows[min(self.flows, key=lambda k: self.flows[k].created)]
        flow_id = secrets.token_urlsafe(24)
        self.flows[flow_id] = _Flow(client, params, now)
        return f"{self.cfg.public_url}/oauth/consent?{urlencode({'id': flow_id})}"

    def _flow(self, flow_id: str | None) -> _Flow | None:
        f = self.flows.get(flow_id or "")
        return f if f and time.time() - f.created < FLOW_TTL else None

    def _back(self, flow: _Flow, **q: str | None) -> RedirectResponse:
        return RedirectResponse(construct_redirect_uri(str(flow.params.redirect_uri), state=flow.params.state, **q),
                                status_code=302)

    # --- CSRF: a token tied to the flow, a cookie copy of it, and a same-origin check -----------------
    def _csrf_token(self, flow_id: str) -> str:
        nonce = secrets.token_urlsafe(16)
        mac = hmac.new(self._csrf_key, f"{flow_id}.{nonce}".encode(), "sha256").hexdigest()[:32]
        return f"{nonce}.{mac}"

    def _csrf_ok(self, flow_id: str, token: str) -> bool:
        nonce, _, mac = token.partition(".")
        want = hmac.new(self._csrf_key, f"{flow_id}.{nonce}".encode(), "sha256").hexdigest()[:32]
        return bool(nonce) and hmac.compare_digest(mac, want)

    def _same_origin(self, request: Request) -> bool:
        """A browser form post from our own page: Origin is ours and the browser says same-origin."""
        u = urlparse(self.cfg.public_url)
        ours = f"{u.scheme}://{u.netloc}"
        if request.headers.get("origin") != ours:
            return False
        return request.headers.get("sec-fetch-site", "same-origin") in SAME_SITE_OK

    # --- step 2: consent page, then GitHub --------------------------------------------------------
    async def consent_page(self, request: Request) -> Response:
        flow_id = request.query_params.get("id")
        flow = self._flow(flow_id)
        if not flow:
            return _page("Link expired", "<p>This sign-in link has expired. Start again from Claude.</p>", 400)
        csrf = self._csrf_token(flow_id or "")
        host = html.escape(urlparse(str(flow.params.redirect_uri)).netloc)
        name = html.escape(flow.client.client_name or "an app")
        body = (f"<p><b>{name}</b> wants to run and watch OASA lab tests.</p>"
                f"<p>After you approve, it will send you back to <b>{host}</b>.</p>"
                "<p>Only continue if you just started this from Claude. Next you sign in with GitHub.</p>"
                f'<form method="post"><input type="hidden" name="id" value="{html.escape(flow_id or "")}">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
                '<button name="choice" value="approve">Approve and sign in with GitHub</button> '
                '<button name="choice" value="deny" class="n">Deny</button></form>')
        resp = _page("Connect Claude to the OASA lab", body)
        resp.set_cookie(CSRF_COOKIE, csrf, max_age=FLOW_TTL, httponly=True, samesite="strict",
                        secure=self.cfg.public_url.startswith("https://"), path="/oauth")
        return resp

    async def consent_post(self, request: Request) -> Response:
        if not self._same_origin(request):
            self.audit.write(kind="oauth", event="consent_rejected", reason="cross-site request")
            return _page("Refused", "<p>This request did not come from the sign-in page.</p>", 403)
        form = await request.form()
        flow_id = str(form.get("id", ""))
        token = str(form.get("csrf", ""))
        if not token or not hmac.compare_digest(token, request.cookies.get(CSRF_COOKIE, "")) or not self._csrf_ok(flow_id, token):
            self.audit.write(kind="oauth", event="consent_rejected", reason="csrf token missing or wrong")
            return _page("Refused", "<p>Open the sign-in page again from Claude and approve there.</p>", 403)
        flow = self._flow(flow_id)
        if not flow:
            return _page("Link expired", "<p>This sign-in link has expired. Start again from Claude.</p>", 400)
        if form.get("choice") != "approve":
            self.flows.pop(flow_id, None)
            self.audit.write(kind="oauth", event="consent_denied")
            return self._back(flow, error="access_denied")
        if not (self.cfg.github_client_id and self.cfg.github_client_secret):
            return _page("Not set up", "<p>GitHub sign-in is not configured on this lab.</p>", 503)
        flow.approved = True
        q = urlencode({"client_id": self.cfg.github_client_id, "state": flow_id, "allow_signup": "false",
                       "redirect_uri": f"{self.cfg.public_url}/oauth/github/callback"})
        resp = RedirectResponse(f"{self.cfg.github_urls['authorize']}?{q}", status_code=302)
        # Ties the GitHub round trip to this browser, so a stranger cannot feed us their own callback.
        resp.set_cookie(COOKIE, flow_id, max_age=FLOW_TTL, httponly=True, samesite="lax",
                        secure=self.cfg.public_url.startswith("https://"), path="/oauth")
        return resp

    # --- step 3: back from GitHub ----------------------------------------------------------------
    def _match_user(self, gh_id: int, gh_login: str) -> User | None:
        for u in self.cfg.users:
            if u.github_id is not None:
                if u.github_id == gh_id:
                    return u
            elif u.github_login and u.github_login.lower() == gh_login.lower():
                return u
        return None

    async def github_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        flow = self._flow(state)
        if not flow or not flow.approved or request.cookies.get(COOKIE) != state:
            self.audit.write(kind="oauth", event="callback_rejected", reason="unknown flow or cookie mismatch")
            return _page("Sign-in failed", "<p>This sign-in did not start here. Start again from Claude.</p>", 400)
        self.flows.pop(state, None)
        if request.query_params.get("error") or not request.query_params.get("code"):
            return self._back(flow, error="access_denied")
        gh = await self._github_identity(request.query_params["code"])
        if gh is None:
            self.audit.write(kind="oauth", event="github_failed")
            return self._back(flow, error="server_error")
        user = self._match_user(*gh)
        if user is None:
            audit.note(actor=f"github:{gh[1]}")
            self.audit.write(kind="oauth", event="login_denied", github_login=gh[1], github_id=gh[0])
            return self._back(flow, error="access_denied")
        code = secrets.token_urlsafe(32)
        self.codes[code] = LabAuthCode(
            code=code, scopes=[SCOPE], expires_at=time.time() + CODE_TTL, client_id=flow.client.client_id or "",
            code_challenge=flow.params.code_challenge, redirect_uri=flow.params.redirect_uri,
            redirect_uri_provided_explicitly=flow.params.redirect_uri_provided_explicitly,
            resource=self.cfg.mcp_url, subject=user.name)
        self.client_meta.setdefault(flow.client.client_id or "", {"created": now_t()})["used"] = True
        audit.note(actor=user.name, via="oauth")
        self.audit.write(kind="oauth", event="login_ok", actor=user.name, client_name=flow.client.client_name)
        resp = self._back(flow, code=code)
        resp.delete_cookie(COOKIE, path="/oauth")
        return resp

    async def _github_identity(self, code: str) -> tuple[int, str] | None:
        """Trade the code for a GitHub token, read who it belongs to, keep nothing."""
        own = self.http is None
        client = self.http or httpx.AsyncClient(timeout=10)
        try:
            r = await client.post(self.cfg.github_urls["token"], headers={"Accept": "application/json"}, data={
                "client_id": self.cfg.github_client_id, "client_secret": self.cfg.github_client_secret,
                "code": code, "redirect_uri": f"{self.cfg.public_url}/oauth/github/callback"})
            gh_token = r.json().get("access_token")
            if r.status_code != 200 or not gh_token:
                return None
            r = await client.get(self.cfg.github_urls["user"], headers={
                "Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"})
            d = r.json()
            return (int(d["id"]), str(d["login"])) if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        finally:
            if own:
                await client.aclose()

    # --- tokens ----------------------------------------------------------------------------------
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        c = self.codes.get(authorization_code)
        if c and (c.client_id != client.client_id or c.expires_at < time.time()):
            self.codes.pop(authorization_code, None)
            return None
        return c

    def _issue(self, client_id: str, user: str, family: str | None = None) -> OAuthToken:
        now = time.time()
        family = family or secrets.token_hex(8)
        at, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.access[_h(at)] = {"client_id": client_id, "user": user, "expires_at": now + ACCESS_TTL, "family": family}
        self.refresh[_h(rt)] = {"client_id": client_id, "user": user, "expires_at": now + REFRESH_TTL, "family": family}
        self._save()
        return OAuthToken(access_token=at, expires_in=ACCESS_TTL, scope=SCOPE, refresh_token=rt)

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:  # single use
            raise TokenError("invalid_grant", "code already used")
        if not self.cfg.user(authorization_code.subject or ""):
            raise TokenError("invalid_grant", "user no longer allowed")
        return self._issue(client.client_id or "", authorization_code.subject or "")

    async def load_refresh_token(self, client, refresh_token: str):
        spent = self.rotated.get(_h(refresh_token))
        if spent:  # a token that was already swapped for a new one: someone kept a copy. End the whole family.
            self._revoke_family(spent["family"])
            self.audit.write(kind="oauth", event="refresh_reuse", family=spent["family"])
            return None
        r = self.refresh.get(_h(refresh_token))
        if not r or r["client_id"] != client.client_id or r["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=[SCOPE], expires_at=int(r["expires_at"]),
                            resource=self.cfg.mcp_url, subject=r["user"])

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        old = self.refresh.pop(_h(refresh_token.token), None)  # rotate: the old one dies here
        family = (old or {}).get("family") or secrets.token_hex(8)
        self.rotated[_h(refresh_token.token)] = {"family": family, "expires_at": time.time() + REFRESH_TTL}
        if not self.cfg.user(refresh_token.subject or ""):
            self._save()
            raise TokenError("invalid_grant", "user no longer allowed")
        return self._issue(client.client_id or "", refresh_token.subject or "", family)

    def _revoke_family(self, family: str) -> None:
        self.access = {k: v for k, v in self.access.items() if v.get("family") != family}
        self.refresh = {k: v for k, v in self.refresh.items() if v.get("family") != family}
        self._save()

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Used for every MCP request: an OAuth token, or one of the configured API tokens."""
        hit = self.cfg.api_token_user(token)
        if hit:
            return AccessToken(token=token, client_id=f"api-token:{hit[0].name}", scopes=[SCOPE], subject=hit[1].name,
                               resource=self.cfg.mcp_url, claims={"via": "api-token", "iss": self.cfg.public_url})
        r = self.access.get(_h(token))
        if not r or r["expires_at"] < time.time() or not self.cfg.user(r["user"]):
            return None
        return AccessToken(token=token, client_id=r["client_id"], scopes=[SCOPE], expires_at=int(r["expires_at"]),
                           subject=r["user"], resource=self.cfg.mcp_url, claims={"via": "oauth", "iss": self.cfg.public_url})

    def lookup(self, token: str) -> tuple[str, str] | None:
        """For the REST API: an OAuth access token -> (user name, client id)."""
        r = self.access.get(_h(token))
        if r and r["expires_at"] > time.time():
            return r["user"], r["client_id"]
        return None

    async def revoke_token(self, token) -> None:
        for table in (self.access, self.refresh):
            table.pop(_h(token.token), None)
        self._save()

    def revoke_all(self) -> int:
        n = len(self.access) + len(self.refresh)
        self.access.clear()
        self.refresh.clear()
        self._save()
        return n

    def routes(self) -> list[Route]:
        return [
            Route("/oauth/consent", self.consent_page, methods=["GET"]),
            Route("/oauth/consent", self.consent_post, methods=["POST"]),
            Route("/oauth/github/callback", self.github_callback, methods=["GET"]),
        ]


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>:root{{color-scheme:light dark}}"
            "body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem 1rem;margin-inline:auto;padding:0 1rem}"
            "button{font:inherit;padding:.5rem .9rem;margin:.25rem .25rem 0 0;cursor:pointer}</style>"
            f"<h1>{html.escape(title)}</h1>{body}")
    return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://github.com"})
