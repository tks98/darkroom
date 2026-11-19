# Installing darkroom

On a Mac, install darkroom natively as below. On Linux or Windows, use the [Docker image](docker.md), which has everything inside. darkroom has been tested on macOS with Apple Silicon; the native installer also knows the Intel Mac and x86-64 Linux builds, but those have not been tried on real machines.

## Install

You need Python 3.11 or later. Everything else is downloaded for you.

```sh
git clone https://github.com/tks98/darkroom.git
cd darkroom
bash setup.sh                # add --starnet-dir DIR if you already have StarNet somewhere
source .venv/bin/activate    # puts `darkroom` on the PATH
```

`setup.sh` creates `.venv`, installs the package into it, builds a copy of the GraXpert denoise model that runs on the Apple GPU (see [why](troubleshooting.md#graxperts-gpu-denoise-fails-on-macos)), links the agent skill into `~/.claude/skills/darkroom` (`--skill-dir DIR` for another agent, `--no-skill` to skip; see [using darkroom with an AI agent](agents.md)), and runs `darkroom setup`, described next. You can run it again at any time; it only redoes what is missing.

## `darkroom setup`

```sh
darkroom setup
```

This gets the external tools in place:

1. It looks for [Siril](https://siril.org) 1.4.5 and [GraXpert](https://graxpert.com) 3.1. If you already have working copies (in `/Applications` on a Mac), it uses them and installs nothing. Otherwise it downloads the official build for your platform, checks its size and SHA-256 against values pinned in darkroom, and installs it in darkroom's own tools folder (`~/Library/Application Support/darkroom/tools` on a Mac), leaving the rest of your system alone.
2. It makes GraXpert download its denoise model (`darkroom fetch-models`).
3. If no StarNet is found, it offers to install StarNet2 with `darkroom get-starnet`, which shows you StarNet's licence and installs it only if you accept.
4. It runs `darkroom doctor`.

`--dry-run` shows what it would download and where, without downloading anything. `--force` installs darkroom's own copy even when one is already present; that copy then takes precedence. `--from DIR` installs from official files you already downloaded (still checked against the pinned hashes).

### macOS security checks

A file downloaded by a program rather than a browser is not marked as downloaded, so macOS would never check it. darkroom does not take advantage of that: it marks the apps it installs exactly as a browser would, so macOS's Gatekeeper decides about them as usual.

Siril's Mac build is signed and notarized by Apple, so Gatekeeper accepts it and it just works. GraXpert's Mac build is not notarized. Before installing it, `setup` tells you so and asks you to type `yes` (or pass `--allow-unnotarized`). After that, macOS blocks it until you allow it yourself: run it once, then open System Settings, Privacy & Security, and click Open Anyway. darkroom never lifts that block for you, and `doctor` reports it until you do.

### Where darkroom finds the tools

For Siril and GraXpert: the environment variable (`DARKROOM_SIRIL`, `DARKROOM_GRAXPERT`) if set, then a copy installed by `darkroom setup`, then the standard location (`/Applications` on a Mac).

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
| `DARKROOM_DECONV_MODEL` | GraXpert's object deconvolution model (1.0.1), for the optional `deconv` stage, if it is not where GraXpert keeps it |
| `DARKROOM_STARNET_DIR` | a StarNet folder to use instead of the one `get-starnet` installed; `[starnet] dir` and `--starnet-dir` take precedence over it |
| `DARKROOM_CACHE` | where the GPU copy of the denoise model is built and where `get-starnet` installs StarNet2. Default: `~/Library/Caches/darkroom` |
| `DARKROOM_TOOLS_DIR` | where `darkroom setup` installs Siril and GraXpert. Default: `~/Library/Application Support/darkroom/tools` |

## Running the tests

```sh
.venv/bin/pip install -e '.[dev]'
pytest
```

The tests use synthetic arrays and fakes. They need no image data, no external tools and no network. GitHub runs them on every pull request and every push to `main`, on Linux with Python 3.11 and 3.13 and on macOS with Python 3.12 (`.github/workflows/tests.yml`).
