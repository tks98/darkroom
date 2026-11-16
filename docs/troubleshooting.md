# Troubleshooting

Start with `darkroom doctor`, which checks most of what follows and prints a fix. When a stage fails, read its log in `<work>/logs/` before changing anything.

## GraXpert's GPU denoise fails on macOS

GraXpert 3.1.0rc2 cannot run its denoise model on the Apple GPU under macOS 27: CoreML fails to compile the model. Forcing it gets no further, and GraXpert's bundled onnxruntime is slower on the GPU than on the CPU anyway, because it splits the network into over a hundred pieces that move back and forth.

darkroom works around this. `setup.sh` (or the first denoise) makes a copy of the same model with a fixed batch size, and darkroom runs it with onnxruntime 1.32, which handles the whole network on the GPU: 23 seconds for M31 instead of about 5 minutes. `doctor` checks that this really runs on the GPU. If it falls back to the CPU, the stage still works, just slower. `--cpu` forces the CPU.

## Plate solving fails because the catalogue server is down

Siril downloads star lists for plate solving from VizieR. VizieR was unreachable on every day darkroom was developed, and plate solving fails without it.

The `color` stage notices that failure in Siril's log, fetches the Gaia DR3 star list for the field from ESA's Gaia archive, puts it in Siril's download cache, and tries again. It adds about a minute, mostly Siril's own timeout, and says in its output that it used the fallback. The request contains only sky coordinates. `doctor` shows VizieR being down as a `WARN`; there is nothing to do about it. To turn the fallback off, set `[color] catalog_fallback = false` or pass `--no-catalog-fallback`.

If both servers are down, wait. Colour calibration is worth more than the time it saves to skip it.

## The master dark does not match the subs

The Seestar's sensor is not cooled, so its temperature follows the night. During the M31 session it fell from 21.4 °C to 8.6 °C (median 10.9 °C), while the only master dark had been taken at 21.5 °C, before the session started. darkroom warns when the dark is more than 5 °C from the subs.

For that data it mattered little. A stack without the dark was 2% noisier in the background and kept some isolated hot pixels. On M13, the dark (6.8 °C off) still gave about 4% less noise than no dark at all. When `init` warns, also make a `dark = "none"` stack and compare. Darks taken at the end of a session, when the sensor has cooled, would match better.

## StarNet is blocked by Gatekeeper

The StarNet command-line version for macOS is an unsigned Intel binary that runs under Rosetta. macOS Gatekeeper blocks it until you choose to allow it, and that is your decision to make.

Clearing the quarantine flag on the StarNet folder is what allows it to run:

```sh
xattr -dr com.apple.quarantine path/to/StarNetv2CLI_MacOS
```

`doctor` reports the flag and never clears it, and it never runs a quarantined StarNet (that would pop up the macOS warning). Until StarNet is allowed, the `cluster` look and `darkroom star-reduce` work without it.

## `siril-cli` cannot find the script

`siril-cli` starts in `~/Pictures`, so a script passed by a relative path is not found, and so are relative paths inside it. darkroom passes everything as absolute paths. This only matters if you run the scripts in `siril/` by hand.

## Siril settings changed after a run

The Siril scripts change two settings that Siril may keep: `stack` switches processed images to 16-bit (`set16bits`), and the plate solve in `color` saves the focal length and pixel size it was given as Siril's defaults. Reset them in Siril's preferences if you use Siril for other work.

## The shell splits paths with spaces

The Seestar names its folders with spaces (`M 31_sub`). Quote paths in the shell. In zsh, an unquoted variable holding several arguments is passed as one argument, so write the arguments out.
