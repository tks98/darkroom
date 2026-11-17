# Notes for AI agents

This file is for coding agents that read `AGENTS.md`. People should start with the [README](README.md).

## Processing images

To stack, process, tune or debug an image with darkroom, follow [skill/SKILL.md](skill/SKILL.md). It is written for any agent that can run shell commands, and it covers what to check after each stage, how to compare variants before choosing a setting, and the known failures. If your agent loads skills from a folder, link `skill/` into it with `bash setup.sh --skill-dir <your agent's skills folder>`.

## Working on the code

- Set up with `bash setup.sh --no-skill` (or build the Docker image, see [docs/docker.md](docs/docker.md)), then run the tests with `.venv/bin/pytest -q`. They use synthetic arrays and fakes, need no image data, external tools or network, and take about a second.
- `darkroom doctor` shows whether Siril, GraXpert, StarNet and the GPU are usable on this machine. The full pipeline needs real subs and takes minutes, so do not run it to check a code change unless the change is to a stage.
- Layout: `darkroom/` is the package (`cli.py` and `stages.py` run the stages, `config.py` resolves settings, `siril.py` renders and runs the Siril scripts, `tools.py` finds the external tools, `finish.py`, `denoise.py`, `star_reduce.py`, `catalog.py`, `darks.py` and `doctor.py` do what their names say), `siril/` holds the Siril script templates, `presets/` the camera and look presets, and `docs/` the user documentation.
- Settings have five sources, highest first: flag, `darkroom.toml`, look preset, camera preset, built-in default. A new setting needs an entry in `config.SCHEMA`, a flag in `cli.py`, and a line in [docs/configuration.md](docs/configuration.md). The built-in defaults must stay equal to the shipped presets; a test checks this.
- The skip logic records each stage's settings. Record a new setting only when it differs from its default, so existing work directories stay up to date.
- Changes to the image maths should keep the M31 result unless the change is a deliberate fix. Say in the commit and in [docs/history.md](docs/history.md) how much a fix moves the output.

## Privacy

FITS and TIFF headers from these cameras contain the observing site's latitude and longitude and the telescope's device ID, and logs and `darkroom.toml` contain local paths. Never commit FITS, TIFF, logs or work-directory configs; `.gitignore` blocks them. Images for the docs must be JPG or PNG rebuilt from pixel data, with no EXIF, XMP or ICC metadata. Keep home-directory paths, usernames and coordinates out of every committed file.

## StarNet

StarNet's licence allows darkroom to run the StarNet command line, but only when the user has downloaded it from the official site and accepted its terms. So:

- Never put StarNet, its weights or its output images in the repository, the Docker image, a test fixture or a reference result. Its licence forbids using its output to test or validate other software.
- Never accept its licence on the user's behalf (for example by passing `--accept-license` to `darkroom get-starnet`). Show the user the terms and let them accept.
- The old StarNet v2.0.2 for macOS is unsigned. Whether to clear its quarantine flag is the user's decision: explain it, and never run `xattr` on it yourself.
