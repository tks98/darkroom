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
