"""Clean checkouts of the lab repo at a commit on `main`. Jobs never run anything else."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class CheckoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class Checkout:
    sha: str
    path: Path

    @property
    def suites_dir(self) -> Path:
        return self.path / "suites"


# git settings that stop a checkout from running anything or reading a config someone planted
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never",
            "-c", "advice.detachedHead=false")


def git_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C"}


def _umask_022() -> None:
    os.umask(0o022)


async def _git(*args: str, cwd: Path | None = None, timeout: float = 120, readable: bool = False) -> str:
    """Run git with a clean environment. `readable`: files come out world-readable, for a copy the job user must read."""
    proc = await asyncio.create_subprocess_exec(
        "git", *GIT_SAFE, *args, cwd=cwd, env=git_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=_umask_022 if readable else None)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise CheckoutError(f"git {args[0]} timed out")
    if proc.returncode != 0:
        raise CheckoutError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:300]}")
    return out.decode().strip()


class Checkouts:
    def __init__(self, repo: str, branch: str, root: Path, keep: int = 5, floor: str = ""):
        self.repo, self.branch, self.root, self.keep, self.floor = repo, branch, Path(root), keep, floor
        self.mirror = self.root / "mirror.git"
        self.dirs = self.root / "checkouts"
        self._lock = asyncio.Lock()

    async def _fetch(self) -> str:
        """Update the local mirror of `main` and return its head commit."""
        self.root.mkdir(parents=True, exist_ok=True)
        ref = f"refs/heads/{self.branch}"
        if not self.mirror.is_dir():
            await _git("init", "--bare", "-q", str(self.mirror))
        await _git("fetch", "-q", "--no-tags", "--force", self.repo, f"{ref}:{ref}", cwd=self.mirror)
        sha = await _git("rev-parse", ref, cwd=self.mirror)
        if not SHA_RE.match(sha):
            raise CheckoutError("unexpected git output")
        return sha

    async def _materialize(self, sha: str) -> Checkout:
        dest = self.dirs / sha
        if (dest / ".git").exists():
            return Checkout(sha, dest)
        self.dirs.mkdir(parents=True, exist_ok=True)
        tmp = self.dirs / f".tmp-{sha}-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        await _git("clone", "-q", "--no-checkout", "--template=", str(self.mirror), str(tmp))
        await _git("checkout", "-q", "--detach", sha, cwd=tmp)
        os.replace(tmp, dest)
        return Checkout(sha, dest)

    async def export(self, sha: str, dest: Path) -> Path:
        """A fresh copy of the code at `sha` for one job: its own clone, no hooks, nothing shared with a
        earlier job. The caller deletes it afterwards."""
        if not SHA_RE.match(sha):
            raise CheckoutError("bad commit id")
        shutil.rmtree(dest, ignore_errors=True)
        await _git("clone", "-q", "--no-checkout", "--no-hardlinks", "--template=", str(self.mirror), str(dest), readable=True)
        await _git("checkout", "-q", "--detach", sha, cwd=dest, readable=True)
        return dest

    async def _above_floor(self, rev: str) -> None:
        """REST callers may pick a commit, but not one older than the configured floor (an old, vulnerable
        or retired suite). The floor itself is allowed."""
        if not self.floor:
            return
        try:
            await _git("merge-base", "--is-ancestor", self.floor, rev, cwd=self.mirror)
        except CheckoutError:
            raise CheckoutError(f"commit {rev[:12]} is older than the lowest commit this lab runs ({self.floor[:12]})")

    async def latest(self) -> Checkout:
        async with self._lock:
            sha = await self._fetch()
            await self._above_floor(sha)
            return await self._materialize(sha)

    async def at(self, rev: str | None) -> Checkout:
        """The head of main, or a given full commit id that is on main (nothing else is allowed)."""
        if not rev:
            return await self.latest()
        if not SHA_RE.match(rev):
            raise CheckoutError("commit must be a full 40-character lowercase id")
        async with self._lock:
            await self._fetch()
            try:
                await _git("merge-base", "--is-ancestor", rev, f"refs/heads/{self.branch}", cwd=self.mirror)
            except CheckoutError:
                raise CheckoutError(f"commit {rev[:12]} is not on {self.branch}")
            await self._above_floor(rev)
            return await self._materialize(rev)

    def prune(self, in_use: set[str]) -> None:
        if not self.dirs.is_dir():
            return
        olds = sorted((p for p in self.dirs.iterdir() if SHA_RE.match(p.name)), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[self.keep:]:
            if p.name not in in_use:
                shutil.rmtree(p, ignore_errors=True)


_SUITES: dict[str, dict[str, dict]] = {}  # commit -> suites; a commit never changes
REQ_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}(:[0-9]{1,2})?$")
WIRING_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")


def _limits() -> None:  # runs in the child before it starts
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


async def read_suites(checkout: Checkout, timeout: float = 30) -> dict[str, dict]:
    """The suites at a commit: name -> {description, transmits, transmits_declared, requires, wiring}.

    Read by the checkout's own oasa_lab/suites.py, the same parser `oasa-lab run` uses, so there is one
    definition of `transmits` and `requires`. It is run isolated (`python -I`), with no environment, small
    limits and nothing but the suite files to read. It parses TOML and runs no suite."""
    if checkout.sha in _SUITES:
        return _SUITES[checkout.sha]
    script = checkout.path / "oasa_lab" / "suites.py"
    if not script.is_file() or script.is_symlink():
        return {}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(script), "--json", "--suites-dir", str(checkout.suites_dir),
        cwd="/", env={}, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=_limits)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise CheckoutError("reading the suite list timed out")
    if proc.returncode != 0:
        raise CheckoutError(f"reading the suite list failed: {err.decode(errors='replace').strip()[-300:]}")
    try:
        raw = json.loads(out[:2_000_000])
    except ValueError:
        raise CheckoutError("the suite list is not JSON")
    found: dict[str, dict] = {}
    for r in raw if isinstance(raw, list) else []:
        try:
            name = r["name"]
            if r.get("error") or not isinstance(name, str) or not SUITE_RE.match(name):
                continue
            req = r["requires"]
            wiring = str(r.get("wiring", ""))
            if not (isinstance(req, list) and all(isinstance(x, str) and REQ_RE.match(x) for x in req)) or not WIRING_RE.match(wiring):
                continue
            found[name] = {"name": name, "description": str(r.get("description", ""))[:500],
                           "transmits": r["transmits"] is True, "transmits_declared": r.get("transmits_declared") is True,
                           "wiring": wiring, "requires": list(req)}
        except (KeyError, TypeError, AttributeError):
            continue
    if len(_SUITES) > 50:
        _SUITES.clear()
    _SUITES[checkout.sha] = found
    return found
