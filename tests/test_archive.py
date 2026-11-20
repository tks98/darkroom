"""darkroom.archive: the zip extractor of `darkroom setup` (GraXpert for Linux) and `get-starnet`.
Hand-made zips only; nothing is downloaded."""
import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

from darkroom import archive, installer, tools


def make_zip(path: Path, entries) -> Path:
    """entries: (name, data) for files, (name, None) for folders, (name, '->target') for symlinks."""
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name)
            if data is None:
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                data = b""
            elif isinstance(data, str) and data.startswith("->"):
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                data = data[2:]
            else:
                info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, data)
    return path


def outside_files(base: Path, into: Path) -> list[str]:
    return sorted(str(p.relative_to(base)) for p in base.rglob("*") if into not in p.parents and p != into
                  and p.suffix != ".zip")


CHAINED = [("p/", None), ("p/a", "->.."), ("p/a/b", "->.."), ("p/a/b/c", "->.."),
           ("p/a/b/c/ESCAPED.txt", b"escaped")]


@pytest.mark.parametrize("extract", [archive.extract, installer.unzip], ids=["archive", "installer"])
def test_chained_symlinks_cannot_write_outside(tmp_path, extract):
    base = tmp_path / "base"
    into = base / "deep" / "target"
    into.mkdir(parents=True)
    z = make_zip(tmp_path / "evil.zip", CHAINED)
    with pytest.raises(tools.StageError):
        extract(z, into)
    assert not any(p.name == "ESCAPED.txt" for p in tmp_path.rglob("*") if into not in p.parents)
    assert outside_files(base, into) == ["deep"]


@pytest.mark.parametrize("entries, match", [
    ([("p/a", "->.."), ("p/a/b", "->..")], "through a symlink"),             # links only: the second goes through the first
    ([("a", "->../outside")], "pointing outside"),
    ([("a", "->/etc")], "absolute"),
    ([("d/", None), ("d/l", "->../../x")], "pointing outside"),
    ([("l", "->d"), ("l/x.txt", b"x")], "twice|through a symlink"),          # a file under a later link's name
    ([("../x", b"x")], "unsafe path"),
    (([("/abs", b"x")]), "unsafe path"),
    ([("a/../../x", b"x")], "unsafe path"),
    ([("C:/x", b"x")], "unsafe path"),
    ([("a\\..\\..\\x", b"x")], "unsafe path"),
    ([("f", b"1"), ("f", b"2")], "twice"),
])
def test_unsafe_entries_are_refused(tmp_path, entries, match):
    into = tmp_path / "out"
    with pytest.warns(UserWarning) if len({n for n, _ in entries}) < len(entries) else _nothing():
        z = make_zip(tmp_path / "e.zip", entries)
    with pytest.raises(tools.StageError, match=match):
        archive.extract(z, into)
    assert {p.name for p in tmp_path.iterdir()} <= {"out", "e.zip"}


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_names_that_merely_start_with_two_dots_are_fine(tmp_path):
    z = make_zip(tmp_path / "ok.zip", [("pkg/", None), ("pkg/..config", b"c"), ("pkg/..d/x", b"x"),
                                       ("pkg/link", "->..config"), ("pkg/lib/libx.so.1", b"l"),
                                       ("pkg/lib/libx.so", "->libx.so.1"), ("pkg/up", "->lib/../..config"),
                                       ("__MACOSX/pkg/._x", b"junk")])
    archive.extract(z, tmp_path / "out")
    out = tmp_path / "out" / "pkg"
    assert (out / "..config").read_bytes() == b"c" and (out / "..d" / "x").read_bytes() == b"x"
    assert (out / "link").is_symlink() and (out / "link").read_bytes() == b"c"
    assert os.readlink(out / "lib" / "libx.so") == "libx.so.1" and (out / "up").read_bytes() == b"c"
    assert not (tmp_path / "out" / "__MACOSX").exists()


def test_executable_bits_are_kept(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        i = zipfile.ZipInfo("bin/tool")
        i.external_attr = (stat.S_IFREG | 0o755) << 16
        z.writestr(i, "#!/bin/sh\n")
        z.writestr("doc.txt", "d")
    (tmp_path / "x.zip").write_bytes(buf.getvalue())
    archive.extract(tmp_path / "x.zip", tmp_path / "out")
    assert os.access(tmp_path / "out" / "bin" / "tool", os.X_OK)
    assert not os.access(tmp_path / "out" / "doc.txt", os.X_OK)


def test_existing_symlink_in_the_target_is_not_followed(tmp_path):
    """A target folder that already holds a symlink (it never should) is not written through."""
    into = tmp_path / "out"
    into.mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", into / "d")
    z = make_zip(tmp_path / "x.zip", [("d/x.txt", b"x")])
    with pytest.raises(tools.StageError, match="through a symlink"):
        archive.extract(z, into)
    assert list((tmp_path / "elsewhere").iterdir()) == []
