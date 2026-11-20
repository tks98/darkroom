# History

darkroom started as one-off scripts for a single night of M31 data from a Seestar S30 Pro: 404 subs of 30 seconds, taken on 2026-11-15. It was turned into a package in five phases.

## Phase 1: the port

The scripts became a package with one command per stage. Run end to end on the 404 subs, every stage's pixel data was identical to the hand-run scripts' output, and the final TIFF and JPEG were byte for byte the same.

One behaviour changed to get there. The original StarNet input had been made by reloading the stretched FITS file before saving the TIFF. Saving the TIFF straight from memory gives slightly different 16-bit values (up to 11 in 65,535), because Siril assigns a colour profile on reload. The stretch script now reloads too.

## Phase 2: `doctor` and `setup.sh`

Most of the first night's time went to environment problems rather than image processing: GraXpert's GPU denoise failing, VizieR down, `siril-cli` paths, StarNet blocked by Gatekeeper. `darkroom doctor` checks for all of them, and `setup.sh` sets up a machine in one step.

## Phase 3: any target

`darkroom.toml`, `darkroom init`, camera and look presets, automatic dark selection, the catalogue fallback, stack framing, cropping by shape, and skipping stages that are up to date.

The M31 run was repeated from a config written by `init`, with automatic dark selection among decoy darks and `crop = "2:3"`. Again every stage matched the original pixel for pixel and the final files were byte-identical. The catalogue fallback was tested against the real outage: VizieR was down, the plate solve failed, the fallback fetched the list from ESA, and the retry succeeded.

## Phase 4: the agent skill

`skill/SKILL.md`, first written for Claude Code and later made agent-neutral, with an `AGENTS.md` for agents that read that instead; see [using darkroom with an AI agent](agents.md).

## Phase 5: a second target

M13, a globular cluster: 18 subs of 30 seconds from 2026-08-27, cut short by cloud, with 15 stacked (7.5 minutes). Stacking, background extraction, colour calibration and denoise all worked with the M31 settings, and the catalogue fallback worked unprompted on the new field. The finish did not:

- StarNet removed the whole cluster, since it is made of stars, and the star shrink then dimmed what was left. This led to `[finish] stars = "keep"` and the `cluster` look.
- The cluster is small in the frame, so the crop needed `crop_scale`.
- Sky neutralisation added a blue cast. It measured the sky colour from the darker half of the sky pixels, which is biased toward the noisiest channel. On M13 it read blue as 0.022 low when the sky was neutral to within 0.002. It now uses every sky pixel.

The neutralisation fix also changes M31's final image slightly, by at most 0.0014 (a third of one 8-bit level); every stage before `finish` is unchanged. The M31 images in the README were made before the fix.

Phase 5 also added the `keep_bkg` and `keep_stars` frame filters. They did not help on M13: see [frame selection](configuration.md#frame-selection).

## After phase 5: StarNet2 2.6 and Docker

StarNet2 2.6.2 came out with a native, signed Apple Silicon build, a new command line and a new licence. Its licence lets other programs run the StarNet command line, as long as the user downloads it from the official site and accepts the terms themselves, and it forbids redistributing it. That shaped two additions: `darkroom get-starnet`, which downloads StarNet2, shows its licence and installs it only after the user accepts, and a Docker image that contains Siril and GraXpert but leaves StarNet to `get-starnet`.

On M31, StarNet2 2.6.2 gave the same starless image as StarNet v2.0.2 to within 0.01 of an 8-bit level, in about 10 seconds instead of 50 to 75.

An open-source alternative, starrem2k13 (MIT), was also tried on the same image and rejected: it left a ring where nearly every star had been, coloured halos, and a visible seam where its tiles meet.

The full M31 run was repeated in the Docker image under emulation on Apple Silicon. Siril's colour calibration crashed there (see [Docker](docker.md#colour-calibration-crashes-under-emulation)), so that stage ran natively; every other stage ran in the container. The result matched the native one to 0.04 of an 8-bit level on average. Two things turned up along the way: Siril on Linux will not `cd` into a read-only folder, so `stack` now links the subs into the work folder when needed, and Siril's AppImage only finds its Python module when `APPDIR` is set.

After that, the image's role was narrowed. Running the Mac pipeline inside Docker is slow and loses the GPU, so the image became the way darkroom is delivered on Linux and Windows, and a native installer, `darkroom setup`, took over on the Mac: it downloads the official Siril and GraXpert builds, checks them against pinned hashes, and marks them for Gatekeeper the way a browser would. Siril's Mac build is notarized; GraXpert's is not, so `setup` asks before installing it and leaves the decision to allow it with the user.

## Mosaics and the Seestar S30

A Rosette Nebula mosaic from the original Seestar S30 (151 frames of 60 seconds at 86 pointings, about 1.5 × 2.5 degrees) showed that registering against one reference frame does not work for mosaics: 58 frames were dropped, and `framing = max` left rectangles of different depth. `[stack] mosaic` plate-solves every frame instead and stacks onto one canvas; see [mosaics](usage.md#mosaics).

Three things had to be found out by hand first. Registered frames must be 32-bit: in 16-bit, Siril's overlap normalisation logged thousands of overflow errors and the mosaic came out as a patchwork of differently coloured tiles. Siril's catalogue server was timing out, and one download per frame would have meant minutes of timeouts, so darkroom fetches the stars for the whole field in one query and writes them where Siril looks; Siril then solved without network access. And `seqplatesolve` drops the frames it cannot solve from the selection, so a second solve after the catalogue fallback has to select them again.

On the Rosette, 139 of 151 frames were solved and 127 stacked, with no visible seams. A 215-frame Alnilam mosaic gave 185 solved and 173 stacked. Colour calibration on the Rosette mosaic used 1615 stars but called its fit imprecise. Ordinary stacks are unchanged.

On Linux, darkroom first wrote the star lists to `~/.cache/siril/download_cache`, a guess. Siril 1.4.5 keeps its download cache in its config folder instead, `~/.config/siril/download_cache` (`$XDG_CONFIG_HOME` when set), as a plate solve in the Docker image showed in its "Cannot create catalogue file" line; the fallback would still have worked, one query and one timeout per pointing. With the path fixed, the Rosette stack in the Docker image (emulated on Apple Silicon) read all 151 catalogues from the cache with no download, solved 139 of 151 frames in 17 seconds (3 seconds natively), stacked 127, and cropped to 2415×3904, one pixel smaller each way than natively.

The same work added a camera preset for the original S30 (`seestar-s30`: 150 mm, its own SPCC sensor entry). Camera detection now prefers an exact match of the header value and otherwise the longest matching text, so the S30 preset does not claim S30 Pro subs.

## Core compression

Another model's review of the M31 result said the core was close to a flat white blob. It was: 7% of the centre 60 pixels across was at 98% brightness or more. `[finish] core` compresses the brightest part of the target before the stars go back in, and the `galaxy` look sets it to 0.3. This deliberately changes the M31 result: about 2% of the pixels move by more than 1/255, all in the core of M31 and of M110, and the centre is no longer clipped. With `core = 0` the output matches the previous result to within 1/255. Work directories finished before this setting existed rerun `finish` once, because `core` is recorded whenever it is on.

The same review suggested pulling the outer disk toward blue and denoising harder. Neither was adopted. The disk colour is what SPCC measured, and `yb` already exaggerates it. Denoise at strength 0.8 instead of 0.6 cut pixel-level grain in the outer disk by 14% but did not reduce the blotchy mottling there (it rose slightly), which comes from too little integration time rather than from noise a denoiser removes.

## Optional deconvolution

A `deconv` stage between `denoise` and `stretch` runs GraXpert's object deconvolution AI model (1.0.1) through onnxruntime, ported from GraXpert 3.1's `deconvolution.py` the same way the denoise stage is. It is off by default (`[deconv] strength = 0`): the stage is then skipped, `stretch` reads `denoised.fits` as before, and existing work directories stay up to date. On M31 with it off, the stretch was skipped as up to date, and a forced rerun gave a `stretched.fit` and `stretched.tif` identical to the earlier ones.

The model is GraXpert's, under CC BY-NC-SA 4.0, so it is not in the repository; GraXpert downloads it from its own server the first time the stage runs. `psf = "auto"` measures the star size from the denoised image: on M31 it gave a median FWHM of 2.50 pixels from 252 stars, against 2.59 from Siril's `findstar` on the green channel. An earlier hand-run trial had assumed 3.2.

With strength 0.5 and a PSF of 3.2 pixels, the stage on the Apple GPU matched the same code on the CPU to 2e-6, and a hand-run prototype that used CoreML's default settings to at most 0.012 and 7e-7 on average. That trial made the dust lanes near the core slightly crisper and the stars slightly tighter. The stage takes about 20 to 25 seconds on M31.

## Turned mosaic crops

The Alnilam mosaic's pointings formed a diagonal band, and the largest upright rectangle with data everywhere, 3737×2373, kept 52% of the pixels with data (39% of the 5017×4582 canvas). Three ways to keep more were tried on that stack:

- Turning the image before cropping. The best angle was 31.75° and the crop 4140×2814: 69% of the pixels with data, with all three belt stars, the Horsehead and the Flame Nebula inside. The search over angles and the resampling took 11 seconds. Turning resamples the image once more (cubic spline): stars came out about 2% wider and the noise was unchanged; bilinear resampling would have smoothed the noise by 13% and left a pattern in it. The plate solution is turned with the image; stars found in the result were within 0.04 pixels of where the canvas's solution put them.
- Keeping the bounding box of the data (here the whole canvas, 26% of it empty) and filling the empty parts with a smooth background and matching noise. GraXpert's background extraction coped with it. But the finished picture still has to be a rectangle with data everywhere, so the fill only moves the crop to the end, and the filled areas would have to be tracked through every stage.
- Doing nothing (`none`), for cropping by hand.

Turning is now the default (`[stack] mosaic_crop = "rotate"`). It turns the image only when that keeps at least 10% more pixels than an upright crop, so the Rosette mosaic, which is nearly upright, is cropped exactly as before (2416×3905, 96% of the pixels with data) and ordinary stacks are not affected. `mosaic_crop = "rect"` keeps the old upright crop and north up. Work directories stacked before this setting existed are not re-cropped.

On the turned Alnilam crop, GraXpert's background extraction gave a smooth model (3.5% from darkest to brightest) and a sky as even as on the upright crop: block medians within 0.5 to 1.3 times the noise per channel, against 0.5 to 1.2. Siril accepted the turned plate solution for colour calibration ("already plate solved"). SPCC itself was not checked: its spectral catalogue server was down that day, and Siril crashed when the download failed, on the upright crop too.

## Colour through the LP filter

The Seestar's LP filter passes two narrow bands, hydrogen-alpha and oxygen III. Siril's SPCC can model it two ways: with the filter's measured transmission curve ("ZWO Seestar LP", what the camera presets use) or in narrowband mode, given each band's wavelength and width (656.3 nm and 20 nm for red; 500.7 nm and 30 nm for green and blue). On the Rosette mosaic the two gave colour factors of 0.896 / 1 / 0.914 and 0.896 / 1 / 0.911, a 0.3% difference in blue, and identical-looking images; both fits used 1,241 stars, and both drew Siril's "imprecise solution" warning. darkroom keeps the filter curve. The comparison on the Orion-belt mosaic and with a G2V white reference did not finish: Zenodo, which serves SPCC's spectra, was down for several hours and then rate-limited the runs, and Siril 1.4.5 crashes when that happens. The `color` stage now recognises that crash and explains it instead of showing Siril's log tail.


## Bias, flats and raw darks

Until here darkroom calibrated with a master dark only, which is what a Seestar provides. A cooled astronomy camera comes with folders of raw darks, bias frames and flats instead, so `[target] dark`, `bias` and `flats` now each take a master file or a folder, and folders are stacked into masters in `<work>/masters` (Siril's usual recipe: bias and darks averaged without normalisation, flats calibrated with the bias and averaged with multiplicative normalisation). A master is rebuilt only when its frames change, and a rebuilt master restacks the lights. SPCC's narrowband mode became a setting, `[color] narrowband`, for dual-band filters that Siril's filter database does not list. M31 and the other Seestar results are unchanged: without bias or flats the stack script and the stack's skip record are what they were.

It was tested on two targets from a ZWO ASI294MC Pro (Sony IMX294, cooled to 0 °C, gain 120) behind a dual-band Hα/OIII filter at 299 mm, with 18 darks of 300 s, 50 bias frames and 20 flats of 70 ms; no flat darks, since 70 ms flats are close enough to bias.

- NGC 2264, 55 lights of 300 s. The three masters took 8 seconds; the stack registered all 55 frames and stacked 45 in 30 seconds. The master flat shows the vignetting (corners 0.93 to 1.02 of the median, the brightest point off-centre) and a handful of dust donuts, none deeper than about 0.5%. Stacked again without flats, the ratio of the two stacks shows what the flats removed: the corners 11% darker than the centre, 22% from the brightest to the darkest part. The darkest tenth of the sky in the corners rose from 0.64 to 0.70 of the centre's; the rest of that difference is the nebula, which fills the middle of the field. The donuts are too shallow to see in either stack.
- With `narrowband = "656.28/12,500.7/12,500.7/12"` (Hα on red, OIII on green and blue; this filter's bandwidths are not known), SPCC fitted 1,376 stars with factors 0.808 / 0.788 / 1, with Siril's "imprecise solution" warning, as through the Seestar's LP filter. The stretched image has red hydrogen, dark sky and blue-green around the cluster.
- NGC 292, the Small Magellanic Cloud, 96 lights of 300 s with the same darks and bias and flats taken 21 days later; darkroom warns about the gap. All 96 registered and 78 were stacked in 54 seconds. The flats removed a similar falloff (corners 9% below the centre) and nothing looked over-corrected, but with donuts this shallow the test says little about dust from another session.

The ASI294MC Pro got a camera preset, `zwo-asi294mc-pro`, detected from the `INSTRUME` keyword.

## SPCC without Zenodo

SPCC reads its star spectra (Gaia DR3 XP sampled) from Siril's servers, Zenodo first, with HTTP range requests on every run. When Zenodo answered 5xx or 429, Siril 1.4.5 crashed instead of trying its other servers, and putting another server first in `spcc_mirrors.json` did not help. Siril can also read the same catalogue from disk (`-catalog=localgaia`, with the folder in its `core.catalogue_gaia_photo` setting), but the full catalogue is 48 files of up to several GB each.

Reading Siril's reader (`src/io/healpix/healpix.cpp` in 1.4.5) showed that it never needs a whole file. A file is a 128-byte header, an index with one cumulative star count per level-8 HEALPix pixel, and the stars, sorted by pixel; a cone search reads the index entries around the pixels it touches and then those stars, and only checks that the file is long enough for them. So the `color` stage now builds such files with only the pixels a field needs: the real header, an index in which every other pixel has no stars, and the real records of the pixels fetched. It fetches them itself, in Python, from the same servers, with retries and backoff, moving on to the next server when one fails, and checks that every star it gets lies in the pixel it asked for. Siril runs on a throwaway copy of its settings (`siril-cli -i`), because siril-cli saves its settings when a script ends and a `set` would otherwise stay in the user's configuration.

On M31 (the 404-sub stack, `bge.fits` unchanged) the field from the header's pointing, with a margin for the solve, was 597 pixels in one file. The fetch took 23 seconds and 18 MB from Zenodo, and the file is 15 MB. Then the stage ran with every connection from Siril sent to a dead proxy, which is enough to make an online SPCC fail (Siril crashed with "Couldn't connect to server"). On the local copy SPCC read 4,333 stars and fitted 1,891, with factors 0.717 / 1.000 / 0.942 and the same linear fits as the online runs before, and `spcc.fit` was identical, pixel for pixel, to the one from an online run. A second run fetched nothing. Siril's own settings file was unchanged.

## Fixes to the stack, the records and the maths on empty images

A review of the pipeline found five defects, now fixed. None of them moves the output of a normal run: on synthetic images and on the stretched NGC 2264 image, `finish` (with and without star removal, and with an aspect crop), `denoise` and `deconv` give results identical, bit for bit, to the code before.

- Siril saved its settings at the end of every script, so `set16bits`, `set32bits` and `setext` changed the user's Siril preferences (a scratch config run of `set32bits` and `setext fits` came back with `force_16bit=false` and `extension=.fits`). Every script now runs on a throwaway copy (`siril-cli -i`), as the local SPCC run already did. A stack, an online SPCC run and a script with `set32bits` and `setext fits` left the user's `config.1.4.ini` unchanged, byte for byte. Siril still found the SPCC database (`spcc_list` and the online SPCC run worked), and its source shows that it finds the download cache and `spcc_mirrors.json` from its config folder, not from the `-i` file.
- The stack ran Siril's `link` in the subs folder, so it stacked every `.fit` there, whatever the camera's `sub_glob` said, and a folder of `.fits` subs failed. darkroom now links exactly the selected subs (those matching `sub_glob`, else every FITS file) into `<work>/process/subs`, and `.fit`, `.fits` and `.fts` in any case count as FITS. Siril's `link` takes all three with `setext fit`: five subs named `.fits`, `.FTS`, `.fit` and `.Fits` were linked and stacked.
- The stack's record held only the settings, and the skip check compared the dates of the subs that were there, so deleting a sub, or adding one dated before the last stack, left the stack "up to date". The record now has the number of selected subs and a digest of their names, and, for a mosaic, the focal length and pixel size of the plate solve. An older record without them stays up to date until the next real stack.
- Records held the darkroom version, but nothing compared it. Each stage now has an algorithm version that the skip check compares, and the records list the versions of Siril, GraXpert, onnxruntime and StarNet used, for reference.
- On images with large areas of one level, the maths divided by zero. `deconv` normalises each 512-pixel tile by its spread, which is 0 on an empty tile: a 1200 × 1200 image that was empty apart from a 200-pixel square came out 86% NaN. Such tiles are now left as they are. `denoise` divides by each channel's MAD, which is 0 when more than half the channel is one level; it now uses a tiny spread there, which leaves that channel practically as it was. In `finish`, an image of one level made the sky noise NaN and the whole result NaN, an empty target mask made the yellow/blue step NaN, and a nebula filling the frame left no sky to neutralise; these now log what they skip or fall back to. All three stages refuse to write NaN or infinite values.
