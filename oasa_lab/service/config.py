"""Lab config (TOML). Everything that decides who may do what lives here, not in code."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROLES = ("viewer", "runner", "admin")
# What each role may do. Adding users or roles later is a config change.
PERMISSIONS = {
    "viewer": {"read"},
    "runner": {"read", "run", "stop_own"},
    "admin": {"read", "run", "stop_own", "stop_any", "pause"},
}
RF_SAFE = ("cabled", "shielded")  # the only setups where a transmitting suite may run
RF_SETUPS = ("unset", "cabled", "shielded", "open")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Where Claude's hosted apps send people back to after sign-in. Claude Code uses a loopback address
# instead (http://localhost/callback); add it under [oauth] redirect_uris only if you use Claude Code.
DEFAULT_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


class ConfigError(ValueError):
    pass


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class User:
    name: str
    role: str
    github_id: int | None = None
    github_login: str | None = None
    email: str | None = None

    def can(self, permission: str) -> bool:
        return permission in PERMISSIONS[self.role]


@dataclass(frozen=True)
class ApiToken:
    name: str
    user: str
    sha256: str


@dataclass(frozen=True)
class Device:
    id: str
    kind: str
    usb: str | None = None  # "vendor:product", hex
    serial: str | None = None
    tcp: str | None = None  # "host:port" probed with a plain TCP connect
    simulated_present: bool = True


@dataclass(frozen=True)
class Profile:
    name: str
    requires: tuple[str, ...] = ()  # device ids that must be present
    description: str = ""


@dataclass(frozen=True)
class Sandbox:
    """How jobs are run. `systemd`: as another user, in their own cgroup, through a small root-owned helper.
    `none`: as the service's own user in a process group. For tests and development only."""
    mode: str = "systemd"
    user: str = "oasalab-job"
    socket: str = "/run/oasa-lab/jobrun.sock"   # the root helper's socket (deploy/systemd/oasa-lab-jobrun.socket)
    memory_max: str = "4G"
    tasks_max: int = 512


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8742
    public_url: str = "http://127.0.0.1:8742"
    api_public_url: str = ""
    state_dir: Path = Path("state")
    repo: str = "https://github.com/joinoasa/oasa-lab"
    branch: str = "main"
    job_timeout_seconds: float = 1800
    stop_grace_seconds: float = 10
    max_queued: int = 20
    keep_checkouts: int = 5
    rf_setup: str = "unset"
    devices_file: str = ""  # the hardware suites' device file (lab.toml: serials, ports, cabling), passed to jobs
    confirm_wiring: dict[str, str] = field(default_factory=dict)  # suite -> wiring the owner says is connected
    min_commit: str = ""                  # REST callers may not pick a commit older than this
    keep_jobs: int = 100                  # finished jobs whose evidence is kept; older ones are deleted
    min_free_mb: int = 2000               # a job does not start with less free disk than this
    max_bundle_mb: int = 500              # the largest evidence bundle the service accepts from a job
    local_pause_file: str = ""            # an owner-only pause the API cannot clear (default: state_dir/LOCAL_PAUSE)
    sandbox: Sandbox = field(default_factory=Sandbox)
    inventory_backend: str = "simulated"
    devices: list[Device] = field(default_factory=list)
    profiles: dict[str, Profile] = field(default_factory=dict)
    users: list[User] = field(default_factory=list)
    tokens: list[ApiToken] = field(default_factory=list)
    github_client_id: str = ""
    github_client_secret: str = ""  # from the environment, never the file
    github_urls: dict[str, str] = field(default_factory=lambda: {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "user": "https://api.github.com/user",
    })
    redirect_uris: tuple[str, ...] = DEFAULT_REDIRECTS
    access_team_domain: str = ""  # e.g. "myteam.cloudflareaccess.com"
    access_audience: str = ""
    access_jwks_url: str = ""  # tests only; derived from the team domain otherwise
    allowed_hosts: list[str] = field(default_factory=list)

    @property
    def mcp_url(self) -> str:
        return self.public_url.rstrip("/") + "/mcp"

    def user(self, name: str) -> User | None:
        return next((u for u in self.users if u.name == name), None)

    def api_token_user(self, token: str) -> tuple[ApiToken, User] | None:
        """Match a bearer token to a configured API token (constant-time over all of them)."""
        import hmac

        digest = hash_token(token)
        found = None
        for t in self.tokens:
            if hmac.compare_digest(digest, t.sha256):
                found = t
        u = self.user(found.user) if found else None
        return (found, u) if found and u else None


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def parse_config(data: dict[str, Any], base: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    base = base or Path.cwd()
    s = data.get("service", {})
    lab = data.get("lab", {})
    cfg = Config()
    cfg.host = str(s.get("host", cfg.host))
    # The service sits behind a tunnel and an identity layer. It never listens on a network address.
    _need(_is_loopback(cfg.host), f"service.host must be a loopback address (127.0.0.1 or ::1), not {cfg.host!r}")
    cfg.port = int(s.get("port", cfg.port))
    _need(0 <= cfg.port < 65536, "service.port out of range")
    cfg.public_url = str(s.get("public_url", f"http://{cfg.host}:{cfg.port}")).rstrip("/")
    cfg.api_public_url = str(s.get("api_public_url", "")).rstrip("/")
    cfg.state_dir = (base / str(s.get("state_dir", "state"))).resolve()
    cfg.repo = str(s.get("repo", cfg.repo))
    cfg.branch = str(s.get("branch", cfg.branch))
    _need(bool(re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", cfg.branch)) and not cfg.branch.startswith("-"), "bad service.branch")
    cfg.job_timeout_seconds = float(s.get("job_timeout_seconds", cfg.job_timeout_seconds))
    cfg.stop_grace_seconds = float(s.get("stop_grace_seconds", cfg.stop_grace_seconds))
    cfg.max_queued = int(s.get("max_queued", cfg.max_queued))
    cfg.keep_checkouts = int(s.get("keep_checkouts", cfg.keep_checkouts))
    cfg.devices_file = str(s.get("devices_file", ""))
    _need(not cfg.devices_file or os.path.isabs(cfg.devices_file), "service.devices_file must be an absolute path")
    cfg.min_commit = str(s.get("min_commit", ""))
    _need(not cfg.min_commit or bool(re.fullmatch(r"[0-9a-f]{40}", cfg.min_commit)), "service.min_commit must be a full 40-character commit id")
    cfg.keep_jobs = int(s.get("keep_jobs", cfg.keep_jobs))
    cfg.min_free_mb = int(s.get("min_free_mb", cfg.min_free_mb))
    cfg.max_bundle_mb = int(s.get("max_bundle_mb", cfg.max_bundle_mb))
    _need(cfg.keep_jobs >= 1 and cfg.min_free_mb >= 0 and cfg.max_bundle_mb >= 1, "keep_jobs, min_free_mb or max_bundle_mb out of range")
    cfg.local_pause_file = str(s.get("local_pause_file", cfg.state_dir / "LOCAL_PAUSE"))
    _need(os.path.isabs(cfg.local_pause_file), "service.local_pause_file must be an absolute path")
    sb = data.get("sandbox", {})
    mode = str(sb.get("mode", "systemd"))
    _need(mode in ("systemd", "none"), "sandbox.mode must be systemd or none")
    user = str(sb.get("user", "oasalab-job"))
    _need(bool(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user)) and user != "root", "sandbox.user must be an unprivileged user name")
    _need(bool(re.fullmatch(r"[0-9]{1,6}[KMG]?", str(sb.get("memory_max", "4G")))), "sandbox.memory_max looks like 4G")
    sock = str(sb.get("socket", Sandbox.socket))
    _need(os.path.isabs(sock), "sandbox.socket must be an absolute path")
    cfg.sandbox = Sandbox(mode, user, sock, str(sb.get("memory_max", "4G")), int(sb.get("tasks_max", 512)))
    cfg.allowed_hosts = [str(h) for h in s.get("allowed_hosts", [])]

    cfg.rf_setup = str(lab.get("rf_setup", "unset"))
    _need(cfg.rf_setup in RF_SETUPS, f"lab.rf_setup must be one of {', '.join(RF_SETUPS)}")

    for suite_name, wiring in data.get("confirm_wiring", {}).items():
        _need(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", suite_name)) and bool(NAME_RE.match(str(wiring))),
              f"confirm_wiring: bad entry {suite_name!r} = {wiring!r}")
        cfg.confirm_wiring[suite_name] = str(wiring)

    inv = data.get("inventory", {})
    cfg.inventory_backend = str(inv.get("backend", "simulated"))
    _need(cfg.inventory_backend in ("simulated", "usb"), "inventory.backend must be simulated or usb")
    for d in inv.get("devices", []):
        _need(bool(NAME_RE.match(str(d.get("id", "")))), f"bad device id {d.get('id')!r}")
        usb = d.get("usb")
        _need(usb is None or bool(re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", str(usb))), f"device {d['id']}: usb must look like 0bda:2838")
        cfg.devices.append(Device(id=d["id"], kind=str(d.get("kind", d["id"])), usb=usb and str(usb).lower(),
                                  serial=d.get("serial"), tcp=d.get("tcp"),
                                  simulated_present=bool(d.get("present", True))))
    ids = [d.id for d in cfg.devices]
    _need(len(set(ids)) == len(ids), "device ids must be unique")
    for name, p in data.get("profiles", {}).items():
        _need(bool(NAME_RE.match(name)), f"bad profile name {name!r}")
        req = tuple(str(x) for x in p.get("requires", []))
        _need(all(r in ids for r in req), f"profile {name}: requires a device that is not in [inventory]")
        cfg.profiles[name] = Profile(name, req, str(p.get("description", "")))

    for u in data.get("users", []):
        name, role = str(u.get("name", "")), str(u.get("role", ""))
        _need(bool(NAME_RE.match(name)), f"bad user name {name!r}")
        _need(role in ROLES, f"user {name}: role must be one of {', '.join(ROLES)}")
        gid = u.get("github_id")
        _need(isinstance(gid, int) and not isinstance(gid, bool) and gid > 0,
              f"user {name}: github_id is required (the number, not the name: gh api users/NAME --jq .id)")
        cfg.users.append(User(name, role, gid, u.get("github_login"), u.get("email")))
    _need(len({u.name for u in cfg.users}) == len(cfg.users), "user names must be unique")
    for t in data.get("tokens", []):
        _need(bool(re.fullmatch(r"[0-9a-f]{64}", str(t.get("sha256", "")))), f"token {t.get('name')}: sha256 must be 64 hex characters")
        _need(cfg.user(str(t.get("user"))) is not None, f"token {t.get('name')}: unknown user {t.get('user')!r}")
        cfg.tokens.append(ApiToken(str(t["name"]), str(t["user"]), str(t["sha256"])))

    gh = data.get("github", {})
    cfg.github_client_id = str(gh.get("client_id", ""))
    cfg.github_client_secret = env.get("OASA_LAB_GITHUB_CLIENT_SECRET", "")
    for k in ("authorize", "token", "user"):
        if gh.get(f"{k}_url"):
            cfg.github_urls[k] = str(gh[f"{k}_url"])
    if "redirect_uris" in data.get("oauth", {}):
        cfg.redirect_uris = tuple(str(x) for x in data["oauth"]["redirect_uris"])
    for r in cfg.redirect_uris:
        pu = urlparse(r)
        _need(pu.scheme == "https" or (pu.scheme == "http" and pu.hostname in ("localhost", "127.0.0.1", "::1")),
              f"oauth.redirect_uris: {r!r} must be https (or loopback)")
        _need(bool(pu.hostname) and not pu.username and not pu.fragment, f"oauth.redirect_uris: {r!r} is not a clean URL")
        _need("*" not in r, "oauth.redirect_uris: no wildcards")

    acc = data.get("cloudflare_access", {})
    cfg.access_team_domain = str(acc.get("team_domain", ""))
    cfg.access_audience = str(acc.get("audience", ""))
    cfg.access_jwks_url = str(acc.get("jwks_url", ""))
    if cfg.access_team_domain or cfg.access_audience:
        _need(bool(cfg.access_team_domain and cfg.access_audience), "cloudflare_access needs team_domain and audience")

    _need(bool(cfg.users), "no [[users]] in the config: nobody could ever sign in")
    pub = urlparse(cfg.public_url)
    if pub.scheme != "https" and not (pub.scheme == "http" and _is_loopback(pub.hostname or "")):
        raise ConfigError("service.public_url must be https (http only for localhost)")
    return cfg


def load_config(path: Path | str, env: dict[str, str] | None = None) -> Config:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from exc
    return parse_config(data, p.parent, env)
