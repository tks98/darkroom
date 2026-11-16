#!/bin/bash
# Set up darkroom in this checkout: venv, dependencies, the cached denoise
# model, the optional Claude Code skill link, then `darkroom doctor`.
# Safe to run again; it only creates what is missing.
set -euo pipefail

usage() {
    cat <<'USAGE'
usage: ./setup.sh [--no-skill] [--starnet-dir DIR] [--offline]

  --no-skill          do not link skill/ into ~/.claude/skills/darkroom
  --starnet-dir DIR   passed to `darkroom doctor` (else $DARKROOM_STARNET_DIR)
  --offline           passed to `darkroom doctor` (skip catalogue server checks)
USAGE
}

skill=1
doctor_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-skill) skill=0 ;;
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

# 4. Claude Code skill link
if [ "$skill" -eq 0 ]; then
    echo "== skill link skipped (--no-skill)"
elif [ ! -f skill/SKILL.md ]; then
    echo "== no skill/SKILL.md in this checkout; nothing to link"
else
    dest="$HOME/.claude/skills/darkroom"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
        echo "== $dest exists and is not a symlink; left untouched"
    else
        mkdir -p "$HOME/.claude/skills"
        ln -sfn "$repo/skill" "$dest"
        echo "== linked $dest -> $repo/skill"
    fi
fi

# 5. environment check
echo "== darkroom doctor"
rc=0
.venv/bin/python -m darkroom.doctor ${doctor_args[@]+"${doctor_args[@]}"} || rc=$?

echo
echo "Activate the environment with:  source .venv/bin/activate"
if [ "$rc" -ne 0 ]; then
    echo "doctor reported failures (see fixes above); setup itself completed."
fi
exit "$rc"
