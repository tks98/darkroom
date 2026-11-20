"""Unpacking the zips that `darkroom setup` (GraXpert for Linux) and `darkroom get-starnet`
download, without letting an entry land outside the target folder.

A lexical check of each name is not enough: a zip can hold a symlink ``a -> ..`` and then an
entry ``a/x`` that is written through it. So:

- names must be relative, without a ``..`` component (``..foo`` is a fine name);
- directories and regular files are written first, and before each write every folder on the
  way is checked to be a real folder (not a symlink) inside the real target root; files are
  opened with O_NOFOLLOW | O_EXCL, so an existing link or file is never written through;
- symlinks are created last, and only when their target is relative and stays inside the
  tree, both as written and once resolved against the finished tree (so chained links
  cannot point out of it either).

Any violation raises StageError; the caller unpacks into a fresh temporary folder and deletes
it on failure.
"""
from __future__ import annotations

import os
import re
import stat
import zipfile
from pathlib import Path

from . import tools

CHUNK = 1 << 20
MACOS_JUNK = "__MACOSX/"


def _parts(name: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", name) if p not in ("", ".")]


def safe_name(name: str) -> bool:
    """A relative entry name without '..' components, drive letters or NUL bytes."""
    if not name or "\0" in name or name.startswith(("/", "\\")):
        return False
    parts = _parts(name)
    return bool(parts) and ".." not in parts and ":" not in parts[0]


def _inside(root: str, p: str) -> bool:
    return p == root or os.path.commonpath([root, p]) == root


def members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [i for i in z.infolist() if not i.filename.startswith(MACOS_JUNK)]


def is_symlink(i: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(i.external_attr >> 16)


def _real_dir(root: str, rel: list[str], create: bool, what: str, zname: str) -> str:
    """The folder root/rel[0]/rel[1]/..., each component a real folder inside root (created when
    missing and create is set); never follows a symlink."""
    cur = root
    for part in rel:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create:
                raise tools.StageError(f"{zname}: {what}: folder {part!r} is missing") from None
            os.mkdir(cur, 0o755)
            continue
        if stat.S_ISLNK(st.st_mode):
            raise tools.StageError(f"{zname} writes {what} through a symlink ({part!r}); refused")
        if not stat.S_ISDIR(st.st_mode):
            raise tools.StageError(f"{zname}: {what}: {part!r} is not a folder")
    real = os.path.realpath(cur)
    if not _inside(root, real):
        raise tools.StageError(f"{zname}: {what} would be written outside the target folder; refused")
    return cur


def extract(zip_path: Path, into: Path, z: zipfile.ZipFile | None = None) -> None:
    """Unpack zip_path into the folder ``into`` (created if missing), keeping executable bits and
    in-tree symlinks; refuse anything that would write or point outside it."""
    if z is None:
        try:
            z = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError) as e:
            raise tools.StageError(f"{zip_path.name} is not a readable zip: {e}") from None
        with z:
            return extract(zip_path, into, z)
    zname = zip_path.name
    infos = members(z)
    for i in infos:
        if not safe_name(i.filename):
            raise tools.StageError(f"{zname} contains an unsafe path: {i.filename!r}")
    into.mkdir(parents=True, exist_ok=True)
    root = os.path.realpath(into)
    links = []
    for i in infos:                                     # 1. folders and regular files
        parts = _parts(i.filename)
        if is_symlink(i):
            links.append((i, parts))
            continue
        if i.is_dir():
            _real_dir(root, parts, True, i.filename, zname)
            continue
        parent = _real_dir(root, parts[:-1], True, i.filename, zname)
        target = os.path.join(parent, parts[-1])
        mode = i.external_attr >> 16
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:
            raise tools.StageError(f"{zname} has {i.filename!r} twice; refused") from None
        with os.fdopen(fd, "wb") as dst, z.open(i) as src:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        os.chmod(target, 0o755 if mode & 0o111 else 0o644)
    made = []
    for i, parts in links:                              # 2. symlinks, after every file
        link = z.read(i).decode("utf-8", errors="strict") if i.file_size < 4096 else None
        if not link or "\0" in link or link.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", link):
            raise tools.StageError(f"{zname} has a symlink with an absolute or invalid target: {i.filename}")
        parent = _real_dir(root, parts[:-1], True, i.filename, zname)
        lexical = os.path.normpath(os.path.join(parent, link))
        if not _inside(root, lexical) or not _inside(root, os.path.realpath(lexical)):
            raise tools.StageError(f"{zname} has a symlink pointing outside it: {i.filename}")
        target = os.path.join(parent, parts[-1])
        try:
            os.symlink(link, target)
        except FileExistsError:
            raise tools.StageError(f"{zname} has {i.filename!r} twice; refused") from None
        made.append((i.filename, target))
    for name, target in made:                           # 3. chains of links must stay inside too
        if not _inside(root, os.path.realpath(target)):
            raise tools.StageError(f"{zname} has a symlink pointing outside it: {name}")


def read_member(z: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """The bytes of one regular-file member, at most limit bytes."""
    i = z.getinfo(name)
    if is_symlink(i) or i.is_dir():
        raise tools.StageError(f"{name} in the zip is not a regular file")
    if i.file_size > limit:
        raise tools.StageError(f"{name} in the zip is {i.file_size:,} bytes; more than the {limit:,} expected")
    with z.open(i) as f:
        return f.read(limit + 1)[:limit]
