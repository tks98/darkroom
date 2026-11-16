# darkroom

calibrate, stack, denoise, star-reduce and finish astrophotos from the command line

darkroom is a Python command-line pipeline that takes the raw sub-frames from a
deep-sky session and produces a finished image. It does not reimplement the
hard parts: it drives [Siril](https://siril.org) for calibration, stacking,
plate solving and colour calibration, [GraXpert](https://graxpert.com) for
background extraction and denoising, and
[StarNet](https://www.starnetastro.com) for star removal, then does the final
blend and finishing in Python. It started as a set of one-off scripts written
to process one night of M31 data from a ZWO Seestar S30 Pro; this repository is
those scripts turned into a package that reproduces the same result, so the
next target can be processed the same way.

![M31, the Andromeda Galaxy, with satellite galaxies M32 (below the core) and M110 (upper right). The galaxy runs diagonally from upper left to lower right, with a warm yellow core, bluer outer arms and visible dark dust lanes, in a dense field of small stars.](docs/images/m31_final.jpg)

M31 with M32 and M110. ZWO Seestar S30 Pro (160 mm focal length, Sony IMX585
one-shot-colour sensor, IR-cut filter, equatorial mode), gain 200, sensor
around 21 °C. 346 of 404 × 30 s sub-frames kept, 2 h 53 min total, taken on
the night of 2026-11-15. Calibrated with the Seestar's own 5-frame master dark
only; no flats or bias.

## Compared with the Seestar's own stack

![Side-by-side comparison of the same field of M31. Left, labelled "Seestar in-app stack": a grey-white galaxy with a soft core and faint, low-contrast dust lanes. Right, labelled "darkroom": the same galaxy with a yellow core, bluish outer arms, clearer dust lanes, smaller stars and a darker, more neutral background.](docs/images/m31_seestar_vs_darkroom.jpg)

Left is the stack the Seestar app built during the session (402 frames),
cropped to the same field as the darkroom result; right is darkroom. The
differences come mostly from colour calibration against Gaia star colours,
denoising before the stretch, and processing the galaxy and the stars as
separate layers so the stars can be made smaller without touching the galaxy.
Some of the colour on the right is cosmetic (see `finish` below).

![Three panels of the same M31 field. Left, "one 30 s sub": a grey frame where only the bright core and a few stars are visible. Middle, "stacked, quick stretch": the full galaxy and its satellites appear, mostly neutral in colour. Right, "finished": the final processed image.](docs/images/m31_progression.jpg)

## Pipeline

Each stage reads the previous stage's output from a work directory and writes
its own, so stages can be re-run individually.

| Stage | Tool | What it does | Output |
|---|---|---|---|
| `stack` | Siril 1.4.5 | Dark subtraction, hot/cold pixel correction, debayer, two-pass star registration, keeps the best ~90% of frames by weighted FWHM and by roundness, crops to the common area, winsorized-sigma rejection stack weighted by FWHM. About 3 minutes for the M31 data on an Apple Silicon Mac. | `stack.fit` |
| `bge` | GraXpert | AI background extraction; removes the sky gradient. | `bge.fits`, `bge_background.fits` |
| `color` | Siril | Plate solve, then spectrophotometric colour calibration (SPCC) against Gaia DR3 using the sensor and filter profiles. | `spcc.fit` |
| `denoise` | GraXpert model via onnxruntime | Runs GraXpert's denoise model (v3.0.2) directly through onnxruntime on the Apple GPU. | `denoised.fits` |
| `stretch` | Siril | Arcsinh stretch, linked autostretch, mild saturation. | `stretched.fit`, `stretched.tif`, `stretched.jpg` |
| `starnet` | StarNet v2 | Splits the image into a starless layer and a star layer. | `starless.tif` |
| `finish` | Python | Galaxy layer: midtone lift, saturation, a yellow/blue contrast push applied inside the galaxy only, masked unsharp mask, sky neutralisation. Star layer: shrunk with a gamma curve. Screen-blend recombination, then crop. | `final.tif`, `final.jpg` |

Logs from each external tool go to `logs/` in the work directory. The `stack`
stage also writes its calibrated and registered frames to `process/` there
(about 31 GB for the M31 data); they can be deleted once `stack.fit` exists.

Notes on what the M31 run showed:

- `color` matched 1,891 stars. Siril flagged the blue-channel fit as imprecise.
- `denoise` took 23 s on the GPU versus about 5 minutes on the CPU. It halved
  the pixel noise (robust neighbour-difference sigma 0.000100 to 0.000048).
  The output matches GraXpert's own CPU result to within 1e-5, apart from a
  constant offset.
- `finish`: the yellow/blue push is cosmetic. It makes the core and arms easier
  to tell apart; it is not photometric colour. Turn it down with `--yb`.
- `finish`: stars are reduced with a gamma curve, which dims halos more than
  cores. Morphological erosion was tried and rejected because it deletes faint
  stars. It remains available as `darkroom star-reduce` for when StarNet is not
  installed.

## Requirements

Tested only on macOS with Apple Silicon.

- Python 3.11
- [Siril](https://siril.org) 1.4.5 (`siril-cli`)
- [GraXpert](https://graxpert.com) 3.1, with the denoise model 3.0.2 downloaded
  through GraXpert
- [StarNet v2](https://www.starnetastro.com) command-line version

None of these are bundled. darkroom finds them through environment variables:

| Variable | Points to |
|---|---|
| `DARKROOM_SIRIL` | `siril-cli` executable (defaults to the standard macOS app location) |
| `DARKROOM_GRAXPERT` | GraXpert executable (defaults to the standard macOS app location) |
| `DARKROOM_STARNET_DIR` | folder containing the StarNet CLI and its weights (no default; `--starnet-dir` also works) |
| `DARKROOM_GRAXPERT_MODEL` | GraXpert denoise model file, if not in GraXpert's default location |
| `DARKROOM_CACHE` | where the fixed-batch copy of the denoise model is built on first use (defaults to `~/Library/Caches/darkroom`) |

## Install

```sh
git clone https://github.com/tks98/darkroom.git
cd darkroom
python3 -m venv .venv
.venv/bin/pip install -e .
source .venv/bin/activate    # puts `darkroom` on the PATH
```

To run the tests (synthetic arrays only, no image data or external tools
needed): `.venv/bin/pip install -e '.[dev]'`, then `pytest`.

## Usage

The M31 run, stage by stage. `examples/m31.sh` holds the full sequence.

```sh
darkroom stack   --subs path/to/lights --dark path/to/master_dark.fit --work m31
darkroom bge     --work m31
darkroom color   --work m31 --sensor "Sony IMX585" --filter "UV/IR Block" --focal 160 --pixel 2.9
darkroom denoise --work m31 --strength 0.6
darkroom stretch --work m31
darkroom starnet --work m31
darkroom finish  --work m31 --crop 0,512,1710,2565
```

Or all at once, and later from any stage onward:

```sh
darkroom run --work m31 --subs path/to/lights --dark path/to/master_dark.fit --crop 0,512,1710,2565
darkroom run --work m31 --from finish --crop 0,512,1710,2565
```

Look at the output of each stage before moving on; most problems (a bad
background model, a failed plate solve, StarNet artefacts) are visible
immediately and are cheaper to fix there than at the end.

### Options

```
darkroom stack   --subs DIR --dark FILE --work DIR
darkroom bge     --work DIR [--smoothing 0.1]
darkroom color   --work DIR [--sensor "Sony IMX585"] [--filter "UV/IR Block"] [--focal 160] [--pixel 2.9] [--catalog gaia]
darkroom denoise --work DIR [--strength 0.6] [--cpu]
darkroom stretch --work DIR [--asinh 25] [--shadows -2.5] [--target-bg 0.10] [--sat 0.35]
darkroom starnet --work DIR [--starnet-dir DIR]
darkroom finish  --work DIR [--gamma 1.6] [--star-strength 0.95] [--mid 0.45] [--sat 1.2] [--yb 1.8]
                            [--sharpen 0.8] [--sharpen-radius 2.0] [--black 0.02] [--erode 0]
                            [--no-neutral] [--crop x,y,w,h] [--out final]
darkroom run     --work DIR [--subs DIR --dark FILE] [--from STAGE] [--to STAGE]
                            [--starnet-dir DIR] [--cpu] [--crop x,y,w,h]
darkroom star-reduce IN OUT [--amount 0.85] [--iters 2] [--radius 6] [--k 4.0] [--mask FILE]
                            # morphological star reduction, for when StarNet isn't available
```

`STAGE` is one of `stack`, `bge`, `color`, `denoise`, `stretch`, `starnet`,
`finish`. `darkroom <command> --help` lists every option.

Defaults are the values used for the M31 image. They are a starting point for
similar data, not tuned for anything else.

## Known issues

- **GraXpert GPU denoise on macOS.** GraXpert 3.1.0rc2's CoreML denoise fails
  on macOS 27, and its bundled onnxruntime is slower than the CPU path when
  forced onto the GPU. darkroom works around this by running a fixed-batch copy
  of the same model with onnxruntime 1.32. `--cpu` uses the CPU instead.
- **Catalogue server unreachable.** During the M31 run Siril's online
  catalogue host (VizieR TAP) could not be reached, so plate solving failed.
  It worked after a Gaia DR3 star list for the field was fetched from ESA's
  Gaia archive and placed in Siril's download cache. That step was manual; an
  automatic fallback is planned but not built.
- **`siril-cli` working directory.** `siril-cli` starts in `~/Pictures`, so
  scripts have to be passed by absolute path. darkroom does this; it matters
  only if you run the scripts in `siril/` by hand.
- **StarNet on macOS.** The StarNet CLI for macOS is an unsigned x86 binary
  that runs under Rosetta. Gatekeeper blocks it until you choose to allow it.
  Whether to do that is your decision. Clearing the quarantine flag on the
  StarNet folder (`xattr -dr com.apple.quarantine <folder>`) is what allows it
  to run. Without StarNet, `darkroom star-reduce` gives a weaker,
  morphological alternative.
- **Siril preferences.** The Siril scripts change settings that Siril may
  keep: `stack` switches processed images to 16-bit (`set16bits`), and the
  plate solve in `color` saves the focal length and pixel size it was given as
  Siril's defaults.

## Limitations

- Tested on one target (M31), one camera (Seestar S30 Pro) and one platform
  (macOS on Apple Silicon).
- One-shot-colour data only. Flats, bias frames and mono/LRGB data are not
  supported yet.
- Calibration uses a master dark you supply; there is no automatic dark
  selection.

## Status

Phase 1 of 5. Done: the one-off scripts ported into a package and CLI. Run
end to end on the 404 M31 subs, it reproduces the original result exactly:
every stage's pixel data is identical to the hand-run pipeline's, and the
final TIFF and JPEG are byte-identical. The `--cpu` denoise path and a
non-editable install have not been tested.

Planned:

- `darkroom doctor` to check the environment, and a setup script
- a per-target config file; camera presets, starting with the Seestar S30 Pro
- automatic dark selection
- a catalogue fallback that works for any field
- a stack-framing option
- skipping stages whose output is already up to date
- a Claude Code skill describing the workflow and the points where the output
  should be checked by eye
- validation on a second target (M13)

## Licence

Copyright (C) 2026 tks98

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE).

It is GPL because the denoise stage reimplements GraXpert's GPL-3 `denoise()`
algorithm.

## Credits

darkroom only orchestrates other people's work. None of the following is
redistributed here; install each yourself.

- [Siril](https://siril.org): calibration, stacking, plate solving, SPCC and
  stretching.
- [GraXpert](https://graxpert.com): background extraction, and the denoise
  model and algorithm used by the `denoise` stage.
- [StarNet](https://www.starnetastro.com): star removal.
- Star positions and colours from the European Space Agency mission
  [Gaia](https://www.cosmos.esa.int/gaia), processed by the Gaia Data
  Processing and Analysis Consortium, retrieved from the
  [ESA Gaia archive](https://gea.esac.esa.int/archive/).
