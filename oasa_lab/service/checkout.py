"""Commits of the lab repo on `main`, read from a git mirror. The service never checks out code to run it
and never runs anything from a checkout.

Two ways to get the mirror:
  - `systemd` mode: the mirror is root-controlled (`/var/lib/oasa-lab/mirror.git`, updated by a timer and by
    `oasa-lab-mirror.service`). The service only reads it, and asks for an update through `refresh`.
  - `none` mode (tests, development): the service keeps its own mirror and fetches into it.

What the service needs from a commit is data: the suites' `suite.toml` text (parsed with the installed parser,
see `read_suites`) and, to judge evidence, a copy of the `suites/` folder (`export_suites`, plain files only).
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..gitsafe import GIT_SAFE, git_env  # noqa: F401  (git_env is re-exported)
from ..policy import SHA_RE, SUITE_RE
from ..suites import parse_suite, suite_facts


class CheckoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class Checkout:
    """A commit on the branch. Just the id: there is no folder."""
    sha: str


def _umask_022() -> None:
    os.umask(0o022)


async def _git_bytes(*args: str, cwd: Path | None = None, timeout: float = 120, readable: bool = False,
                     stdin: bytes | None = None, global_config: str | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "git", *GIT_SAFE, *args, cwd=cwd, env=git_env(global_config),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=_umask_022 if readable else None)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise CheckoutError(f"git {args[0]} timed out")
    if proc.returncode != 0:
        raise CheckoutError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:300]}")
    return out


async def _git(*args: str, cwd: Path | None = None, timeout: float = 120, readable: bool = False,
               global_config: str | None = None) -> str:
    """Run git with a clean environment. `readable`: files come out world-readable, for a copy the job user must read."""
    return (await _git_bytes(*args, cwd=cwd, timeout=timeout, readable=readable, global_config=global_config)).decode().strip()


MAX_SUITE_TOML = 65536
MAX_SUITES = 500
LS_TREE_ENTRY = re.compile(rb"^(\d{6}) (\w+) ([0-9a-f]{40})\t(.+)$", re.S)
SUITE_TOML_PATH = re.compile(r"^suites/([a-z0-9][a-z0-9-]{0,63})/suite\.toml$")


class Checkouts:
    def __init__(self, repo: str, branch: str, root: Path, floor: str = "", mirror: Path | None = None,
                 refresh: Callable[[], Awaitable[None]] | None = None, git_config: str | None = None,
                 refresh_every: float = 20.0):
        """`mirror` given: a mirror someone else keeps up to date (systemd mode); this class only reads it.
        Otherwise it keeps `root/mirror.git` and fetches `repo` into it."""
        self.repo, self.branch, self.root, self.floor = repo, branch, Path(root), floor
        self.shared = mirror is not None
        self.mirror = Path(mirror) if mirror is not None else self.root / "mirror.git"
        self.refresh, self.git_config, self.refresh_every = refresh, git_config, refresh_every
        self._refreshed = 0.0
        self._lock = asyncio.Lock()

    def _g(self, *args: str, **kw):
        return _git(*args, cwd=self.mirror, global_config=self.git_config, **kw)

    async def _fetch(self) -> str:
        """The head commit of `main` in the mirror, after bringing the mirror up to date."""
        ref = f"refs/heads/{self.branch}"
        if self.shared:
            now = asyncio.get_running_loop().time()
            if self.refresh and now - self._refreshed > self.refresh_every:
                self._refreshed = now
                await self.refresh()
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.mirror.is_dir():
                await _git("init", "--bare", "-q", str(self.mirror))
            await self._g("fetch", "-q", "--no-tags", "--force", self.repo, f"{ref}:{ref}")
        try:
            sha = await self._g("rev-parse", "--verify", "-q", ref)
        except CheckoutError:
            raise CheckoutError(f"the mirror has no {self.branch} branch yet")
        if not SHA_RE.match(sha):
            raise CheckoutError("unexpected git output")
        return sha

    async def export(self, sha: str, dest: Path) -> Path:
        """A fresh copy of the code at `sha` for one job (`none` mode): its own clone, no hooks, nothing shared
        with an earlier job. The caller deletes it afterwards."""
        if not SHA_RE.match(sha):
            raise CheckoutError("bad commit id")
        shutil.rmtree(dest, ignore_errors=True)
        await _git("clone", "-q", "--no-checkout", "--no-hardlinks", "--template=", str(self.mirror), str(dest),
                   readable=True, global_config=self.git_config)
        await _git("checkout", "-q", "--detach", sha, cwd=dest, readable=True)
        return dest

    async def _above_floor(self, rev: str) -> None:
        """REST callers may pick a commit, but not one older than the configured floor (an old, vulnerable
        or retired suite). The floor itself is allowed."""
        if not self.floor:
            return
        try:
            await self._g("merge-base", "--is-ancestor", self.floor, rev)
        except CheckoutError:
            raise CheckoutError(f"commit {rev[:12]} is older than the lowest commit this lab runs ({self.floor[:12]})")

    async def latest(self) -> Checkout:
        async with self._lock:
            sha = await self._fetch()
            await self._above_floor(sha)
            return Checkout(sha)

    async def at(self, rev: str | None) -> Checkout:
        """The head of main, or a given full commit id that is on main (nothing else is allowed)."""
        if not rev:
            return await self.latest()
        if not SHA_RE.match(rev):
            raise CheckoutError("commit must be a full 40-character lowercase id")
        async with self._lock:
            await self._fetch()
            try:
                await self._g("merge-base", "--is-ancestor", rev, f"refs/heads/{self.branch}")
            except CheckoutError:
                raise CheckoutError(f"commit {rev[:12]} is not on {self.branch}")
            await self._above_floor(rev)
            return Checkout(rev)

    # --- data from a commit -------------------------------------------------------------------------
    async def suite_texts(self, sha: str) -> dict[str, bytes]:
        """name -> the bytes of suites/NAME/suite.toml at `sha`. Plain files only (a link or a submodule is
        skipped), size-capped. Nothing is checked out and nothing is run."""
        if not SHA_RE.match(sha):
            raise CheckoutError("bad commit id")
        listing = await _git_bytes("ls-tree", "-r", "-z", "--full-tree", sha, "--", "suites", cwd=self.mirror,
                                   global_config=self.git_config)
        oids: dict[str, str] = {}
        for entry in listing.split(b"\0"):
            m = LS_TREE_ENTRY.match(entry)
            if not m:
                continue
            try:
                path = m.group(4).decode("utf-8")
            except UnicodeDecodeError:
                continue
            hit = SUITE_TOML_PATH.match(path)
            if hit and m.group(1) in (b"100644", b"100755") and m.group(2) == b"blob" and len(oids) < MAX_SUITES:
                oids[hit.group(1)] = m.group(3).decode()
        if not oids:
            return {}
        order = sorted(oids)
        raw = await _git_bytes("cat-file", "--batch", cwd=self.mirror, global_config=self.git_config,
                               stdin=("\n".join(oids[n] for n in order) + "\n").encode())
        found: dict[str, bytes] = {}
        pos = 0
        for name in order:
            nl = raw.find(b"\n", pos)
            if nl < 0:
                break
            fields = raw[pos:nl].split()
            if len(fields) != 3 or fields[1] != b"blob" or not fields[2].isdigit():
                pos = nl + 1
                continue
            size = int(fields[2])
            body = raw[nl + 1: nl + 1 + size]
            pos = nl + 1 + size + 1
            if size <= MAX_SUITE_TOML and len(body) == size:
                found[name] = body
        return found

    async def export_suites(self, sha: str, dest: Path) -> Path:
        """Write the `suites/` folder of a commit to `dest/suites` and return `dest`: plain files and folders only,
        made by us. The independent checker reads suite.toml and the test names from it. No link is ever created."""
        if not SHA_RE.match(sha):
            raise CheckoutError("bad commit id")
        tar_bytes = await _git_bytes("archive", "--format=tar", sha, "suites", cwd=self.mirror, global_config=self.git_config)
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, mode=0o755)
        total = 0
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            for m in tar:
                parts = m.name.rstrip("/").split("/")
                if parts[0] != "suites" or any(p in ("", ".", "..") for p in parts) or "\0" in m.name:
                    continue
                target = dest.joinpath(*parts)
                if m.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                elif m.isreg():
                    total += m.size
                    if total > 200_000_000:
                        raise CheckoutError("the suites folder is too big")
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    src = tar.extractfile(m)
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                    with os.fdopen(fd, "wb") as out:
                        shutil.copyfileobj(src, out)
        return dest


_SUITES: dict[str, dict[str, dict]] = {}  # commit -> suites; a commit never changes
REQ_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}(:[0-9]{1,2})?\Z")
WIRING_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}\Z")


async def read_suites(checkouts: Checkouts, checkout: Checkout) -> dict[str, dict]:
    """The suites at a commit: name -> {description, transmits, transmits_declared, requires, wiring}.

    The suite.toml files are read as data and parsed here, with the installed `oasa_lab.suites` parser (the
    same one `oasa-lab run` and `job-run` use, so there is one definition of `transmits` and `requires`).
    No code from the commit is run, ever. A suite.toml that does not parse is left out."""
    if checkout.sha in _SUITES:
        return _SUITES[checkout.sha]
    found: dict[str, dict] = {}
    for name, raw in (await checkouts.suite_texts(checkout.sha)).items():
        if not SUITE_RE.match(name):
            continue
        try:
            facts = suite_facts(parse_suite(name, raw.decode("utf-8")))
        except (ValueError, TypeError, AttributeError):  # TOML errors are ValueErrors
            continue
        facts["description"] = facts["description"][:500]
        if not (all(REQ_RE.match(x) for x in facts["requires"]) and WIRING_RE.match(facts["wiring"])):
            continue
        found[name] = facts
    if len(_SUITES) > 50:
        _SUITES.clear()
    _SUITES[checkout.sha] = found
    return found
