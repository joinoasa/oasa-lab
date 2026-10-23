"""Sign-in for Claude: discovery, client registration, consent, GitHub (faked), tokens, refresh, revocation."""

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .conftest import free_port, start, stop
from .test_mcp import Session, run

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"


class FakeGitHub:
    """Just enough of github.com: a code maps to a GitHub account."""

    def __init__(self):
        self.accounts = {}  # code -> (id, login)
        self.tokens = {}
        self.port = free_port()

        async def token(request):
            form = await request.form()
            assert form["client_id"] == "gh-client" and form["client_secret"] == "gh-secret"
            acct = self.accounts.pop(form["code"], None)
            if not acct:
                return JSONResponse({"error": "bad_verification_code"})
            t = "gho_" + secrets.token_hex(8)
            self.tokens[t] = acct
            return JSONResponse({"access_token": t, "token_type": "bearer"})

        async def user(request):
            acct = self.tokens.get(request.headers.get("authorization", "")[7:])
            return JSONResponse({"id": acct[0], "login": acct[1]}) if acct else JSONResponse({}, 401)

        app = Starlette(routes=[Route("/token", token, methods=["POST"]), Route("/user", user)])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)

    def urls(self):
        b = f"http://127.0.0.1:{self.port}"
        return {"authorize_url": f"{b}/authorize", "token_url": f"{b}/token", "user_url": f"{b}/user"}

    def sign_in_as(self, gh_id, login):
        code = "ghcode-" + secrets.token_hex(6)
        self.accounts[code] = (gh_id, login)
        return code


@pytest.fixture
def gh():
    g = FakeGitHub()
    yield g
    g.server.should_exit = True


@pytest.fixture
def oauth_svc(make_service, gh):
    return make_service(extra={"github": gh.urls()})


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def register(svc, redirect=CLAUDE_CB):
    r = httpx.post(f"{svc.url}/register", json={
        "client_name": "Claude", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    return r


def begin(svc, client_id, challenge, redirect=CLAUDE_CB, state="st-1"):
    return httpx.get(f"{svc.url}/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "scope": "lab", "resource": f"{svc.url}/mcp"},
        follow_redirects=False)


def consent(browser, svc, flow_id, choice="approve"):
    """What a browser does: open the consent page (gets the cookie and token), then post the form from that page."""
    page = browser.get(f"/oauth/consent?id={flow_id}")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return browser.post("/oauth/consent", data={"id": flow_id, "choice": choice, "csrf": token},
                        headers={"Origin": svc.url, "Sec-Fetch-Site": "same-origin"})


def sign_in(svc, gh, gh_id, login, approve=True):
    """Drive the whole browser flow. Returns (callback response, verifier, client_id, browser)."""
    client_id = register(svc).json()["client_id"]
    verifier, challenge = pkce()
    browser = httpx.Client(base_url=svc.url, follow_redirects=False)
    r = begin(svc, client_id, challenge)
    assert r.status_code == 302 and r.headers["location"].startswith(f"{svc.url}/oauth/consent?id=")
    page = browser.get(r.headers["location"])
    assert page.status_code == 200 and "Approve" in page.text and "claude.ai" in page.text
    flow_id = parse_qs(urlparse(r.headers["location"]).query)["id"][0]
    r = consent(browser, svc, flow_id, "approve" if approve else "deny")
    assert r.status_code == 302
    if not approve:
        return r, verifier, client_id, browser
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == gh.urls()["authorize_url"]
    q = parse_qs(loc.query)
    assert q["client_id"] == ["gh-client"] and q["state"] == [flow_id]
    code = gh.sign_in_as(gh_id, login)
    cb = browser.get("/oauth/github/callback", params={"code": code, "state": q["state"][0]})
    return cb, verifier, client_id, browser


def exchange(svc, client_id, code, verifier, redirect=CLAUDE_CB):
    return httpx.post(f"{svc.url}/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": redirect,
        "code_verifier": verifier, "resource": f"{svc.url}/mcp"})


def code_of(resp):
    return parse_qs(urlparse(resp.headers["location"]).query)


def test_discovery_metadata_matches_what_claude_needs(oauth_svc):
    s = oauth_svc
    meta = httpx.get(f"{s.url}/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == s.url
    assert meta["authorization_endpoint"] == f"{s.url}/authorize" and meta["token_endpoint"] == f"{s.url}/token"
    assert meta["registration_endpoint"] == f"{s.url}/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "client_secret_post" in meta["token_endpoint_auth_methods_supported"]
    assert "authorization_code" in meta["grant_types_supported"] and "refresh_token" in meta["grant_types_supported"]


def test_only_claudes_callback_urls_may_register(oauth_svc):
    for bad in ("https://evil.example/cb", "http://claude.ai/api/mcp/auth_callback",
                "https://claude.ai.evil.example/api/mcp/auth_callback", "https://claude.ai/api/mcp/auth_callback/x",
                "https://claude.ai/other", "https://user@claude.ai/api/mcp/auth_callback", "javascript:alert(1)"):
        r = register(oauth_svc, bad)
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri", bad
    assert register(oauth_svc, CLAUDE_CB).status_code == 201
    assert register(oauth_svc, "https://claude.com/api/mcp/auth_callback").status_code == 201
    assert register(oauth_svc, "http://localhost:51234/callback").status_code == 400  # Claude Code is opt-in


def test_claude_code_loopback_can_be_allowed_by_config(make_service):
    s = make_service(extra={"oauth": {"redirect_uris": [CLAUDE_CB, "http://localhost/callback"]}})
    assert register(s, "http://localhost:51234/callback").status_code == 201  # any port, as RFC 8252 says
    assert register(s, "http://localhost:51234/other").status_code == 400
    assert register(s, "http://127.0.0.1:51234/callback").status_code == 400  # only the host that was listed


def test_full_sign_in_as_the_owner_gives_a_working_token(oauth_svc, gh):
    s = oauth_svc
    cb, verifier, client_id, _ = sign_in(s, gh, 1001, "owner-gh")
    assert cb.status_code == 302 and cb.headers["location"].startswith(CLAUDE_CB)
    q = code_of(cb)
    assert q["state"] == ["st-1"] and "error" not in q
    tok = exchange(s, client_id, q["code"][0], verifier)
    assert tok.status_code == 200, tok.text
    t = tok.json()
    assert t["token_type"] == "Bearer" and t["refresh_token"] and t["expires_in"] > 0
    # the code is single use
    assert exchange(s, client_id, q["code"][0], verifier).status_code == 400
    # the token works on the connector, as the owner, and is audited as such
    async def go():
        async with Session(s, token=t["access_token"], mode="legacy") as sess:
            err, lab = await sess.call("lab_status")
            assert not err
            err, p = await sess.call("pause_lab", reason="oauth")  # owner is admin
            assert not err
            await sess.call("resume_lab")
    run(go())
    assert any(r.get("kind") == "oauth" and r.get("event") == "login_ok" and r.get("actor") == "owner" for r in s.audit())
    assert any(r.get("kind") == "tool" and r.get("via") == "oauth" and r.get("actor") == "owner" for r in s.audit())
    # and on the REST API
    assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t['access_token']}"}).status_code == 200
    # tokens are stored as hashes only
    stored = (s.cfg.state_dir / "oauth.json").read_text()
    assert t["access_token"] not in stored and t["refresh_token"] not in stored
    assert oct((s.cfg.state_dir / "oauth.json").stat().st_mode & 0o777) == "0o600"


def test_a_client_that_registers_without_an_auth_method_gets_a_secret_and_works(oauth_svc, gh):
    """The other way a connector can register (no token_endpoint_auth_method): a confidential client."""
    s = oauth_svc
    reg = httpx.post(f"{s.url}/register", json={"client_name": "Claude", "redirect_uris": [CLAUDE_CB]}).json()
    assert reg["client_secret"]
    verifier, challenge = pkce()
    browser = httpx.Client(base_url=s.url, follow_redirects=False)
    loc = begin(s, reg["client_id"], challenge).headers["location"]
    flow_id = parse_qs(urlparse(loc).query)["id"][0]
    consent(browser, s, flow_id)
    cb = browser.get("/oauth/github/callback", params={"code": gh.sign_in_as(1001, "owner-gh"), "state": flow_id})
    code = code_of(cb)["code"][0]
    no_secret = exchange(s, reg["client_id"], code, verifier)
    assert no_secret.status_code in (400, 401)
    r = httpx.post(f"{s.url}/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                           "client_secret": reg["client_secret"], "redirect_uri": CLAUDE_CB, "code_verifier": verifier})
    assert r.status_code == 200 and r.json()["access_token"]


def test_a_wrong_pkce_verifier_gets_nothing(oauth_svc, gh):
    cb, verifier, client_id, _ = sign_in(oauth_svc, gh, 1001, "owner-gh")
    r = exchange(oauth_svc, client_id, code_of(cb)["code"][0], "x" * 60)
    assert r.status_code == 400 and "access_token" not in r.text


def test_any_other_github_account_is_denied_and_gets_no_code(oauth_svc, gh):
    for gh_id, login in ((9999, "stranger"), (1004, "owner-gh"), (4242, "citruscybersec")):
        cb, *_ = sign_in(oauth_svc, gh, gh_id, login)
        q = code_of(cb)
        assert q.get("error") == ["access_denied"] and "code" not in q, (gh_id, login)
    assert any(r.get("event") == "login_denied" and r["github_login"] == "stranger" for r in oauth_svc.audit())
    assert not oauth_svc.lab.provider.access  # nothing was issued


def test_the_account_is_matched_by_numeric_id_not_by_name(oauth_svc, gh):
    cb, *_ = sign_in(oauth_svc, gh, 1001, "renamed-owner")  # same id, new login: still the owner
    assert "code" in code_of(cb)


def test_denying_consent_sends_the_client_back_with_an_error(oauth_svc, gh):
    r, *_ = sign_in(oauth_svc, gh, 1001, "owner-gh", approve=False)
    assert code_of(r)["error"] == ["access_denied"]


def test_a_callback_from_another_browser_is_rejected(oauth_svc, gh):
    s = oauth_svc
    client_id = register(s).json()["client_id"]
    _, challenge = pkce()
    loc = begin(s, client_id, challenge).headers["location"]
    flow_id = parse_qs(urlparse(loc).query)["id"][0]
    victim = httpx.Client(base_url=s.url, follow_redirects=False)
    consent(victim, s, flow_id)
    attacker = httpx.Client(base_url=s.url, follow_redirects=False)  # no cookie
    code = gh.sign_in_as(1001, "owner-gh")
    assert attacker.get("/oauth/github/callback", params={"code": code, "state": flow_id}).status_code == 400
    # a flow that was never approved, and an unknown flow, are rejected too
    loc2 = begin(s, client_id, challenge).headers["location"]
    flow2 = parse_qs(urlparse(loc2).query)["id"][0]
    victim.cookies.set("oasa_lab_flow", flow2, path="/oauth")
    assert victim.get("/oauth/github/callback", params={"code": code, "state": flow2}).status_code == 400
    assert victim.get("/oauth/github/callback", params={"code": code, "state": "nope"}).status_code == 400


def test_authorize_checks_the_registered_redirect_and_resource(oauth_svc):
    s = oauth_svc
    client_id = register(s).json()["client_id"]
    _, challenge = pkce()
    r = begin(s, client_id, challenge, redirect="https://evil.example/cb")
    assert r.status_code == 400 and "location" not in r.headers
    r = httpx.get(f"{s.url}/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_CB, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": "https://other.example/mcp"}, follow_redirects=False)
    assert "error=invalid_target" in r.headers.get("location", "") or r.status_code == 400
    r = httpx.get(f"{s.url}/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_CB},
                  follow_redirects=False)  # no PKCE
    assert "code=" not in r.headers.get("location", "") and "oauth/consent" not in r.headers.get("location", "")


def test_refresh_rotates_and_the_old_token_dies(oauth_svc, gh):
    s = oauth_svc
    cb, verifier, client_id, _ = sign_in(s, gh, 1001, "owner-gh")
    t = exchange(s, client_id, code_of(cb)["code"][0], verifier).json()
    r = httpx.post(f"{s.url}/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": client_id})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["access_token"] != t["access_token"] and t2["refresh_token"] != t["refresh_token"]
    assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t2['access_token']}"}).status_code == 200
    again = httpx.post(f"{s.url}/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": client_id})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_tokens_expire(oauth_svc, gh):
    s = oauth_svc
    cb, verifier, client_id, _ = sign_in(s, gh, 1001, "owner-gh")
    t = exchange(s, client_id, code_of(cb)["code"][0], verifier).json()
    for v in s.lab.provider.access.values():
        v["expires_at"] = time.time() - 1
    assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t['access_token']}"}).status_code == 401


def test_removing_the_user_from_the_config_ends_their_tokens(oauth_svc, gh):
    s = oauth_svc
    cb, verifier, client_id, _ = sign_in(s, gh, 1001, "owner-gh")
    t = exchange(s, client_id, code_of(cb)["code"][0], verifier).json()
    users = s.cfg.users
    s.cfg.users = [u for u in users if u.name != "owner"]
    try:
        assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t['access_token']}"}).status_code == 401
        r = httpx.post(f"{s.url}/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": client_id})
        assert r.status_code == 400
    finally:
        s.cfg.users = users


def test_revoking_a_token(oauth_svc, gh):
    s = oauth_svc
    cb, verifier, client_id, _ = sign_in(s, gh, 1001, "owner-gh")
    t = exchange(s, client_id, code_of(cb)["code"][0], verifier).json()
    assert httpx.post(f"{s.url}/revoke", data={"token": t["access_token"], "client_id": client_id, "client_secret": ""}).status_code == 200
    assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t['access_token']}"}).status_code == 401


def test_github_failure_gives_no_token(oauth_svc, gh):
    s = oauth_svc
    client_id = register(s).json()["client_id"]
    _, challenge = pkce()
    flow_id = parse_qs(urlparse(begin(s, client_id, challenge).headers["location"]).query)["id"][0]
    browser = httpx.Client(base_url=s.url, follow_redirects=False)
    consent(browser, s, flow_id)
    r = browser.get("/oauth/github/callback", params={"code": "not-a-real-code", "state": flow_id})
    assert code_of(r).get("error") == ["server_error"]


def test_github_sign_in_unconfigured_is_a_clear_error(make_service):
    s = make_service(env={"OASA_LAB_GITHUB_CLIENT_SECRET": ""})
    client_id = register(s).json()["client_id"]
    _, challenge = pkce()
    flow_id = parse_qs(urlparse(begin(s, client_id, challenge).headers["location"]).query)["id"][0]
    r = consent(httpx.Client(base_url=s.url, follow_redirects=False), s, flow_id)
    assert r.status_code == 503


def test_consent_page_escapes_what_a_client_calls_itself(oauth_svc):
    s = oauth_svc
    r = httpx.post(f"{s.url}/register", json={"client_name": "<script>alert(1)</script>", "redirect_uris": [CLAUDE_CB],
                                              "token_endpoint_auth_method": "none"})
    _, challenge = pkce()
    loc = begin(s, r.json()["client_id"], challenge).headers["location"]
    page = httpx.get(loc)
    assert "<script>alert" not in page.text and "&lt;script&gt;" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]


# --- CSRF on the consent form (H1) -----------------------------------------------------------------
def _flow(s):
    client_id = register(s).json()["client_id"]
    _, challenge = pkce()
    return parse_qs(urlparse(begin(s, client_id, challenge).headers["location"]).query)["id"][0]


def test_a_cross_site_page_cannot_approve_a_flow(oauth_svc):
    """The attack: a page on another site auto-submits the consent form in the owner's browser."""
    s = oauth_svc
    flow_id = _flow(s)
    owner = httpx.Client(base_url=s.url, follow_redirects=False)
    for headers in ({"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                    {"Origin": "https://evil.example"}, {"Origin": "null"}, {}, {"Origin": s.url, "Sec-Fetch-Site": "cross-site"},
                    {"Origin": s.url, "Sec-Fetch-Site": "same-site"}):
        r = owner.post("/oauth/consent", data={"id": flow_id, "choice": "approve", "csrf": "x.y"}, headers=headers)
        assert r.status_code == 403 and "oasa_lab_flow" not in r.headers.get("set-cookie", "")
    assert "oasa_lab_flow" not in owner.cookies


def test_the_consent_post_needs_the_token_and_cookie_from_the_page(oauth_svc):
    s = oauth_svc
    flow_id = _flow(s)
    same = {"Origin": s.url, "Sec-Fetch-Site": "same-origin"}
    owner = httpx.Client(base_url=s.url, follow_redirects=False)
    assert owner.post("/oauth/consent", data={"id": flow_id, "choice": "approve"}, headers=same).status_code == 403  # no token
    page = owner.get(f"/oauth/consent?id={flow_id}")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert "oasa_lab_csrf" in page.headers["set-cookie"] and "SameSite=strict" in page.headers["set-cookie"]
    # the token alone (attacker's own browser, no cookie)
    attacker = httpx.Client(base_url=s.url, follow_redirects=False)
    assert attacker.post("/oauth/consent", data={"id": flow_id, "choice": "approve", "csrf": token}, headers=same).status_code == 403
    # a token minted for another flow
    other = _flow(s)
    owner.get(f"/oauth/consent?id={other}")
    assert owner.post("/oauth/consent", data={"id": other, "choice": "approve", "csrf": token}, headers=same).status_code == 403
    # a cookie that differs from the form's token
    owner.cookies.set("oasa_lab_csrf", "forged.value", path="/oauth")
    assert owner.post("/oauth/consent", data={"id": flow_id, "choice": "approve", "csrf": token}, headers=same).status_code == 403
    # the real thing
    good = httpx.Client(base_url=s.url, follow_redirects=False)
    assert consent(good, s, flow_id).status_code == 302


# --- registration and flow limits (L1), redirect hosts (L7), refresh families (L4) ---------------------
def test_new_sign_ins_are_never_refused_the_oldest_is_dropped(oauth_svc, monkeypatch):
    s = oauth_svc
    monkeypatch.setattr("oasa_lab.service.oauth.MAX_FLOWS", 3)
    ids = [_flow(s) for _ in range(5)]
    assert len(s.lab.provider.flows) == 3 and ids[-1] in s.lab.provider.flows and ids[0] not in s.lab.provider.flows


def test_a_flood_of_registrations_cannot_evict_a_client_that_signed_in(oauth_svc, gh, monkeypatch):
    s = oauth_svc
    cb, verifier, client_id, _ = sign_in(s, gh, 1001, "owner-gh")
    assert "code" in code_of(cb)
    monkeypatch.setattr("oasa_lab.service.oauth.MAX_CLIENTS", 5)
    for _ in range(12):
        register(s)
    assert client_id in s.lab.provider.clients and len(s.lab.provider.clients) <= 5
    # when every slot is a used client, new registrations are refused instead
    for c in s.lab.provider.clients:
        s.lab.provider.client_meta[c]["used"] = True
    assert register(s).status_code == 400


@pytest.mark.parametrize("uri", ["https://claude.ai.evil.example/api/mcp/auth_callback", "https://evil.example/https://claude.ai/api/mcp/auth_callback",
                                 "https://claude.ai@evil.example/api/mcp/auth_callback", "https://claude.ai:8443/api/mcp/auth_callback",
                                 "http://claude.ai/api/mcp/auth_callback", "https://claude.ai/api/mcp/auth_callback/x",
                                 "https://claude.ai/api/mcp/auth_callback?x=1", "https://claude.ai/api/mcp/auth_callback#f"])
def test_redirect_uris_are_compared_by_parts(oauth_svc, uri):
    assert register(oauth_svc, uri).status_code == 400


def test_config_redirect_hosts_are_parsed_not_prefix_matched():
    from pathlib import Path
    from oasa_lab.service.config import ConfigError, parse_config
    base = {"service": {"port": 8765, "public_url": "http://127.0.0.1:8765"}, "sandbox": {"mode": "none"}, "users": [{"name": "o", "role": "admin", "github_id": 1}]}
    for bad in ("http://localhost.evil.example/cb", "http://127.0.0.1.evil.example/cb", "https://u@evil.example/cb"):
        with pytest.raises(ConfigError):
            parse_config({**base, "oauth": {"redirect_uris": [bad]}}, Path("."), {})
    parse_config({**base, "oauth": {"redirect_uris": ["http://localhost/callback", "https://claude.ai/cb"]}}, Path("."), {})
    with pytest.raises(ConfigError):
        parse_config({**base, "service": {"port": 8765, "public_url": "http://127.0.0.1.evil.example"}}, Path("."), {})


def test_reusing_a_rotated_refresh_token_ends_the_whole_family(oauth_svc, gh):
    s = oauth_svc
    cb, verifier, client_id, _ = sign_in(s, gh, 1001, "owner-gh")
    t1 = exchange(s, client_id, code_of(cb)["code"][0], verifier).json()

    def refresh(rt):
        return httpx.post(f"{s.url}/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id})

    t2 = refresh(t1["refresh_token"]).json()
    t3 = refresh(t2["refresh_token"]).json()
    assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t3['access_token']}"}).status_code == 200
    assert refresh(t1["refresh_token"]).status_code == 400  # a thief replays the first token
    # the family is gone: the newest refresh and access tokens stop working too
    assert refresh(t3["refresh_token"]).status_code == 400
    assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t3['access_token']}"}).status_code == 401
    # a second sign-in is a separate family and is untouched
    cb2, v2, cid2, _ = sign_in(s, gh, 1001, "owner-gh")
    t4 = exchange(s, cid2, code_of(cb2)["code"][0], v2).json()
    assert httpx.get(f"{s.url}/api/lab", headers={"Authorization": f"Bearer {t4['access_token']}"}).status_code == 200
