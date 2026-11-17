# Installing darkroom

darkroom has been tested only on macOS with Apple Silicon.

## What you need first

None of these are bundled. Install each one yourself:

- Python 3.11 or later.
- [Siril](https://siril.org) 1.4.5. darkroom uses its command-line version, `siril-cli`, which ships inside the app.
- [GraXpert](https://graxpert.com) 3.1. darkroom uses its denoise model 3.0.2 directly; `darkroom fetch-models` makes GraXpert download it, and `darkroom denoise` does so by itself if it is missing.
- StarNet2, optional. `darkroom get-starnet` downloads the command-line version for your platform from the official [StarNet site](https://www.starnetastro.com), shows you its licence and installs it only if you accept (see [StarNet](#starnet)). Without StarNet, use the `cluster` look or `darkroom star-reduce`.

## Install

```sh
git clone https://github.com/tks98/darkroom.git
cd darkroom
bash setup.sh                # add --starnet-dir DIR to include StarNet in the check
source .venv/bin/activate    # puts `darkroom` on the PATH
```

`setup.sh` creates `.venv`, installs the package into it, builds a copy of the GraXpert denoise model that runs on the Apple GPU (see [why](troubleshooting.md#graxperts-gpu-denoise-fails-on-macos)), and links the agent skill into `~/.claude/skills/darkroom`. Pass `--skill-dir DIR` to link it into another agent's skills folder instead, or `--no-skill` to skip it; see [using darkroom with an AI agent](agents.md). It finishes by running `darkroom doctor`. You can run it again at any time; it only redoes what is missing.

## StarNet

```sh
darkroom get-starnet
```

This finds the current StarNet2 command-line build for your platform on the official StarNet site, downloads it, prints its full licence and asks you to type `accept`. Only then does it install it, in darkroom's cache, with an `accepted.json` recording the version, the download address and when you accepted. If you do not accept, it deletes the download and installs nothing. `--dry-run` shows what it would download, `--list` shows what is installed, and `--zip FILE` installs a zip you downloaded from the StarNet site yourself.

StarNet2's licence lets you use it free of charge for astrophotography processing. It does not allow redistributing it or using its output to train or test other software. Read it in full when `get-starnet` shows it.

On Apple Silicon this installs StarNet2 2.6, which is signed and runs natively: about 10 seconds for the M31 image, against 50 to 75 seconds for the older StarNet v2.0.2 under Rosetta, with the same result to within 0.01 of an 8-bit level. darkroom still runs v2.0.2 if you point `DARKROOM_STARNET_DIR` or `[starnet] dir` at its folder; see [StarNet v2.0.2 and Gatekeeper](troubleshooting.md#starnet-v202-is-blocked-by-gatekeeper).

## Check the setup with `darkroom doctor`

```sh
darkroom doctor --work path/to/work --subs path/to/lights
```

`doctor` checks everything a run depends on and prints a fix for anything that is wrong. It looks at Python and its packages, the Siril and GraXpert versions, the denoise model, and whether the model really runs on the GPU (it times one batch on the GPU and one on the CPU). It checks that StarNet is there and whether macOS Gatekeeper is blocking it, whether Siril's catalogue server and ESA's Gaia archive answer, and, with `--work` and `--subs`, whether there is enough free disk for the stack.

A run on this machine takes about 12 seconds. A `WARN` is information; only a `FAIL` needs fixing before you process anything. `doctor` reports and changes nothing, with one exception: the first time, it builds the GPU copy of the denoise model in the cache directory.

Useful flags: `--run-starnet` also runs the old StarNet v2.0.2 on its bundled sample (StarNet2 is checked without running an image), `--offline` skips the network checks, and `--json` prints the results for scripts.

## Where darkroom looks for things

darkroom finds the external tools through environment variables. Defaults are the standard macOS install locations.

| Variable | Points to |
|---|---|
| `DARKROOM_SIRIL` | the `siril-cli` executable. Default: inside `/Applications/Siril.app` |
| `DARKROOM_GRAXPERT` | the GraXpert executable. Default: inside `/Applications/GraXpert.app` |
| `DARKROOM_GRAXPERT_MODEL` | the GraXpert denoise model file, if it is not where GraXpert keeps it |
| `DARKROOM_STARNET_DIR` | a StarNet folder to use instead of the one `get-starnet` installed; `[starnet] dir` and `--starnet-dir` take precedence over it |
| `DARKROOM_CACHE` | where the GPU copy of the denoise model is built. Default: `~/Library/Caches/darkroom` |

## Running the tests

```sh
.venv/bin/pip install -e '.[dev]'
pytest
```

The tests use synthetic arrays and fakes. They need no image data, no external tools and no network.
