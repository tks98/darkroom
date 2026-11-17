#!/bin/bash
# Set up darkroom in this checkout: venv, dependencies, the cached denoise
# model, the optional agent skill link(s), then `darkroom setup` (Siril and
# GraXpert if missing, the denoise model, an offer to install StarNet2, and
# `darkroom doctor`). Safe to run again; it only creates what is missing.
set -euo pipefail

usage() {
    cat <<'USAGE'
usage: ./setup.sh [--skill-dir DIR]... [--no-skill] [--no-tools] [--starnet-dir DIR] [--offline]

  --skill-dir DIR     link skill/ as DIR/darkroom: the skills folder of your agent
                      (repeatable; default: ~/.claude/skills)
  --no-skill          do not link the skill anywhere
  --no-tools          do not run `darkroom setup` (no Siril/GraXpert install, no
                      denoise model download, no StarNet2 offer); only run `darkroom doctor`
  --starnet-dir DIR   passed to `darkroom setup` / `darkroom doctor` (else $DARKROOM_STARNET_DIR)
  --offline           passed to `darkroom setup` / `darkroom doctor` (no downloads, skip
                      catalogue server checks)
USAGE
}

skill=1
tools=1
skill_dirs=()
doctor_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-skill) skill=0 ;;
        --no-tools) tools=0 ;;
        --skill-dir)
            [ $# -ge 2 ] || { echo "setup: --skill-dir needs a directory" >&2; exit 2; }
            skill_dirs+=("$2"); shift ;;
        --skill-dir=*) skill_dirs+=("${1#*=}") ;;
        --offline) doctor_args+=("--offline") ;;
        --starnet-dir)
            [ $# -ge 2 ] || { echo "setup: --starnet-dir needs a directory" >&2; exit 2; }
            doctor_args+=("--starnet-dir" "$2"); shift ;;
        --starnet-dir=*) doctor_args+=("--starnet-dir" "${1#*=}") ;;
        -h|--help) usage; exit 0 ;;
        *) echo "setup: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

cd "$(dirname "$0")"
repo="$(pwd -P)"

# 1. virtual environment (Python >= 3.11)
if [ -x .venv/bin/python ]; then
    echo "== .venv exists ($(.venv/bin/python -V 2>&1))"
else
    py=""
    for c in python3.11 python3 python3.12 python3.13; do
        if command -v "$c" >/dev/null 2>&1 \
           && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            py="$c"; break
        fi
    done
    if [ -z "$py" ]; then
        echo "setup: no Python >= 3.11 found (tried python3.11, python3); install it from python.org" >&2
        exit 1
    fi
    echo "== creating .venv with $py ($("$py" -V 2>&1))"
    "$py" -m venv .venv
fi
if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "setup: .venv uses $(.venv/bin/python -V 2>&1); darkroom needs >= 3.11." >&2
    echo "       Remove .venv yourself and run setup again." >&2
    exit 1
fi

# 2. dependencies
echo "== installing darkroom and its dependencies into .venv"
.venv/bin/python -m pip install --quiet --disable-pip-version-check -e '.[dev]'

# 3. fixed-batch copy of the GraXpert denoise model (cached; built once)
echo "== denoise model"
.venv/bin/python - <<'PY'
import tempfile
from pathlib import Path
from darkroom import denoise, tools

src = tools.graxpert_model()
if not src.is_file():
    print(f"   GraXpert denoise model not found at {src}; skipping (darkroom doctor explains the fix)")
else:
    dst = tools.cache_dir() / f"graxpert_denoise_3.0.2_b{denoise.BATCH}.onnx"
    had = dst.is_file()
    with tempfile.TemporaryDirectory() as d:
        try:
            out = denoise.fixed_batch_model(Path(d) / "build.log", log=lambda m: print("   " + m))
            print(f"   fixed-batch model {'already built' if had else 'built'}: {out}")
        except tools.StageError as e:
            print(f"   could not build the fixed-batch model: {e}")
PY

# 4. agent skill link(s): skill/ is an Agent Skills folder (SKILL.md with name and
#    description); any agent that loads skills from a folder can use it
if [ "$skill" -eq 0 ]; then
    echo "== skill link skipped (--no-skill)"
elif [ ! -f skill/SKILL.md ]; then
    echo "== no skill/SKILL.md in this checkout; nothing to link"
else
    [ ${#skill_dirs[@]} -gt 0 ] || skill_dirs=("$HOME/.claude/skills")
    for d in "${skill_dirs[@]}"; do
        d="${d/#\~/$HOME}"
        dest="$d/darkroom"
        if [ -e "$dest" ] && [ ! -L "$dest" ]; then
            echo "== $dest exists and is not a symlink; left untouched"
        else
            mkdir -p "$d"
            ln -sfn "$repo/skill" "$dest"
            echo "== linked $dest -> $repo/skill"
        fi
    done
fi

# 5. Siril, GraXpert, the denoise model and StarNet2 (`darkroom setup` ends with doctor),
#    or only the environment check with --no-tools
rc=0
if [ "$tools" -eq 1 ]; then
    echo "== darkroom setup"
    .venv/bin/python -m darkroom setup ${doctor_args[@]+"${doctor_args[@]}"} || rc=$?
else
    echo "== darkroom doctor (tools skipped: --no-tools)"
    .venv/bin/python -m darkroom.doctor ${doctor_args[@]+"${doctor_args[@]}"} || rc=$?
fi

echo
echo "Activate the environment with:  source .venv/bin/activate"
if [ "$rc" -ne 0 ]; then
    echo "setup or doctor reported failures (see above); the venv and package are installed."
fi
exit "$rc"
