# Installing darkroom

darkroom has been tested only on macOS with Apple Silicon.

## What you need first

None of these are bundled. Install each one yourself:

- Python 3.11 or later.
- [Siril](https://siril.org) 1.4.5. darkroom uses its command-line version, `siril-cli`, which ships inside the app.
- [GraXpert](https://graxpert.com) 3.1. Open it once and run a denoise so that it downloads denoise model 3.0.2; darkroom uses that model file directly.
- The [StarNet v2](https://www.starnetastro.com) command-line version. It is optional: without it, use the `cluster` look or `darkroom star-reduce`. See [StarNet on macOS](troubleshooting.md#starnet-is-blocked-by-gatekeeper) before you run it.

## Install

```sh
git clone https://github.com/tks98/darkroom.git
cd darkroom
bash setup.sh                # add --starnet-dir DIR to include StarNet in the check
source .venv/bin/activate    # puts `darkroom` on the PATH
```

`setup.sh` creates `.venv`, installs the package into it, builds a copy of the GraXpert denoise model that runs on the Apple GPU (see [why](troubleshooting.md#graxperts-gpu-denoise-fails-on-macos)), and links the Claude Code skill into `~/.claude/skills/darkroom` (pass `--no-skill` to skip that). It finishes by running `darkroom doctor`. You can run it again at any time; it only redoes what is missing.

## Check the setup with `darkroom doctor`

```sh
darkroom doctor --work path/to/work --subs path/to/lights
```

`doctor` checks everything a run depends on and prints a fix for anything that is wrong. It looks at Python and its packages, the Siril and GraXpert versions, the denoise model, and whether the model really runs on the GPU (it times one batch on the GPU and one on the CPU). It checks that StarNet is there and whether macOS Gatekeeper is blocking it, whether Siril's catalogue server and ESA's Gaia archive answer, and, with `--work` and `--subs`, whether there is enough free disk for the stack.

A run on this machine takes about 12 seconds. A `WARN` is information; only a `FAIL` needs fixing before you process anything. `doctor` reports and changes nothing, with one exception: the first time, it builds the GPU copy of the denoise model in the cache directory.

Useful flags: `--run-starnet` also runs StarNet on its bundled sample (about a minute), `--offline` skips the network checks, and `--json` prints the results for scripts.

## Where darkroom looks for things

darkroom finds the external tools through environment variables. Defaults are the standard macOS install locations.

| Variable | Points to |
|---|---|
| `DARKROOM_SIRIL` | the `siril-cli` executable. Default: inside `/Applications/Siril.app` |
| `DARKROOM_GRAXPERT` | the GraXpert executable. Default: inside `/Applications/GraXpert.app` |
| `DARKROOM_GRAXPERT_MODEL` | the GraXpert denoise model file, if it is not where GraXpert keeps it |
| `DARKROOM_STARNET_DIR` | the folder with the StarNet CLI and its weights. No default; `[starnet] dir` in the config or `--starnet-dir` also work |
| `DARKROOM_CACHE` | where the GPU copy of the denoise model is built. Default: `~/Library/Caches/darkroom` |

## Running the tests

```sh
.venv/bin/pip install -e '.[dev]'
pytest
```

The tests use synthetic arrays and fakes. They need no image data, no external tools and no network.
