"""setup.sh, docker/darkroom-docker and the Dockerfile, run with stand-ins for docker and the venv's
python (nothing is installed, built or started)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = os.environ.get("DARKROOM_TEST_BASH") or shutil.which("bash")      # e.g. /bin/bash 3.2 on macOS
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return d


# ---------------------------------------------------------------- docker/darkroom-docker

def wrapper(tmp_path, *argv, stdin=subprocess.DEVNULL):
    log = tmp_path / "docker-args"
    d = fake_bin(tmp_path, "docker", f'for a in "$@"; do printf "%s\\n" "$a"; done > "{log}"\n')
    env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}
    p = subprocess.run([BASH, str(REPO / "docker" / "darkroom-docker"), *argv], env=env, cwd=tmp_path,
                       stdin=stdin, capture_output=True, text=True)
    args = log.read_text().splitlines() if log.exists() else None
    return p.returncode, p.stderr, args


def test_wrapper_runs_docker_as_you_with_the_folders(tmp_path):
    (tmp_path / "subs").mkdir()
    rc, err, args = wrapper(tmp_path, "--work", "w", "--subs", "subs", "--", "run", "--work", "/work")
    assert rc == 0, err
    assert args[:3] == ["run", "--rm", "--platform"] and args[-3:] == ["run", "--work", "/work"]
    assert f"{(tmp_path / 'w').resolve()}:/work" in args and f"{(tmp_path / 'subs').resolve()}:/subs:ro" in args
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "darkroom-cache:/cache" in args and "darkroom" in args and (tmp_path / "w").is_dir()


@pytest.mark.parametrize("argv, message", [
    (["--work"], "--work needs a value"),
    (["--work", "--", "doctor"], "--work needs a value"),
    (["--subs"], "--subs needs a value"),
    (["--work", "w", "--image"], "--image needs a value"),
    (["--", "doctor"], "--work DIR is required"),
    (["--work", "w", "--subs", "missing", "--", "doctor"], "--subs missing: no such folder"),
    (["--work", "w", "--darks", "nope"], "--darks nope: no such folder"),
    (["--wrok", "w"], "unknown option --wrok"),
])
def test_wrapper_usage_errors(tmp_path, argv, message):
    rc, err, args = wrapper(tmp_path, *argv)
    assert rc == 2 and message in err and "unbound variable" not in err
    assert args is None                                         # docker was not started
    assert not (tmp_path / "w").exists() or "image" in message  # no work folder made for a bad call


def test_wrapper_refuses_a_file_as_work_folder(tmp_path):
    (tmp_path / "f").write_text("x")
    rc, err, args = wrapper(tmp_path, "--work", "f", "--", "doctor")
    assert rc == 2 and "is not a folder" in err and args is None


# ---------------------------------------------------------------- Dockerfile

def test_dockerfile_runs_unprivileged_from_a_pinned_base():
    text = (REPO / "Dockerfile").read_text()
    base = re.search(r"^ARG BASE=(\S+)$", text, re.M).group(1)
    assert re.fullmatch(r"debian:trixie-slim@sha256:[0-9a-f]{64}", base)
    assert len(re.findall(r"^FROM --platform=linux/amd64 \$\{BASE\}", text, re.M)) == 2
    users = re.findall(r"^USER (\S+)$", text, re.M)
    assert users and users[-1] not in ("root", "0", "0:0")
    assert "HOME=/cache" in text and "chmod 1777 /cache /work" in text
    assert text.index("USER ") > text.index("chmod 1777")


# ---------------------------------------------------------------- setup.sh

FAKE_PYTHON = 'case "$1" in -V) echo "Python 3.12.0" ;; -) cat > /dev/null ;; esac\nexit 0\n'


def checkout(tmp_path) -> Path:
    """A copy of setup.sh and skill/ with a stand-in .venv/bin/python (pip, the model step and
    `darkroom setup` all do nothing)."""
    repo = tmp_path / "checkout"
    (repo / "skill").mkdir(parents=True)
    (repo / "skill" / "SKILL.md").write_text("---\nname: darkroom\n---\n")
    shutil.copy(REPO / "setup.sh", repo / "setup.sh")
    py = repo / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n" + FAKE_PYTHON)
    py.chmod(0o755)
    return repo


def run_setup(repo, *argv):
    p = subprocess.run([BASH, str(repo / "setup.sh"), *argv], capture_output=True, text=True,
                       env={**os.environ, "HOME": str(repo.parent / "home")})
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_setup_links_the_skill_and_keeps_a_link_to_another_checkout(tmp_path):
    repo = checkout(tmp_path)
    skills = tmp_path / "skills"
    out = run_setup(repo, "--skill-dir", str(skills))
    dest = skills / "darkroom"
    assert dest.is_symlink() and dest.resolve() == (repo / "skill").resolve() and "== linked" in out
    assert "already links to this checkout" in run_setup(repo, "--skill-dir", str(skills))

    other = tmp_path / "other-checkout" / "skill"
    other.mkdir(parents=True)
    dest.unlink()
    dest.symlink_to(other)
    out = run_setup(repo, "--skill-dir", str(skills))
    assert "links to another checkout" in out and "--replace-skill" in out
    assert dest.resolve() == other.resolve()                   # left alone
    out = run_setup(repo, "--skill-dir", str(skills), "--replace-skill")
    assert "relinked" in out and dest.resolve() == (repo / "skill").resolve()


def test_setup_leaves_a_dangling_link_and_a_real_folder(tmp_path):
    repo = checkout(tmp_path)
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "darkroom").symlink_to(tmp_path / "deleted-checkout" / "skill")
    out = run_setup(repo, "--skill-dir", str(skills))
    assert "a folder that no longer exists" in out and not (skills / "darkroom").exists()
    (skills / "darkroom").unlink()
    (skills / "darkroom").mkdir()
    out = run_setup(repo, "--skill-dir", str(skills), "--replace-skill")
    assert "is not a symlink; left untouched" in out and (skills / "darkroom").is_dir()


def test_setup_no_skill(tmp_path):
    repo = checkout(tmp_path)
    out = run_setup(repo, "--no-skill")
    assert "skill link skipped" in out and not (tmp_path / "home" / ".claude").exists()


def test_scripts_parse():
    for script in ("setup.sh", "docker/darkroom-docker", "docker/siril-cli", "docker/entrypoint"):
        sh = BASH if script not in ("docker/siril-cli", "docker/entrypoint") else (shutil.which("sh") or BASH)
        assert subprocess.run([sh, "-n", str(REPO / script)]).returncode == 0, script
