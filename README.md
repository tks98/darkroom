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
cooling from 21 °C to 9 °C over the session. 346 of 404 × 30 s sub-frames
kept, 2 h 53 min total, taken on the night of 2026-11-15. Calibrated with the
Seestar's own 5-frame master dark only; no flats or bias.

## Compared with the Seestar's own stack

![Side-by-side comparison of the same field of M31. Left, labelled "Seestar in-app stack": a grey-white galaxy with a soft core and faint, low-contrast dust lanes. Right, labelled "darkroom": the same galaxy with a yellow core, bluish outer arms, clearer dust lanes, smaller stars and a darker, more neutral background.](docs/images/m31_seestar_vs_darkroom.jpg)

Left is the stack the Seestar app built during the session (402 frames),
cropped to the same field as the darkroom result; right is darkroom. The
differences come mostly from colour calibration against Gaia star colours,
denoising before the stretch, and processing the galaxy and the stars as
separate layers so the stars can be made smaller without touching the galaxy.
Some of the colour on the right is cosmetic (see `finish` below).

![Three panels of the same M31 field. Left, "one 30 s sub": a grey frame where only the bright core and a few stars are visible. Middle, "stacked, quick stretch": the full galaxy and its satellites appear, mostly neutral in colour. Right, "finished": the final processed image.](docs/images/m31_progression.jpg)

## A second target: M13

![Two panels of the globular cluster M13 at the same scale. Left, "one 30 s sub": a faint grey smudge with a few stars on a noisy grey-brown sky. Right, "darkroom, 15 subs": the cluster resolved into hundreds of stars with yellow and blue ones visible, on a dark neutral sky.](docs/images/m13_sub_vs_darkroom.jpg)

M13, from 18 × 30 s frames taken before cloud ended the session; 15 were
stacked (7.5 minutes). A globular cluster is made of stars, so the galaxy
workflow is wrong for it: StarNet removes the whole cluster, and shrinking
the stars dims the target. The `cluster` look keeps the stars, skips StarNet,
and uses a stronger arcsinh stretch that keeps the core resolved. The crop is
`crop = "1:1"` with `crop_scale = 0.45`, because the cluster is only a few hundred
pixels across in a 2160×3840 frame.

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
| `DARKROOM_STARNET_DIR` | folder containing the StarNet CLI and its weights (no default; `[starnet] dir` in the config or `--starnet-dir` also work) |
| `DARKROOM_GRAXPERT_MODEL` | GraXpert denoise model file, if not in GraXpert's default location |
| `DARKROOM_CACHE` | where the fixed-batch copy of the denoise model is built on first use (defaults to `~/Library/Caches/darkroom`) |

## Install

```sh
git clone https://github.com/tks98/darkroom.git
cd darkroom
bash setup.sh                # add --starnet-dir DIR to include StarNet in the check
source .venv/bin/activate    # puts `darkroom` on the PATH
```

`setup.sh` creates `.venv`, installs the package, builds the fixed-batch copy
of the denoise model, links the Claude Code skill (see below; `--no-skill`
skips it) and runs `darkroom doctor`. It can be run again at any time.

`darkroom doctor` checks everything a run depends on and prints a fix for
whatever is wrong: Python and its packages, Siril, GraXpert, the denoise
model, that the model really runs on the GPU, StarNet (including whether
Gatekeeper is blocking it), the catalogue servers, and free disk space
(`--work DIR --subs DIR`). It only reports; it changes nothing on the system.

To run the tests (synthetic arrays and fakes only; no image data, external
tools or network): `pytest`.

## Usage

Describe the target once, then run:

```sh
darkroom init --work m31 --subs path/to/lights --dark-library path/to/darklibrary
# look over m31/darkroom.toml: set [starnet] dir and [finish] crop
darkroom run  --work m31
```

`init` reads the subs' FITS headers, detects the camera preset, and writes a
commented `darkroom.toml` in the work directory that lists every setting and
where its value comes from. It prints a summary: frame count, total
integration, sensor temperature range, and the master dark it would pick. Use
`--dark FILE` for a specific master dark or `--no-dark` for none.

`run` executes the stages in order and skips any stage whose output is up to
date. After changing a setting, run it again: only the stages affected by the
change are redone. `--from STAGE` forces that stage and everything after it,
`--to STAGE` stops early, `--force` redoes everything.

Every stage is also a command of its own, and flags override the config file:

```sh
darkroom run    --work m31 --to stack              # stop after stacking and look at it
darkroom finish --work m31 --gamma 1.4 --out try   # try a variant without touching final.*
darkroom stack  --work m31 --reuse-registration --framing cog --out stack_cog
```

Look at the output of each stage before moving on; most problems (a bad
background model, a failed plate solve, StarNet artefacts) are visible
immediately and are cheaper to fix there than at the end.

`examples/m31.toml` and `examples/m13.toml` are the configurations for the two
images above; `examples/m31.sh` is the M31 run written as individual commands.

### Settings

Precedence: command-line flag > `darkroom.toml` > look preset > camera preset >
built-in default. Unknown keys and bad values are errors that name the file
and key.

| Section | Keys |
|---|---|
| `[target]` | `name`, `subs`, `dark` (`"auto"`, `"none"` or a path), `dark_library`, `camera`, `look` |
| `[stack]` | `framing` (`min`, `cog`, `max`), `keep_fwhm`, `keep_round`, `keep_bkg`, `keep_stars` (percent of frames kept), `clean` |
| `[bge]` | `smoothing` |
| `[color]` | `sensor`, `filter`, `focal`, `pixel`, `catalog`, `whiteref`, `catalog_fallback` |
| `[denoise]` | `strength`, `cpu` |
| `[stretch]` | `asinh`, `shadows`, `target_bg`, `sat` |
| `[starnet]` | `dir` |
| `[finish]` | `gamma`, `star_strength`, `mid`, `sat`, `yb`, `sharpen`, `sharpen_radius`, `black`, `erode`, `neutral`, `crop`, `crop_center`, `crop_scale`, `stars` |

- **Dark selection.** With `dark = "auto"`, darkroom picks from the dark
  library the master with the same exposure and gain and the nearest sensor
  temperature (ties go to the newest), and warns when the nearest is more
  than 5 °C away. The library layout is the Seestar's:
  `<library>/<exposure>s/MasterDark_*.fit`.
- **Framing.** `min` keeps the area common to all frames. `cog` keeps the full
  sensor frame at the frames' centre of gravity; on the M31 data that is
  2160×3840 instead of 1710×3632, with edge strips about 20% noisier. `max`
  keeps the bounding box of all frames (2609×4050): its edges are ragged and
  much noisier, about 1% of it is never covered, and it has to be cropped.
- **Frame selection.** `keep_fwhm` and `keep_round` (90 by default) keep the
  sharpest and roundest frames. `keep_bkg` and `keep_stars` (100, off) can
  also drop the frames with the brightest sky or the fewest stars, for
  cloud. Compare before adopting them: on the M13 data, where thin cloud
  crossed several frames, dropping the 3 stacked frames with the brightest
  sky and fewest stars (15 down to 12) left the gradient unchanged and raised
  the noise by 6%, because rejection and normalisation had already coped.
- **Crop.** `crop = "2:3"` takes the largest box of that shape, centred on the
  brightest part of the blurred image; `crop_scale` (0 to 1) shrinks that box
  for a target that is small in the frame, and `crop_center = "x,y"`
  overrides the centre. `"x,y,w,h"` crops by pixels.
- **Stars.** `stars = "reduce"` is the galaxy workflow: StarNet removes the
  stars, the rest is enhanced, and the stars go back smaller. `stars = "keep"`
  does none of that and StarNet is not run; `mid`, `sat`, `black`, `neutral`
  and the crop still apply, to the whole image.
- **Presets.** One camera (`seestar-s30-pro`) and two looks are shipped, in
  `presets/`: `galaxy` (the M31 settings) and `cluster` (the M13 settings).
  `camera` and `look` also accept the path of your own `.toml`.

`darkroom <command> --help` lists every flag.

## Claude Code skill

`skill/SKILL.md` describes this workflow for [Claude Code](https://claude.com/claude-code):
what to check by eye after each stage, how to compare variants before
choosing a setting, the known failures, and what must not be shared. `setup.sh`
links it into `~/.claude/skills/darkroom` so it is available from any folder.

## Known issues

- **GraXpert GPU denoise on macOS.** GraXpert 3.1.0rc2's CoreML denoise fails
  on macOS 27, and its bundled onnxruntime is slower than the CPU path when
  forced onto the GPU. darkroom works around this by running a fixed-batch copy
  of the same model with onnxruntime 1.32. `--cpu` uses the CPU instead.
- **Catalogue server unreachable.** Siril's online catalogue host (VizieR TAP)
  was unreachable on both days this was developed, which makes plate solving
  fail. The `color` stage detects that failure, fetches the Gaia DR3 star list
  for the field from ESA's Gaia archive into Siril's download cache, and
  retries. It adds about a minute, mostly Siril's own timeout.
  `--no-catalog-fallback` turns it off. The request contains sky coordinates
  only.
- **Master dark temperature.** The Seestar's sensor is not cooled. During the
  M31 session it fell from 21.4 °C to 8.6 °C (median 10.9 °C), while the only
  master dark was taken at 21.5 °C. darkroom warns about a mismatch like this.
  For this data it mattered little: a stack without the dark was 2% noisier
  in the background and kept some isolated hot pixels.
- **`siril-cli` working directory.** `siril-cli` starts in `~/Pictures`, so
  scripts have to be passed by absolute path. darkroom does this; it matters
  only if you run the scripts in `siril/` by hand.
- **StarNet on macOS.** The StarNet CLI for macOS is an unsigned x86 binary
  that runs under Rosetta. Gatekeeper blocks it until you choose to allow it.
  Whether to do that is your decision. Clearing the quarantine flag on the
  StarNet folder (`xattr -dr com.apple.quarantine <folder>`) is what allows it
  to run; `darkroom doctor` reports the flag and never clears it. Without
  StarNet, `darkroom star-reduce` gives a weaker, morphological alternative.
- **Siril preferences.** The Siril scripts change settings that Siril may
  keep: `stack` switches processed images to 16-bit (`set16bits`), and the
  plate solve in `color` saves the focal length and pixel size it was given as
  Siril's defaults.

## Limitations

- Tested on two targets (M31 and M13), one camera (Seestar S30 Pro) and one
  platform (macOS on Apple Silicon). Each look is one image's settings: the
  `cluster` look comes from 7.5 minutes of data. A nebula will need its own.
- One-shot-colour data only. Flats, bias frames and mono/LRGB data are not
  supported yet.
- Automatic dark selection was tested against a library built from copies of
  one real master dark, not against a full Seestar dark library.
- Not tested: the `--cpu` denoise path, a non-editable (wheel) install,
  `setup.sh` creating a fresh `.venv`, the `LP` filter mapping, and
  `keep_fwhm` / `keep_round` values other than 90.

## Status

All five planned phases are done.

1. Port of the original one-off scripts into a package and CLI.
2. `darkroom doctor` and `setup.sh`.
3. `darkroom.toml`, `darkroom init`, presets, automatic dark selection, the
   catalogue fallback, stack framing, aspect-ratio crop, and skipping stages
   that are up to date.
4. The Claude Code skill.
5. A second target (M13), which added the `cluster` look, `stars = "keep"`,
   `crop_scale`, the `keep_bkg` / `keep_stars` frame filters, and a fix to sky
   neutralisation.

After phase 1 and again after phase 3 (driven by `darkroom.toml`, with
automatic dark selection and `crop = "2:3"`) the pipeline was run end to end
on the 404 M31 subs. Both times every stage's pixel data was identical to the
original hand-run pipeline's, and the final TIFF and JPEG were byte-identical.

Phase 5 changed the last step on purpose. Sky neutralisation used to measure
the sky colour from the darker half of the sky pixels, which reports a colour
cast that is not there when one channel is noisier than the others: it added
a visible blue cast to M13. It now uses every sky pixel. On M31 this moves
the final image by at most 0.0014 (a third of one 8-bit level); every stage
before `finish` is still identical. The M31 images in this README were made
before that fix.

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
