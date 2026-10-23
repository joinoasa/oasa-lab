"""Clean checkouts of the lab repo at a commit on `main`. Jobs never run anything else."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tomllib
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


async def _git(*args: str, cwd: Path | None = None, timeout: float = 120) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise CheckoutError(f"git {args[0]} timed out")
    if proc.returncode != 0:
        raise CheckoutError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:300]}")
    return out.decode().strip()


class Checkouts:
    def __init__(self, repo: str, branch: str, root: Path, keep: int = 5):
        self.repo, self.branch, self.root, self.keep = repo, branch, Path(root), keep
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
        await _git("clone", "-q", "--no-checkout", str(self.mirror), str(tmp))
        await _git("checkout", "-q", "--detach", sha, cwd=tmp)
        os.replace(tmp, dest)
        return Checkout(sha, dest)

    async def latest(self) -> Checkout:
        async with self._lock:
            return await self._materialize(await self._fetch())

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
            return await self._materialize(rev)

    def prune(self, in_use: set[str]) -> None:
        if not self.dirs.is_dir():
            return
        olds = sorted((p for p in self.dirs.iterdir() if SHA_RE.match(p.name)), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[self.keep:]:
            if p.name not in in_use:
                shutil.rmtree(p, ignore_errors=True)


def read_suites(checkout: Checkout) -> dict[str, dict]:
    """Visible suites at a commit: name -> {description, transmits, requires}. Read from the files, never run."""
    out: dict[str, dict] = {}
    d = checkout.suites_dir
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        f = p / "suite.toml"
        if p.name.startswith("_") or not SUITE_RE.match(p.name) or not f.is_file() or p.is_symlink():
            continue
        try:
            data = tomllib.loads(f.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        out[p.name] = {"name": p.name, "description": str(data.get("description", "")),
                       "transmits": bool(data.get("transmits", False)),
                       "requires": [str(x) for x in data.get("requires", [])]}
    return out
