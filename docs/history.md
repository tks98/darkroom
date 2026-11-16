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
