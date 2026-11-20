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


def uargs(*argv):
    """For the 2.7.0 test build, which darkroom has no pinned hash for."""
    return args("--allow-unpinned", *argv)


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
        go(uargs(), net, "accept")
    assert leftovers() == []


def test_pinned_size_and_hash_are_checked():
    stem = gs.PINNED["linux"].stem
    page = f'<a href="https://download.starnetastro.com/{stem}.zip">x</a>'.encode()
    net = Net({gs.CLI_PAGE: page, gs.PINNED["linux"].url: fake_zip(stem)})
    with pytest.raises(tools.StageError, match="pinned size"):
        go(uargs(), net, "accept")
    net = Net({gs.CLI_PAGE: page, gs.PINNED["linux"].url: fake_zip(stem)}, length=None)
    with pytest.raises(tools.StageError, match="pinned size"):
        go(uargs(), net, "accept")
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
    rc, out, asked = go(uargs(), net, "accept")
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
        go(uargs(), Net(routes()), answer)
    assert leftovers() == []


def test_non_interactive_needs_the_flag_and_prints_the_licence():
    out = []
    with pytest.raises(tools.StageError, match="not accepted"):
        gs.run(uargs(), opener=Net(routes()), interactive=False, ask=lambda p: pytest.fail("must not prompt"),
               out=out.append, plat="linux")
    assert FAKE_LICENSE.strip() in "\n".join(out) and "--accept-license" in "\n".join(out)
    assert leftovers() == []
    rc, out, asked = go(uargs("--accept-license"), Net(routes()), interactive=False)
    assert rc == 0 and asked == [] and FAKE_LICENSE.strip() in out          # licence printed even then
    acc = starnet.read_accepted(starnet.installs_root() / NEW)
    assert acc["accepted_with"] == "--accept-license"


def test_package_without_licence_or_executable_is_refused():
    with pytest.raises(tools.StageError, match="no LICENSE.txt"):
        go(uargs("--accept-license"), Net(routes(fake_zip(license=None))))
    with pytest.raises(tools.StageError, match="no starnet2 executable"):
        go(uargs("--accept-license"), Net(routes(fake_zip(exe=False))))
    assert leftovers() == []


def test_unsafe_zip_paths_are_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(tools.StageError, match="unsafe path"):
        go(uargs("--accept-license"), Net(routes(buf.getvalue())))
    assert not (starnet.installs_root().parent / "escape.txt").exists() and leftovers() == []


# ---- already installed, --force, --list, --zip, --dry-run

def test_already_installed_and_force():
    go(uargs("--accept-license"), Net(routes()))
    net = Net(routes())
    rc, out, _ = go(uargs("--accept-license"), net)
    assert rc == 0 and "already installed" in out and net.opened == [gs.CLI_PAGE]      # not downloaded again
    dest = starnet.installs_root() / NEW
    (dest / "marker").write_text("old")
    rc, out, _ = go(uargs("--accept-license", "--force"), Net(routes()))
    assert rc == 0 and not (dest / "marker").exists() and leftovers() == [NEW]


def test_list(capsys):
    out = []
    gs.list_installed(out.append)
    assert out == [f"no StarNet2 installed by darkroom in {starnet.installs_root()}"]
    go(uargs("--accept-license"), Net(routes()))
    rc, out, _ = go(uargs("--list"), Net({}))
    assert rc == 0 and "StarNet2 2.7.0 (build 0300)" in out and "[default]" in out and "licence accepted" in out


def test_zip_install(tmp_path):
    z = tmp_path / f"{NEW}.zip"
    z.write_bytes(fake_zip())
    net = Net({})
    with pytest.raises(tools.StageError, match="not accepted: nothing was installed$"):
        go(uargs("--zip", str(z)), net, "no")
    assert z.is_file() and leftovers() == []                       # the user's own zip is kept
    rc, out, _ = go(uargs("--zip", str(z)), net, "accept")
    assert rc == 0 and net.opened == []                             # no network at all
    acc = starnet.read_accepted(starnet.installs_root() / NEW)
    assert acc["source"] == "local zip" and acc["zip_sha256"] == hashlib.sha256(z.read_bytes()).hexdigest()
    other = tmp_path / "starnet2_macos-arm64_2.7.0-0300_COREML_arm64_cli.zip"
    other.write_bytes(fake_zip(other.stem))
    with pytest.raises(tools.StageError, match="this machine needs"):
        go(uargs("--zip", str(other)), net, "accept")
    renamed = tmp_path / "starnet.zip"
    renamed.write_bytes(fake_zip())
    with pytest.raises(tools.StageError, match="not named like"):
        go(uargs("--zip", str(renamed)), net, "accept")


def test_zip_with_pinned_name_is_hash_checked(tmp_path):
    z = tmp_path / gs.PINNED["linux"].filename
    z.write_bytes(fake_zip(gs.PINNED["linux"].stem))
    with pytest.raises(tools.StageError, match="SHA-256 mismatch"):
        go(uargs("--zip", str(z), "--accept-license"), Net({}))
    assert leftovers() == []


def test_dry_run_reads_only_the_page():
    net = Net(routes())
    rc, out, asked = go(uargs("--dry-run"), net)
    assert rc == 0 and net.opened == [gs.CLI_PAGE] and asked == []
    assert f"https://download.starnetastro.com/{NEW}.zip" in out and "nothing was downloaded" in out
    assert leftovers() == []
    net = Net({gs.CLI_PAGE: OSError("offline")})
    rc, out, _ = go(uargs("--dry-run"), net)
    assert "pinned 2.6.2" in out and "151,481,786 bytes (pinned)" in out


def test_cli_wiring(monkeypatch, capsys):
    monkeypatch.setattr(gs, "http_open", Net(routes()))
    monkeypatch.setattr(gs, "platform_key", lambda: "linux")
    assert cli.main(["get-starnet", "--allow-unpinned"]) == 1                         # stdin is not a terminal under pytest
    err = capsys.readouterr()
    assert "licence not accepted" in err.err and FAKE_LICENSE.strip() in err.out
    assert leftovers() == []
    assert cli.main(["get-starnet", "--accept-license", "--allow-unpinned"]) == 0
    assert leftovers() == [NEW]


# ---- every 2.6.2 build is pinned; other builds need a confirmation

def test_all_platforms_are_pinned_by_size_and_hash():
    for plat, pkg in gs.PINNED.items():
        assert len(pkg.sha256) == 64 and pkg.size > 100_000_000, plat
        assert pkg.filename.startswith(f"starnet2_{plat}_2.6.2-0242_")


def test_unpinned_build_stops_without_confirmation_and_says_why():
    out = []
    with pytest.raises(tools.StageError, match="no pinned SHA-256 for this build and it was not confirmed"):
        gs.run(args("--accept-license"), opener=Net(routes()), interactive=False,
               ask=lambda p: pytest.fail("must not prompt"), out=out.append, plat="linux")
    text = "\n".join(out)
    data = fake_zip()
    assert "WARNING: darkroom has no pinned SHA-256" in text and f"{NEW}.zip" in text
    assert f"{len(data):,} bytes" in text and hashlib.sha256(data).hexdigest() in text
    assert "--allow-unpinned" in text and FAKE_LICENSE.strip() not in text     # stopped before the licence
    assert leftovers() == []


@pytest.mark.parametrize("answer, ok", [("yes", True), ("YES ", True), ("no", False), ("", False),
                                        (EOFError(), False)])
def test_unpinned_build_prompt(answer, ok):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if "unverified" in prompt:
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return "accept"
    if not ok:
        with pytest.raises(tools.StageError, match="not confirmed; the download was deleted"):
            gs.run(args(), opener=Net(routes()), interactive=True, ask=ask, out=lambda m: None, plat="linux")
        assert len(asked) == 1 and leftovers() == []
        return
    assert gs.run(args(), opener=Net(routes()), interactive=True, ask=ask, out=lambda m: None, plat="linux") == 0
    assert len(asked) == 2 and '"accept"' in asked[1]                   # the licence is still the user's call
    acc = starnet.read_accepted(starnet.installs_root() / NEW)
    assert acc["sha256_check"] == "none" and acc["unpinned_confirmed_with"] == "prompt"
    assert acc["zip_sha256"] == hashlib.sha256(fake_zip()).hexdigest() and acc["zip_size"] == len(fake_zip())


def test_allow_unpinned_does_not_accept_the_licence():
    with pytest.raises(tools.StageError, match="licence not accepted"):
        go(uargs(), Net(routes()), interactive=False)
    assert leftovers() == []


def test_published_hash_on_the_page_is_checked():
    data = fake_zip()
    good = hashlib.sha256(data).hexdigest()

    def page(sha):
        return (f'<div><span>SHA-256</span><code>{sha}</code></div>'
                f'<a href="https://download.starnetastro.com/{NEW}.zip">{NEW}.zip</a>').encode()
    assert gs.published_hashes(page(good).decode()) == {f"{NEW}.zip": good}
    net = Net({gs.CLI_PAGE: page("0" * 64), f"{gs.DOWNLOAD_BASE}{NEW}.zip": data})
    with pytest.raises(tools.StageError, match="publishes 0000"):
        go(uargs("--accept-license"), net)
    assert leftovers() == []
    rc, out, _ = go(uargs("--accept-license"), Net({gs.CLI_PAGE: page(good), f"{gs.DOWNLOAD_BASE}{NEW}.zip": data}))
    assert rc == 0 and "rules out a damaged download, not a compromised site" in out
    assert starnet.read_accepted(starnet.installs_root() / NEW)["sha256_check"] == "published"


def test_published_hashes_belong_to_the_package_that_follows():
    html = ("<code>" + "a" * 64 + "</code> StarNet2_win_x.exe "
            "<a href='https://download.starnetastro.com/starnet2_win_2.6.2-0242_ORT_x64_cli.zip'>w</a>"
            "<code>" + "b" * 64 + "</code> no zip here "
            "<code>" + "c" * 64 + "</code> starnet2_linux_2.6.2-0242_ORT_x64_cli.zip")
    assert gs.published_hashes(html) == {"starnet2_win_2.6.2-0242_ORT_x64_cli.zip": "a" * 64,
                                         "starnet2_linux_2.6.2-0242_ORT_x64_cli.zip": "c" * 64}


def test_download_without_pinned_size_or_content_length_is_capped(monkeypatch):
    monkeypatch.setattr(gs, "MAX_UNPINNED", 1000)
    with pytest.raises(tools.StageError, match="larger than the 1,000 bytes expected"):
        go(uargs("--accept-license"), Net(routes(), length=None))
    with pytest.raises(tools.StageError, match="more than the 1,000 darkroom accepts"):
        go(uargs("--accept-license"), Net(routes()))                    # Content-Length says too big
    assert leftovers() == []


def test_redirect_refusal_names_the_host():
    h = gs._Redirects({gs.DOWNLOAD_HOST})
    with pytest.raises(Exception) as e:
        h.redirect_request(None, None, 302, "Found", {}, "https://cdn.example.net/x.zip")
    assert "cdn.example.net" in gs._short(e.value) and gs.DOWNLOAD_HOST in gs._short(e.value)
    pkg = gs.Package("linux", "2.6.2", "0242", "ORT_x64")
    with pytest.raises(tools.StageError, match="redirected to https://cdn.example.net; refused"):
        gs.download(pkg, Path(os.devnull), lambda url, timeout=60: Resp("https://cdn.example.net/a.zip", b"x"),
                    log=lambda m: None)


# ---- the licence is read from the zip; nothing is unpacked before acceptance

def test_licence_is_shown_before_anything_is_unpacked(monkeypatch):
    from darkroom import archive
    unpacked = []
    real = archive.extract
    monkeypatch.setattr(archive, "extract", lambda *a, **k: unpacked.append(a[1]) or real(*a, **k))

    def ask(prompt):
        assert unpacked == [] and not list(starnet.installs_root().rglob("starnet2"))
        return "no"
    with pytest.raises(tools.StageError, match="licence not accepted"):
        gs.run(uargs(), opener=Net(routes()), interactive=True, ask=ask, out=lambda m: None, plat="linux")
    assert unpacked == [] and leftovers() == []
    rc, out, _ = go(uargs(), Net(routes()), "accept")
    assert rc == 0 and len(unpacked) == 1


def test_licence_found_at_top_level_and_under_other_names():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        i = zipfile.ZipInfo("starnet2")
        i.external_attr = (stat.S_IFREG | 0o755) << 16
        z.writestr(i, "x")
        z.writestr("Licence-StarNet.txt", FAKE_LICENSE)
    rc, out, _ = go(uargs("--accept-license"), Net(routes(buf.getvalue())))
    assert rc == 0 and FAKE_LICENSE.strip() in out
    assert starnet.read_accepted(starnet.installs_root() / NEW)["license_file"] == "Licence-StarNet.txt"


def test_symlinked_licence_or_executable_is_refused():
    for name in ("LICENSE.txt", "starnet2"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in ("starnet2", "LICENSE.txt"):
                i = zipfile.ZipInfo(f"{NEW}/{n}")
                if n == name:
                    i.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(i, "../../../etc/passwd")
                else:
                    i.external_attr = (stat.S_IFREG | 0o755) << 16
                    z.writestr(i, FAKE_LICENSE)
        with pytest.raises(tools.StageError, match="no LICENSE.txt|no starnet2 executable"):
            go(uargs("--accept-license"), Net(routes(buf.getvalue())))
    assert leftovers() == []


def test_chained_symlink_zip_writes_nothing_outside():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        def add(name, data, mode):
            i = zipfile.ZipInfo(name)
            i.external_attr = mode << 16
            z.writestr(i, data)
        add(f"{NEW}/starnet2", "x", stat.S_IFREG | 0o755)
        add(f"{NEW}/LICENSE.txt", FAKE_LICENSE, stat.S_IFREG | 0o644)
        add(f"{NEW}/a", "..", stat.S_IFLNK | 0o777)
        add(f"{NEW}/a/b", "..", stat.S_IFLNK | 0o777)
        add(f"{NEW}/a/b/c", "..", stat.S_IFLNK | 0o777)
        add(f"{NEW}/a/b/c/ESCAPED.txt", "escaped", stat.S_IFREG | 0o644)
    with pytest.raises(tools.StageError):
        go(uargs("--accept-license"), Net(routes(buf.getvalue())))
    cache = starnet.installs_root().parent
    assert not list(cache.rglob("ESCAPED.txt")) and leftovers() == []


# ---- macOS: quarantined like Siril and GraXpert

class FakeMacTools:
    """Stands in for xattr and spctl; never touches real flags."""

    def __init__(self, notarized=False, write_rc=0):
        self.notarized, self.write_rc, self.calls, self.flags = notarized, write_rc, [], {}

    def __call__(self, cmd, timeout=None, cwd=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[:2] == ["xattr", "-w"]:
            assert cmd[2:4] == ["-r", "com.apple.quarantine"]
            if self.write_rc:
                return self.write_rc, "xattr: [Errno 1] Operation not permitted"
            root = Path(cmd[5])
            for p in [root, *root.rglob("*")]:
                self.flags[str(p)] = cmd[4]
            return 0, ""
        if cmd[:2] == ["xattr", "-p"]:
            v = self.flags.get(cmd[3])
            return (0, v + "\n") if v else (1, "No such xattr")
        if cmd[0] == "spctl":
            assert cmd[:6] == ["spctl", "-a", "-t", "open", "--context", "context:primary-signature"]
            return (0, "accepted\nsource=Notarized Developer ID\n") if self.notarized else (3, f"{cmd[-1]}: rejected\n")
        raise AssertionError(f"unexpected command {cmd}")


MAC_NEW = "starnet2_macos-arm64_2.7.0-0300_COREML_arm64_cli"


@pytest.mark.parametrize("notarized", [False, True])
def test_macos_install_is_quarantined_and_assessed(notarized):
    fake = FakeMacTools(notarized=notarized)
    out = []
    rc = gs.run(uargs("--accept-license"), opener=Net(routes(fake_zip(MAC_NEW), MAC_NEW)), interactive=False,
                ask=lambda p: pytest.fail("no prompt"), out=out.append, plat="macos-arm64", cmd=fake, system="darwin")
    assert rc == 0
    dest = starnet.installs_root() / MAC_NEW
    acc = starnet.read_accepted(dest)
    assert acc["quarantined"] is True and acc["quarantine"].startswith("0081;") and acc["notarized"] is notarized
    text = "\n".join(out)
    if notarized:
        assert "Gatekeeper: accepted (Notarized Developer ID)" in text
    else:
        assert "macOS will block it until you allow it" in text and "Open Anyway" in text
        assert f"xattr -dr com.apple.quarantine '{dest}'" in text
    assert not any(c[0] == "xattr" and c[1] in ("-d", "-c", "-dr") for c in fake.calls)   # never cleared


def test_macos_install_stops_when_the_flag_cannot_be_written():
    with pytest.raises(tools.StageError, match="could not mark StarNet2 as downloaded"):
        gs.run(uargs("--accept-license"), opener=Net(routes(fake_zip(MAC_NEW), MAC_NEW)), interactive=False,
               out=lambda m: None, plat="macos-arm64", cmd=FakeMacTools(write_rc=1), system="darwin")
    assert leftovers() == []


def test_macos_quarantine_only_on_macos():
    rc = gs.run(uargs("--accept-license"), opener=Net(routes()), interactive=False, out=lambda m: None,
                plat="linux", cmd=lambda *a, **k: pytest.fail("no xattr on Linux"), system="linux")
    assert rc == 0 and "quarantined" not in starnet.read_accepted(starnet.installs_root() / NEW)
