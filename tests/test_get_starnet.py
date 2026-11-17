"""`darkroom get-starnet` with a fake HTTP layer and a fake zip (nothing is downloaded, and the
zip holds stand-in files, not StarNet)."""
import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from darkroom import cli, get_starnet as gs, starnet, tools

FAKE_LICENSE = "FAKE TEST LICENCE\nRevision 2099-01-01 (test)\nThis text stands in for the real terms.\n"
NEW = "starnet2_linux_2.7.0-0300_ORT_x64_cli"           # not pinned: no hash to check
PAGE = f"""<html><body>
<a href="https://download.starnetastro.com/starnet2_macos-arm64_2.7.0-0300_COREML_arm64_cli.zip">mac</a>
<a href="https://download.starnetastro.com/starnet2_macos-x64_2.7.0-0300_ORT_x64_cli.zip">intel</a>
<a href='https://download.starnetastro.com/starnet2_linux_2.6.2-0242_ORT_x64_cli.zip'>old linux</a>
<a href="https://download.starnetastro.com/{NEW}.zip">linux</a>
<a href="https://download.starnetastro.com/starnet2_win_2.7.0-0300_ORT_x64_cli.zip">win</a>
<a href="https://evil.example/starnet2_linux_9.9.9-0999_ORT_x64_cli.zip">mirror</a>
<a href="http://download.starnetastro.com/starnet2_linux_9.9.8-0998_ORT_x64_cli.zip">plain http</a>
</body></html>"""


def fake_zip(stem: str = NEW, license: str | None = FAKE_LICENSE, exe: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        def add(name, data, mode=0o644):
            info = zipfile.ZipInfo(f"{stem}/{name}")
            info.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(info, data)
        if exe:
            add("starnet2", "#!/bin/sh\necho stand-in\n", 0o755)
        add("StarNet2_weights.onnx", b"not weights")
        add("lib/libstandin.so.1", b"lib")
        link = zipfile.ZipInfo(f"{stem}/lib/libstandin.so")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "libstandin.so.1")
        add("README.txt", "readme")
        if license is not None:
            add("LICENSE.txt", license)
    return buf.getvalue()


class Resp:
    def __init__(self, url, data, length="auto"):
        self.url, self._b = url, io.BytesIO(data)
        self.headers = {} if length is None else {"Content-Length": str(len(data) if length == "auto" else length)}

    def read(self, n=-1):
        return self._b.read(n)

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    """Fake HTTP: url -> bytes (or an exception); records every URL opened."""

    def __init__(self, routes, length="auto"):
        self.routes, self.length, self.opened = routes, length, []

    def __call__(self, url, timeout=60):
        self.opened.append(url)
        r = self.routes.get(url)
        if r is None:
            raise OSError(f"no route to {url}")
        if isinstance(r, Exception):
            raise r
        return Resp(url, r, self.length if url != gs.CLI_PAGE else "auto")


def args(*argv):
    return cli.build_parser().parse_args(["get-starnet", *argv])


def routes(zipdata=None, stem=NEW):
    return {gs.CLI_PAGE: PAGE.encode(), f"{gs.DOWNLOAD_BASE}{stem}.zip": zipdata or fake_zip(stem)}


def leftovers() -> list[str]:
    root = starnet.installs_root()
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def go(a, net, answer=None, interactive=True, plat="linux"):
    out = []
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    rc = gs.run(a, opener=net, interactive=interactive, ask=ask, out=out.append, plat=plat)
    return rc, "\n".join(out), asked


# ---- URLs and platform

def test_parse_page_picks_newest_official_link():
    p = gs.parse_page(PAGE, "linux")
    assert (p.version, p.build, p.backend, p.sha256) == ("2.7.0", "0300", "ORT_x64", None)
    assert p.url == f"https://download.starnetastro.com/{NEW}.zip"
    assert gs.parse_page(PAGE, "macos-arm64").backend == "COREML_arm64"
    assert gs.parse_page(PAGE, "macos-x64").filename == "starnet2_macos-x64_2.7.0-0300_ORT_x64_cli.zip"
    only_pinned = '<a href="https://download.starnetastro.com/starnet2_linux_2.6.2-0242_ORT_x64_cli.zip">x</a>'
    p = gs.parse_page(only_pinned, "linux")
    assert p.sha256 == gs.PINNED["linux"].sha256 and p.size == 151_481_786
    assert gs.parse_page("<html>nothing</html>", "linux") is None


@pytest.mark.parametrize("url", [
    "http://download.starnetastro.com/starnet2_linux_2.6.2-0242_ORT_x64_cli.zip",
    "https://evil.example/starnet2_linux_2.6.2-0242_ORT_x64_cli.zip",
    "https://download.starnetastro.com.evil.example/starnet2_linux_2.6.2-0242_ORT_x64_cli.zip",
    "https://download.starnetastro.com@evil.example/starnet2_linux_2.6.2-0242_ORT_x64_cli.zip",
    "https://download.starnetastro.com:8443/starnet2_linux_2.6.2-0242_ORT_x64_cli.zip",
    "https://download.starnetastro.com/other/../starnet2_linux_2.6.2-0242_ORT_x64_cli.zip",
    "https://download.starnetastro.com/setup.exe",
])
def test_non_official_urls_are_refused(url):
    with pytest.raises(tools.StageError, match="only fetched from"):
        gs.check_official(url)


def test_official_url_is_accepted():
    gs.check_official(gs.PINNED["linux"].url)
    assert gs.PINNED["macos-arm64"].url == ("https://download.starnetastro.com/"
                                            "starnet2_macos-arm64_2.6.2-0242_COREML_arm64_cli.zip")


def test_redirect_off_the_download_host_is_refused():
    h = gs._Redirects({gs.DOWNLOAD_HOST})
    with pytest.raises(Exception, match="refused"):
        h.redirect_request(None, None, 302, "Found", {}, "https://evil.example/x.zip")


def test_page_unreadable_falls_back_to_pinned():
    pkg, note = gs.resolve("linux", Net({gs.CLI_PAGE: OSError("timed out")}))
    assert pkg == gs.PINNED["linux"] and "pinned 2.6.2" in note and "timed out" in note
    pkg, note = gs.resolve("macos-arm64", Net({gs.CLI_PAGE: b"<html>redesigned</html>"}))
    assert pkg == gs.PINNED["macos-arm64"] and "found no macos-arm64 download link" in note


@pytest.mark.parametrize("system, machine, rosetta, want", [
    ("darwin", "arm64", False, "macos-arm64"),
    ("darwin", "x86_64", False, "macos-x64"),
    ("darwin", "x86_64", True, "macos-arm64"),          # x86_64 Python under Rosetta on Apple Silicon
    ("linux", "x86_64", False, "linux"),
])
def test_platform_key(system, machine, rosetta, want):
    assert gs.platform_key(system, machine, rosetta=lambda: rosetta) == want


@pytest.mark.parametrize("system, machine", [("linux", "aarch64"), ("win32", "AMD64"), ("freebsd14", "amd64")])
def test_platform_key_unsupported(system, machine):
    with pytest.raises(tools.StageError, match="no StarNet2 command-line build"):
        gs.platform_key(system, machine, rosetta=lambda: False)


# ---- download checks

def test_size_mismatch_installs_nothing():
    net = Net(routes(), length=10**9)                          # Content-Length larger than what arrives
    with pytest.raises(tools.StageError, match="incomplete"):
        go(args(), net, "accept")
    assert leftovers() == []


def test_pinned_size_and_hash_are_checked():
    stem = gs.PINNED["linux"].stem
    page = f'<a href="https://download.starnetastro.com/{stem}.zip">x</a>'.encode()
    net = Net({gs.CLI_PAGE: page, gs.PINNED["linux"].url: fake_zip(stem)})
    with pytest.raises(tools.StageError, match="pinned size"):
        go(args(), net, "accept")
    net = Net({gs.CLI_PAGE: page, gs.PINNED["linux"].url: fake_zip(stem)}, length=None)
    with pytest.raises(tools.StageError, match="pinned size"):
        go(args(), net, "accept")
    pkg = gs.PINNED["linux"]
    data = fake_zip(stem)
    with pytest.raises(tools.StageError, match="SHA-256 mismatch"):
        gs.download(gs.Package(pkg.platform, pkg.version, pkg.build, pkg.backend, sha256="0" * 64),
                    Path(os.devnull), Net({pkg.url: data}), log=lambda m: None)
    assert leftovers() == []


def test_download_only_from_the_official_host():
    pkg = gs.Package("linux", "2.6.2", "0242", "ORT_x64")
    redirected = lambda url, timeout=60: Resp("https://evil.example/a.zip", b"x")
    with pytest.raises(tools.StageError, match="redirected"):
        gs.download(pkg, Path(os.devnull), redirected, log=lambda m: None)
    evil = gs.Package("linux", "2.6.2", "0242", "ORT_x64/../../x")
    with pytest.raises(tools.StageError, match="only fetched from"):
        gs.download(evil, Path(os.devnull), pytest.fail, log=lambda m: None)


# ---- licence acceptance

def test_prompt_accept_installs():
    net = Net(routes())
    rc, out, asked = go(args(), net, "accept")
    assert rc == 0 and len(asked) == 1 and '"accept"' in asked[0]
    assert FAKE_LICENSE.strip() in out and "StarNet2 2.7.0 (build 0300)" in out
    dest = starnet.installs_root() / NEW
    assert leftovers() == [NEW]                                     # no temp folders left behind
    assert sorted(p.name for p in dest.iterdir()) == ["LICENSE.txt", "README.txt", "StarNet2_weights.onnx",
                                                      "accepted.json", "lib", "starnet2"]
    assert os.access(dest / "starnet2", os.X_OK)
    assert (dest / "lib" / "libstandin.so").is_symlink() and (dest / "lib" / "libstandin.so").read_bytes() == b"lib"
    acc = json.loads((dest / "accepted.json").read_text())
    assert acc["product"] == "StarNet2" and (acc["version"], acc["build"]) == ("2.7.0", "0300")
    assert acc["license_revision"] == "Revision 2099-01-01 (test)" and acc["accepted_with"] == "prompt"
    assert acc["url"] == f"https://download.starnetastro.com/{NEW}.zip" and acc["accepted_at"].endswith("Z")
    assert net.opened == [gs.CLI_PAGE, acc["url"]]
    assert tools.starnet_dir(None) == dest                          # now the default StarNet
    assert starnet.detect(dest).params() == {"product": "starnet2", "version": "2.7.0", "build": "0300"}


@pytest.mark.parametrize("answer", ["yes", "Accept", "", "accept it", EOFError(), KeyboardInterrupt()])
def test_anything_but_accept_aborts(answer):
    with pytest.raises(tools.StageError, match="not accepted: nothing was installed, and the download was deleted"):
        go(args(), Net(routes()), answer)
    assert leftovers() == []


def test_non_interactive_needs_the_flag_and_prints_the_licence():
    out = []
    with pytest.raises(tools.StageError, match="not accepted"):
        gs.run(args(), opener=Net(routes()), interactive=False, ask=lambda p: pytest.fail("must not prompt"),
               out=out.append, plat="linux")
    assert FAKE_LICENSE.strip() in "\n".join(out) and "--accept-license" in "\n".join(out)
    assert leftovers() == []
    rc, out, asked = go(args("--accept-license"), Net(routes()), interactive=False)
    assert rc == 0 and asked == [] and FAKE_LICENSE.strip() in out          # licence printed even then
    acc = starnet.read_accepted(starnet.installs_root() / NEW)
    assert acc["accepted_with"] == "--accept-license"


def test_package_without_licence_or_executable_is_refused():
    with pytest.raises(tools.StageError, match="no LICENSE.txt"):
        go(args("--accept-license"), Net(routes(fake_zip(license=None))))
    with pytest.raises(tools.StageError, match="no starnet2 executable"):
        go(args("--accept-license"), Net(routes(fake_zip(exe=False))))
    assert leftovers() == []


def test_unsafe_zip_paths_are_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(tools.StageError, match="unsafe path"):
        go(args("--accept-license"), Net(routes(buf.getvalue())))
    assert not (starnet.installs_root().parent / "escape.txt").exists() and leftovers() == []


# ---- already installed, --force, --list, --zip, --dry-run

def test_already_installed_and_force():
    go(args("--accept-license"), Net(routes()))
    net = Net(routes())
    rc, out, _ = go(args("--accept-license"), net)
    assert rc == 0 and "already installed" in out and net.opened == [gs.CLI_PAGE]      # not downloaded again
    dest = starnet.installs_root() / NEW
    (dest / "marker").write_text("old")
    rc, out, _ = go(args("--accept-license", "--force"), Net(routes()))
    assert rc == 0 and not (dest / "marker").exists() and leftovers() == [NEW]


def test_list(capsys):
    out = []
    gs.list_installed(out.append)
    assert out == [f"no StarNet2 installed by darkroom in {starnet.installs_root()}"]
    go(args("--accept-license"), Net(routes()))
    rc, out, _ = go(args("--list"), Net({}))
    assert rc == 0 and "StarNet2 2.7.0 (build 0300)" in out and "[default]" in out and "licence accepted" in out


def test_zip_install(tmp_path):
    z = tmp_path / f"{NEW}.zip"
    z.write_bytes(fake_zip())
    net = Net({})
    with pytest.raises(tools.StageError, match="not accepted: nothing was installed$"):
        go(args("--zip", str(z)), net, "no")
    assert z.is_file() and leftovers() == []                       # the user's own zip is kept
    rc, out, _ = go(args("--zip", str(z)), net, "accept")
    assert rc == 0 and net.opened == []                             # no network at all
    acc = starnet.read_accepted(starnet.installs_root() / NEW)
    assert acc["source"] == "local zip" and acc["zip_sha256"] == hashlib.sha256(z.read_bytes()).hexdigest()
    other = tmp_path / "starnet2_macos-arm64_2.7.0-0300_COREML_arm64_cli.zip"
    other.write_bytes(fake_zip(other.stem))
    with pytest.raises(tools.StageError, match="this machine needs"):
        go(args("--zip", str(other)), net, "accept")
    renamed = tmp_path / "starnet.zip"
    renamed.write_bytes(fake_zip())
    with pytest.raises(tools.StageError, match="not named like"):
        go(args("--zip", str(renamed)), net, "accept")


def test_zip_with_pinned_name_is_hash_checked(tmp_path):
    z = tmp_path / gs.PINNED["linux"].filename
    z.write_bytes(fake_zip(gs.PINNED["linux"].stem))
    with pytest.raises(tools.StageError, match="SHA-256 mismatch"):
        go(args("--zip", str(z), "--accept-license"), Net({}))
    assert leftovers() == []


def test_dry_run_reads_only_the_page():
    net = Net(routes())
    rc, out, asked = go(args("--dry-run"), net)
    assert rc == 0 and net.opened == [gs.CLI_PAGE] and asked == []
    assert f"https://download.starnetastro.com/{NEW}.zip" in out and "nothing was downloaded" in out
    assert leftovers() == []
    net = Net({gs.CLI_PAGE: OSError("offline")})
    rc, out, _ = go(args("--dry-run"), net)
    assert "pinned 2.6.2" in out and "151,481,786 bytes (pinned)" in out


def test_cli_wiring(monkeypatch, capsys):
    monkeypatch.setattr(gs, "http_open", Net(routes()))
    monkeypatch.setattr(gs, "platform_key", lambda: "linux")
    assert cli.main(["get-starnet"]) == 1                         # stdin is not a terminal under pytest
    err = capsys.readouterr()
    assert "licence not accepted" in err.err and FAKE_LICENSE.strip() in err.out
    assert leftovers() == []
    assert cli.main(["get-starnet", "--accept-license"]) == 0
    assert leftovers() == [NEW]
