"""Who is calling, and what may they do.

Three ways in, all ending in a configured user with a role:
  1. an API token (bearer; only its SHA-256 is in the config),
  2. an OAuth access token this service issued after a GitHub sign-in (what Claude uses),
  3. a Cloudflare Access JWT (Cf-Access-Jwt-Assertion), verified here against Cloudflare's keys.
Anything else is rejected. A user removed from the config loses access on the next request.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from .config import Config, User, hash_token


@dataclass(frozen=True)
class Principal:
    user: User
    via: str  # "api-token" | "oauth" | "cf-access"
    credential: str  # token name or client id; never the secret itself

    @property
    def name(self) -> str:
        return self.user.name

    def can(self, permission: str) -> bool:
        return self.user.can(permission)


def new_api_token() -> tuple[str, str]:
    """A fresh token and its hash. Show the token once; only the hash goes in the config."""
    token = "oasa_" + secrets.token_urlsafe(32)
    return token, hash_token(token)


class AccessVerifier:
    """Verifies Cloudflare Access JWTs. Cloudflare signs them with keys at the team's /certs URL."""

    def __init__(self, team_domain: str, audience: str, jwks_url: str = ""):
        self.team_domain = team_domain
        self.audience = audience
        self.jwks_url = jwks_url or f"https://{team_domain}/cdn-cgi/access/certs"
        self._client = None

    def _keys(self):
        import jwt

        if self._client is None:
            self._client = jwt.PyJWKClient(self.jwks_url, cache_keys=True, lifespan=3600, timeout=5)
        return self._client

    def verify(self, token: str) -> dict[str, Any] | None:
        import jwt

        try:
            key = self._keys().get_signing_key_from_jwt(token).key
            return jwt.decode(
                token, key, algorithms=["RS256"], audience=self.audience,
                issuer=f"https://{self.team_domain}", options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except Exception:
            return None


class Authenticator:
    def __init__(self, cfg: Config, oauth_lookup=None):
        self.cfg = cfg
        self.oauth_lookup = oauth_lookup  # token string -> (user name, client id) | None
        self.access = (AccessVerifier(cfg.access_team_domain, cfg.access_audience, cfg.access_jwks_url)
                       if cfg.access_team_domain else None)

    def bearer(self, token: str) -> Principal | None:
        hit = self.cfg.api_token_user(token)
        if hit:
            return Principal(hit[1], "api-token", hit[0].name)
        if self.oauth_lookup:
            found = self.oauth_lookup(token)
            if found:
                user = self.cfg.user(found[0])
                if user:
                    return Principal(user, "oauth", found[1])
        return None

    def authenticate(self, headers) -> Principal | None:
        auth = headers.get("authorization", "")
        if auth[:7].lower() == "bearer " and auth[7:].strip():
            return self.bearer(auth[7:].strip())
        jwt_ = headers.get("cf-access-jwt-assertion")
        if jwt_ and self.access:
            claims = self.access.verify(jwt_)
            email = (claims or {}).get("email", "")
            user = next((u for u in self.cfg.users if u.email and u.email.lower() == email.lower()), None) if email else None
            if user:
                return Principal(user, "cf-access", "access")
        return None
