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

## Mosaics

The Seestar's mosaic mode moves the mount between frames: a couple of frames at each pointing, dozens of pointings, covering a field bigger than the sensor. Normal registration matches every frame against one reference frame, so frames that do not overlap it are dropped. On a 151-frame Rosette mosaic that lost 58 frames, and `framing = max` left visible rectangles of different depth.

`init` notices a mosaic from the pointings in the FITS headers (when they spread over more than a quarter of the field of view) and writes `[stack] mosaic = true`. `stack` warns when a mosaic is stacked without it. Use `--mosaic` or `--no-mosaic` to override it for one run. In mosaic mode `stack`:

1. calibrates the frames as usual;
2. fetches the Gaia stars for the whole field in one query from the ESA archive and splits them into the per-frame catalogue files Siril's plate solver reads, so solving needs no further downloads. Siril keeps these files in `~/Library/Application Support/org.siril.Siril/siril/download_cache` on a Mac and in `~/.config/siril/download_cache` on Linux (`$XDG_CONFIG_HOME/siril/download_cache` when that is set; in the Docker image, the cache volume). If Siril still asks for a file darkroom did not predict, the [catalogue fallback](troubleshooting.md#plate-solving-fails-because-the-catalogue-server-is-down) fetches it and the frames that missed it are solved again;
3. plate-solves every frame (using `[color] focal` and `pixel`); the solutions take the place of registration;
4. keeps the best frames by sharpness and roundness, as usual, and stacks them onto one canvas that holds every frame, normalising each frame on the areas where frames overlap and feathering their edges, in 32-bit;
5. crops the canvas to the largest rectangle that has data in every pixel. The plate solution is kept and moved with the crop. The uncropped canvas is kept as `stack_full.fit`.

`stack` prints how many frames were solved and stacked, and the crop. On the Rosette mosaic, 139 of 151 frames were solved (most of the others had too few stars), 127 were stacked, and the 2452×4140 canvas was cropped to 2416×3905. On a 215-frame mosaic around Alnilam, whose pointings formed a diagonal band, 185 were solved, 173 stacked, and the crop kept 3737×2373 of a 5017×4582 canvas. When the crop throws away too much, start from `stack_full.fit` and crop it yourself.

`--reuse-registration` restacks an already solved mosaic, for example with other `keep_*` settings. A mosaic needs more disk space than a normal stack, since the registered frames are 32-bit and as big as the whole canvas.

## Disk space and cleanup

The `stack` stage writes calibrated and registered frames to `<work>/process`: about 80 MB per sub, so 31 GB for the 404 M31 subs. They are only needed to restack. Delete the folder when you are happy with the stack, or set `[stack] clean = true` to have it removed after every successful stack.

`stack` refuses to run when `process/` already holds frames from an earlier run, so two runs never get mixed. `--force` clears it first, and `--reuse-registration` re-frames and re-stacks the frames that are already there.

Keep the linear files (`stack.fit` through `denoised.fits`) and `darkroom.toml`. Reprocessing from them is fast.

## Sharing results

FITS and TIFF headers from the Seestar contain the observing site's latitude and longitude and the telescope's device ID. Share JPG or PNG files rebuilt from the pixels, and keep FITS, TIFF, logs and `darkroom.toml` (it holds your local paths) out of public places.
