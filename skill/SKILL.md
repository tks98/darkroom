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
| `starnet` | StarNet v2 star removal | `starless.tif` |
| `finish` | Python: enhance, shrink stars, recombine, crop | `final.tif/.jpg` |

The numbers are the tool's job. Your job is the part it cannot do: looking at
the images and deciding whether each step actually improved them.

## Before you start

1. Find the repo: this skill's folder is `<repo>/skill` (often reached through
   a symlink in `~/.claude/skills/`, so resolve it). Use `<repo>/.venv/bin/darkroom`.
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

**After `stack`**
- How many frames were registered and kept? (`logs/stack.log`.) Losing more
  than about 20% deserves a look at why: cloud, wind, trailing.
- Quick-stretch the stack and view it at full frame and at 1:1 in the centre
  and one corner. Stars should be round to the corners.
- If the camera made its own stack of the same session, put the two side by
  side. darkroom should be at least as good; if it is not, stop and find out
  why before going on.
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

**After `bge`**
- The background should be even. Measure the sky in a coarse grid away from
  the target; the blocks should agree within a few percent.
- Look at `bge_background.fits`: it should be a smooth gradient. If it shows
  the shape of the target, the model has eaten real signal; raise
  `[bge] smoothing`.

**After `color`**
- Read the SPCC lines in `logs/color.log`: how many stars were used, and
  whether Siril warned that the fit was imprecise. Report the warning; do not
  hide it.
- The plate-solve result (pixel scale, field centre) should match the camera.

**After `denoise`**
- Compare a 1:1 crop before and after. Noise should drop by roughly half at
  the default strength without the faint structure turning to plastic. Lower
  `[denoise] strength` if detail is being smoothed away.

**After `stretch`**
- The sky should be dark grey, not black, and the brightest core should not
  be pure white. A flat, washed-out target means the arcsinh factor is too
  high; a blown core means the autostretch is too aggressive.

**After `starnet`**
- View `starless.tif`. Look for blotches or holes where bright stars were, and
  for real detail (star clouds, small galaxies) that went with the stars.

**After `finish`**
Check, at 1:1, the core, each corner, any companion objects, and an outer
part of the target:
- Background neutral and even (compare R, G and B of the sky).
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
| `erode` | morphological star shrink | any value deletes the faint 1–2 px stars; leave at 0 |
| `mid` | <0.5 brightens the starless layer | core blows out, background lifts |
| `sat` | overall saturation of the starless layer | noise turns into colour blotches |
| `yb` | stretches yellow/blue differences inside the target only | core goes green-yellow, arms purple (seen at 2.6 on M31) |
| `sharpen` | masked unsharp mask on the target | halos along dust lanes, grain |
| `crop` | `"W:H"` (largest box of that shape centred on the brightest part of the target), or `"x,y,w,h"` | target touching the edge; use `crop_center` if the centre is wrong |

`yb` is cosmetic. It makes regions easier to tell apart; it is not measured
colour. Say so when you describe the result.

A new kind of target (a nebula, a cluster) will not suit the galaxy look
unchanged. Expect to tune `stretch` and `finish` from scratch and tell the
user that the defaults were made for one galaxy.

## Known failures

- **GraXpert's own GPU denoise fails on recent macOS.** darkroom does not use
  it; `denoise` runs the same model through onnxruntime. If `doctor` says the
  GPU check fell back to CPU, the stage still works, about 15× slower.
- **Plate solve fails with a catalogue error.** Siril's catalogue server
  (VizieR) is sometimes unreachable; `doctor` shows it as a WARN. `color`
  then fetches Gaia DR3 from ESA's archive by itself and retries (about a
  minute longer, mostly Siril's timeout) and says so in its output. If both
  servers are down, wait: do not skip colour calibration.
- **`siril-cli` starts in `~/Pictures`.** darkroom passes absolute paths. If
  you write a Siril script by hand, do the same.
- **StarNet is blocked by Gatekeeper.** The macOS StarNet CLI is an unsigned
  binary. `doctor` reports the quarantine flag and the command that clears it
  (`xattr -dr com.apple.quarantine <folder>`). That is a security decision:
  explain it and let the user run the command. Do not run it yourself.
  Until then, `darkroom star-reduce` is a weaker fallback that needs no
  StarNet.
- **Shell is often zsh.** Unquoted variables are not split into words; quote
  paths (the Seestar's folders contain spaces) and pass arguments explicitly.

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
