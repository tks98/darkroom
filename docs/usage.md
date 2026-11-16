# Using darkroom

## A new target

Describe the target once, then run it:

```sh
darkroom init --work m31 --subs path/to/lights --dark-library path/to/darklibrary
darkroom run  --work m31
```

`init` reads the FITS headers of the subs and writes `m31/darkroom.toml`. It detects the camera, picks a master dark, and prints a summary you should read: frame count, total integration time, the sensor temperature range over the session, and any warning. For the M31 data it looked like this:

```
  target        M 31
  subs          404 frames matching Light_*.fit
  exposure      30 s each, 3 h 22 min in total
  gain          200
  filter        IRCUT -> SPCC filter "UV/IR Block"
  sensor temp   median 10.9 C (range 8.6 to 21.4)
  optics        focal length 160 mm, pixel 2.9 um, Bayer GRBG
  camera        seestar-s30-pro (detected from the FITS header)
  look          galaxy
```

Use `--dark FILE` for one particular master dark, or `--no-dark` to stack without one. Use `--look cluster` for a target made of stars (see [Looks](configuration.md#looks)).

The config file lists every setting. The ones `init` did not set are commented out and show the value in effect and where it comes from, so it doubles as a record of how the image was made. Before the first run, set `[starnet] dir` (or `DARKROOM_STARNET_DIR`) and, if you want one, `[finish] crop`.

## Re-running

`run` goes through the stages in order and skips any stage whose output is up to date. A stage counts as up to date when its outputs exist, are newer than its inputs, and were made with the settings now in force. So after you change a setting, run it again and only the affected stages are redone: a change to `[finish] gamma` takes about a second, while a change to `[stack]` redoes everything.

| Flag | Effect |
|---|---|
| `--to STAGE` | stop after this stage |
| `--from STAGE` | redo this stage and everything after it, up to date or not |
| `--force` | redo everything |

The stages are `stack`, `bge`, `color`, `denoise`, `stretch`, `starnet` and `finish`. [How it works](pipeline.md) describes each one.

## Running one stage

Every stage is also a command of its own. Flags override the config file, which is handy for trying something without editing it:

```sh
darkroom finish --work m31 --gamma 1.4 --out try_g14    # writes try_g14.tif/.jpg, leaves final.* alone
darkroom stack  --work m31 --reuse-registration --framing cog --out stack_cog
```

`darkroom <command> --help` lists every flag. `examples/m31.sh` is the whole M31 run written out as single commands.

## Look at every stage

The first time you process a new target, go one stage at a time (`--to stack`, then `--to bge`, and so on) and look at each result before moving on. Most problems are visible right away and are cheaper to fix at the stage that caused them:

- After `stack`, check how many frames were kept (`logs/stack.log`) and that stars are round into the corners.
- After `bge`, the sky should be even, and `bge_background.fits` should be a smooth gradient with no trace of the target.
- After `color`, read the SPCC lines in `logs/color.log`. Siril says how many stars it used and warns when the fit is imprecise.
- After `starnet`, open `starless.tif` and look for blotches where bright stars were, or for detail that went with the stars.
- After `finish`, look at the core, the corners and the edge of the target at full size. The sky should be neutral, nothing should be clipped, and faint stars should still be there.

## Comparing variants

When you tune how the image looks, render two or three versions and put them side by side before choosing. Judging one change on its own is how you end up with a core that has gone green. `finish` takes about a second, so variants are cheap:

```sh
darkroom finish --work m31 --gamma 1.0 --out try_g10
darkroom finish --work m31 --gamma 2.4 --out try_g24
```

[Configuration](configuration.md#finish) shows what the main `finish` settings do and what they look like pushed too far.

## Disk space and cleanup

The `stack` stage writes calibrated and registered frames to `<work>/process`: about 80 MB per sub, so 31 GB for the 404 M31 subs. They are only needed to restack. Delete the folder when you are happy with the stack, or set `[stack] clean = true` to have it removed after every successful stack.

`stack` refuses to run when `process/` already holds frames from an earlier run, so two runs never get mixed. `--force` clears it first, and `--reuse-registration` re-frames and re-stacks the frames that are already there.

Keep the linear files (`stack.fit` through `denoised.fits`) and `darkroom.toml`. Reprocessing from them is fast.

## Sharing results

FITS and TIFF headers from the Seestar contain the observing site's latitude and longitude and the telescope's device ID. Share JPG or PNG files rebuilt from the pixels, and keep FITS, TIFF, logs and `darkroom.toml` (it holds your local paths) out of public places.
