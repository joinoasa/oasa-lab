"""Reading files a job wrote, when the job may have planted symlinks.

A job runs suite code. Anything in its folder is hostile data: a symlink to /etc/shadow, to the service's
token file, to another job's results. So the service never opens a path inside a job folder the usual way.
It walks from the folder's own file descriptor, one name at a time, refusing every symlink, and accepts
only plain files and folders.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class UnsafePath(OSError):
    pass


class TooBig(OSError):
    pass


def _name_ok(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name


def open_inside(root: Path | str, *parts: str) -> int:
    """A file descriptor for the plain file root/parts[0]/.../parts[-1]. No component may be a symlink."""
    if not parts or not all(_name_ok(p) for p in parts):
        raise UnsafePath("bad path")
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        ffd = os.open(parts[-1], FILE_FLAGS, dir_fd=fd)
    except OSError as exc:
        raise UnsafePath(f"cannot open {'/'.join(parts)}: {exc.strerror}") from None
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(ffd).st_mode):
        os.close(ffd)
        raise UnsafePath("not a plain file")
    return ffd


def read_inside(root: Path | str, *parts: str, offset: int = 0, limit: int = 1_000_000) -> bytes:
    fd = open_inside(root, *parts)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, limit)
    finally:
        os.close(fd)


def size_inside(root: Path | str, *parts: str) -> int:
    fd = open_inside(root, *parts)
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def list_dirs(root: Path | str) -> list[str]:
    """Names of the real (not symlinked) folders directly under root, sorted."""
    try:
        fd = os.open(root, DIR_FLAGS)
    except OSError:
        return []
    try:
        with os.scandir(fd) as it:
            return sorted(e.name for e in it if _name_ok(e.name) and e.is_dir(follow_symlinks=False))
    finally:
        os.close(fd)


MAX_DEPTH = 24


def copy_tree(root: Path | str, name: str, dest: Path, max_bytes: int, max_files: int = 5000, max_depth: int = MAX_DEPTH) -> dict:
    """Copy folder root/name to dest: plain files and folders only, nothing followed, with size, count and depth
    limits (a deeper tree is rejected as a whole, never half copied).
    Returns {"files": n, "bytes": n, "skipped": [names of anything that was not a plain file or folder]}."""
    stats = {"files": 0, "bytes": 0, "skipped": []}
    if not _name_ok(name):
        raise UnsafePath("bad bundle name")
    rfd = os.open(root, DIR_FLAGS)
    try:
        top = os.open(name, DIR_FLAGS, dir_fd=rfd)
    except OSError as exc:
        os.close(rfd)
        raise UnsafePath(f"cannot open the bundle: {exc.strerror}") from None
    os.close(rfd)

    def walk(dfd: int, out: Path, rel: str, depth: int) -> None:
        if depth > max_depth:
            raise TooBig(f"folders nested more than {max_depth} deep")
        out.mkdir(mode=0o755, parents=True)
        with os.scandir(dfd) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if not _name_ok(e.name):
                stats["skipped"].append(rel + e.name)
                continue
            if e.is_dir(follow_symlinks=False):
                try:
                    sub = os.open(e.name, DIR_FLAGS, dir_fd=dfd)
                except OSError:
                    stats["skipped"].append(rel + e.name)
                    continue
                try:
                    walk(sub, out / e.name, rel + e.name + "/", depth + 1)
                finally:
                    os.close(sub)
            elif e.is_file(follow_symlinks=False):
                try:
                    ffd = os.open(e.name, FILE_FLAGS, dir_fd=dfd)
                except OSError:
                    stats["skipped"].append(rel + e.name)
                    continue
                with os.fdopen(ffd, "rb") as src:
                    if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                        stats["skipped"].append(rel + e.name)
                        continue
                    stats["files"] += 1
                    if stats["files"] > max_files:
                        raise TooBig(f"more than {max_files} files")
                    fd = os.open(out / e.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                    with os.fdopen(fd, "wb") as dst:
                        while chunk := src.read(1 << 20):
                            stats["bytes"] += len(chunk)
                            if stats["bytes"] > max_bytes:
                                raise TooBig(f"more than {max_bytes // 1_000_000} MB")
                            dst.write(chunk)
            else:
                stats["skipped"].append(rel + e.name)

    try:
        walk(top, Path(dest), "", 1)
    except RecursionError:
        shutil.rmtree(dest, ignore_errors=True)
        raise TooBig("folders nested too deep") from None
    except (TooBig, OSError):
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        os.close(top)
    return stats
