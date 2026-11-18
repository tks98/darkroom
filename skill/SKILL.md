---
name: darkroom
description: Process deep-sky astrophotography sub-frames into a finished image with the darkroom CLI (Siril, GraXpert, StarNet). Use when the user asks to stack, process, edit or finish astro subs or lights (for example from a ZWO Seestar), to reprocess or tune an image made with darkroom, or to debug a darkroom stage. Not for planetary, lunar or solar video stacking.
---

# darkroom: sub-frames to finished deep-sky image

darkroom is a command-line pipeline. Each stage reads the previous stage's
file from a work directory and writes its own:

| Stage | Tool | Output |
|---|---|---|
| `stack` | Siril: calibrate, register, reject, stack | `stack.fit` |
| `bge` | GraXpert background extraction | `bge.fits` |
| `color` | Siril plate solve + SPCC colour calibration | `spcc.fit` |
| `denoise` | GraXpert denoise model on the GPU | `denoised.fits` |
| `stretch` | Siril arcsinh + autostretch | `stretched.fit/.tif/.jpg` |
| `starnet` | StarNet2 star removal | `starless.tif` |
| `finish` | Python: enhance, shrink stars, recombine, crop | `final.tif/.jpg` |

The tool does the numbers. Your part is looking at the images and deciding
whether each step improved them, which the tool cannot do.

This skill works with any agent that can run shell commands. It also assumes
you can look at images. If you cannot, report the measurements this skill asks
for (frames kept, sky levels, clipped pixels, noise), make the preview JPGs
anyway, and ask the user to look at them before you move on.

## Before you start

0. If Siril, GraXpert or the denoise model are missing, `darkroom setup`
   installs them (on Linux or Windows, use the Docker image instead). It asks
   before installing GraXpert, whose Mac build Apple has not notarized; that
   answer, and allowing it to run afterwards, are the user's to give, so never
   pass `--allow-unnotarized` for them.
1. Find the repo: this skill's folder is `<repo>/skill`, often reached through
   a symlink in the agent's skills folder, so resolve the link. Use
   `<repo>/.venv/bin/darkroom`.
   If there is no `.venv`, run `bash <repo>/setup.sh` first.
2. Run `darkroom doctor --work WORK --subs SUBS`. Fix every FAIL before
   processing; a WARN is information, not a blocker. See "Known failures".
3. Survey the data before promising anything: count the `.fit` subs, read one
   FITS header (exposure, gain, temperature, filter, Bayer pattern, target),
   and look at a few of the camera's preview JPGs spread across the session.
4. Never write inside the folder that holds the user's raw subs, and never
   delete or move them. Work in a separate work directory on a disk with room:
   stacking needs about 80 MB per sub (roughly 31 GB for 400 subs).

## Running it

```sh
darkroom init --work WORK --subs SUBS --dark-library DARKLIB   # or --dark FILE, or --no-dark
# look over WORK/darkroom.toml: [starnet] dir, [finish] crop
darkroom run --work WORK --to stack        # first time: one stage at a time
darkroom run --work WORK                   # later: everything that is out of date
```

`init` reads the FITS headers, detects the camera preset, picks the master
dark (same exposure and gain, nearest temperature) and writes a commented
`WORK/darkroom.toml` listing every setting and where its value comes from.
Read its summary: frame count, total integration, sensor temperature range,
and any warning.

`run` executes the stages in order and skips any stage whose output is up to
date, so after changing a setting just run it again; only the affected stages
rerun. `--from STAGE` forces that stage and everything after it; `--force`
forces all. Every stage is also a command of its own (`darkroom finish --work
WORK --gamma 1.4`). Precedence: flag > `darkroom.toml` > look preset > camera
preset > built-in default.

`stack` refuses to run when `WORK/process` already holds frames from an
earlier run. `--force` clears that folder first; `--reuse-registration`
re-frames and re-stacks the frames already there.

Logs are in `WORK/logs/`. When a stage fails, read its log before changing
anything.

## Checkpoints: look before moving on

Run stage by stage the first time on a new target (`--to STAGE`), and check
each result. Make a JPG preview, view it, and say what you see. Problems are
cheap to fix at the stage that caused them and expensive to notice at the end.

### After `stack`
- How many frames were registered and kept? (`logs/stack.log`.) Losing more
  than about 20% deserves a look at why: cloud, wind, trailing.
- Quick-stretch the stack and view it at full frame and at 1:1 in the centre
  and one corner. Stars should be round to the corners.
- If the camera made its own stack of the same session, put the two side by
  side. darkroom should be at least as good; if it is not, stop and find out
  why before going on.
- Cloud: the default filters rank frames by star sharpness and roundness
  only. Fully clouded frames fail registration by themselves; frames with
  thin cloud can get through. Before reaching for `[stack] keep_bkg` /
  `keep_stars`, compare a filtered restack
  (`darkroom stack --work WORK --reuse-registration --keep-bkg 70 --keep-stars 70 --out stack_clean`)
  against the default for noise and gradient. On M13, dropping the 3 frames with
  the brightest sky and fewest stars (15 down to 12) left the gradient
  unchanged and made the stack 6% noisier.
- Dark temperature: if `init` or `stack` warns that the master dark is more
  than 5 °C from the subs, also make a no-dark stack and compare noise and
  hot pixels before choosing. (On the M31 data the dark was 10.6 °C off; the
  no-dark stack was only 2% noisier but left isolated hot pixels.)
- Framing: the default (`min`) keeps only the area common to all frames. If
  the mount drifted and that crop cuts into the target, compare
  `darkroom stack --work WORK --reuse-registration --framing cog --out stack_cog`.
  `cog` gives the full sensor frame with edges about 20% noisier. `max` gives
  the bounding box of all frames, with ragged, much noisier edges and empty
  corners: it must be cropped, and its brightness scale differs from the
  others. To adopt a framing, set `[stack] framing` and restack.

### After `stack` on a mosaic
- `init` sets `[stack] mosaic = true` when the pointings in the headers
  spread over more than a quarter of the field; `stack` warns when it looks
  like a mosaic but the setting is off. Do not stack a mosaic without it.
- Check "N of M frames plate-solved" and "frames stacked". On the Rosette
  mosaic 139 of 151 solved and 127 were stacked; the unsolved ones were mostly
  frames with few stars. Far fewer solved means a wrong `[color] focal` or
  `pixel`, or catalogues that could not be fetched (`logs/stack_solve.log`).
- Look at a quick stretch of `stack_full.fit` (the whole canvas) for seams:
  tiles of different brightness or colour, or steps along frame edges. The
  stack is 32-bit to avoid exactly that; if it shows anyway, stop and report.
- Look at the crop it printed. `stack.fit` is the largest rectangle with data
  everywhere; for an irregular mosaic it can be well under half the canvas
  (39% on Alnilam). If the crop cuts the target, tell the user and offer to
  crop `stack_full.fit` by hand instead.

### After `bge`
- The background should be even. Measure the sky in a coarse grid away from
  the target; the blocks should agree within a few percent.
- Look at `bge_background.fits`: it should be a smooth gradient. If it shows
  the shape of the target, the model has eaten real signal; raise
  `[bge] smoothing`.

### After `color`
- Read the SPCC lines in `logs/color.log`: how many stars were used, and
  whether Siril warned that the fit was imprecise. Report the warning; do not
  hide it.
- The plate-solve result (pixel scale, field centre) should match the camera.

### After `denoise`
- Compare a 1:1 crop before and after. Noise should drop by roughly half at
  the default strength without the faint structure turning to plastic. Lower
  `[denoise] strength` if detail is being smoothed away.

### After `stretch`
- The sky should be dark grey, not black, and the brightest core should not
  be pure white. A flat, washed-out target means the arcsinh factor is too
  high; a blown core means the autostretch is too aggressive.

### After `starnet`
- View `starless.tif`. Look for blotches or holes where bright stars were, and
  for real detail (star clouds, small galaxies) that went with the stars.

### After `finish`
Check, at 1:1, the core, each corner, any companion objects, and an outer
part of the target:
- Background neutral and even. Measure R, G and B as the median of all the
  pixels in a patch of sky. Do not measure only the darkest pixels: that is
  biased toward the noisiest channel and shows a cast that is not there.
- No clipped highlights.
- Stars smaller but all still present. If faint stars vanished, the star
  reduction is too strong.
- Colour plausible for the object, and no colour fringe where the galaxy mask
  ends.

## Tuning: always compare variants

Never tune a look by changing one number and declaring it better. For every
aesthetic setting, render two or three variants, put them side by side with
the current result (full frame, plus 1:1 crops of the regions that matter),
view the sheet, and choose. Then say which you chose and what was wrong with
the others. `finish` takes about a second, so variants are cheap:

```sh
darkroom finish --work WORK --gamma 1.4 --out try_g14
darkroom finish --work WORK --gamma 2.0 --out try_g20
```

What the main `finish` settings do, and what going too far looks like:

| Setting | Effect | Too far |
|---|---|---|
| `gamma` (star layer) | >1 shrinks stars by dimming halos more than cores | around 2.0 the field looks thinned out |
| `erode` | morphological star shrink | any value deletes the faint 1 to 2 px stars; leave at 0 |
| `mid` | <0.5 brightens the starless layer | core blows out, background lifts |
| `sat` | overall saturation of the starless layer | noise turns into colour blotches |
| `yb` | stretches yellow/blue differences inside the target only | core goes green-yellow, arms purple (seen at 2.6 on M31) |
| `core` | lowers a near-white core, keeps its detail | flat, greyish core (0.4 on M31) |
| `sharpen` | masked unsharp mask on the target | halos along dust lanes, grain |
| `crop` | `"W:H"` (largest box of that shape centred on the brightest part of the target), or `"x,y,w,h"` | target touching the edge; use `crop_center` if the centre is wrong |

`yb` is cosmetic. It makes regions easier to tell apart; it is not measured
colour. Say so when you describe the result.

## Choosing a look

Two looks are shipped. Pass `--look` to `init`, or set `[target] look`.

- `galaxy` (the default): StarNet removes the stars, the galaxy is
  enhanced, the stars go back smaller. Made on M31.
- `cluster`: for targets that are made of stars. StarNet would remove the
  target itself and star reduction would dim it, so the look sets
  `[finish] stars = "keep"`: StarNet is not run, and `gamma`,
  `star_strength`, `yb`, `core`, `sharpen` and `erode` do nothing. It uses a stronger
  arcsinh stretch (100) so the core stays resolved. Made on M13 from 7.5
  minutes of data.

If you are unsure which applies, run the galaxy look once and view
`starless.tif`: if the target is gone from it, use `stars = "keep"`.

For a cluster, tune `[stretch] asinh` first (too low washes the core to
white, too high mottles the sky with colour noise), then `[finish] sat` and
`black`. A small target needs `crop = "1:1"` (or another shape) with
`crop_scale` below 1.

A nebula has no look yet. Expect to tune `stretch` and `finish` from scratch,
and tell the user that each look was made from a single image.

## Known failures

### GraXpert's own GPU denoise fails on recent macOS

darkroom does not use it; `denoise` runs the same model through onnxruntime.
If `doctor` says the GPU check fell back to CPU, the stage still works, about
15× slower.

### Plate solve fails with a catalogue error

Siril's catalogue server (VizieR) is sometimes unreachable; `doctor` shows it
as a WARN. `color` then fetches Gaia DR3 from ESA's archive by itself and
retries (about a minute longer, mostly Siril's timeout) and says so in its
output. If both servers are down, wait: do not skip colour calibration.

### `siril-cli` starts in `~/Pictures`

darkroom passes absolute paths. If you write a Siril script by hand, do the
same.

### StarNet is not installed

`darkroom get-starnet` downloads StarNet2 from its official site and installs
it after the user accepts its licence. The acceptance must be the user's own:
show them that the licence will be printed and that they must type `accept`,
and never pass `--accept-license` for them. Until then, the `cluster` look and
`darkroom star-reduce` work without StarNet.

### StarNet v2.0.2 is blocked by Gatekeeper

The old macOS StarNet v2.0.2 is an unsigned binary. `doctor` reports the
quarantine flag and the command that clears it (`xattr -dr
com.apple.quarantine <folder>`). That is a security decision: explain it and
let the user run the command. Do not run it yourself. StarNet2 2.6 from
`get-starnet` is signed and avoids this.

### Running in Docker

`docker/darkroom-docker` runs the Linux image with the user's folders at
`/subs`, `/darks` and `/work`; use those paths inside the container. Under
emulation on Apple Silicon, Siril's colour calibration (SPCC) crashes; run
the `color` stage natively there, or use the image on an x86-64 machine. See
`docs/docker.md`.

### Shell is often zsh

Unquoted variables are not split into words; quote paths (the Seestar's
folders contain spaces) and pass arguments explicitly.

## Housekeeping

- `WORK/process/` holds the calibrated and registered frames, the bulk of the
  disk use. It is only needed to restack. Ask before deleting it, or set
  `[stack] clean = true` if the user wants that every time.
- Keep the linear files (`stack.fit` … `denoised.fits`): reprocessing from
  them is fast. Keep `darkroom.toml` with the result; it is the record of how
  the image was made.

## Privacy

FITS and TIFF headers from these cameras contain the observing site's
latitude and longitude and a device ID. Before anything is shared, published
or committed:
- share JPG or PNG rebuilt from pixels, with no EXIF, XMP or ICC data;
- never put FITS or TIFF files, logs or `darkroom.toml` (it holds local
  paths) into a repository or a public post.

## Reporting to the user

Lead with the result and how it compares with what they had. State how many
frames went in and the total integration time. Say plainly what was not
fixed (a bright core, a noisy edge) and what is cosmetic. Show the image.
