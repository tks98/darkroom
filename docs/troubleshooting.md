# Troubleshooting

Start with `darkroom doctor`, which checks most of what follows and prints a fix. When a stage fails, read its log in `<work>/logs/` before changing anything.

## GraXpert's GPU denoise fails on macOS

GraXpert 3.1.0rc2 cannot run its denoise model on the Apple GPU under macOS 27: CoreML fails to compile the model. Forcing it gets no further, and GraXpert's bundled onnxruntime is slower on the GPU than on the CPU anyway, because it splits the network into over a hundred pieces that move back and forth.

darkroom works around this. `setup.sh` (or the first denoise) makes a copy of the same model with a fixed batch size, and darkroom runs it with onnxruntime 1.32, which handles the whole network on the GPU: 23 seconds for M31 instead of about 5 minutes. `doctor` checks that this really runs on the GPU. If it falls back to the CPU, the stage still works, just slower. `--cpu` forces the CPU.

## Plate solving fails because the catalogue server is down

Siril downloads star lists for plate solving from VizieR. VizieR was unreachable on every day darkroom was developed, and plate solving fails without it.

The `color` stage notices that failure in Siril's log, fetches the Gaia DR3 star list for the field from ESA's Gaia archive, puts it in Siril's download cache, and tries again. It adds about a minute, mostly Siril's own timeout, and says in its output that it used the fallback. The request contains only sky coordinates. `doctor` shows VizieR being down as a `WARN`; there is nothing to do about it. To turn the fallback off, set `[color] catalog_fallback = false` or pass `--no-catalog-fallback`.

If both servers are down, wait. Colour calibration is worth more than the time it saves to skip it.

## Colour calibration stops with "Siril could not download the star spectra"

SPCC compares the stars in the image with Gaia DR3 spectra, which Siril downloads from its own servers, Zenodo first. When Zenodo answers with an error, Siril 1.4.5 logs "Query failed on mirror ..., trying alternatives..." and then crashes instead of trying the next server, so macOS may also show a crash report for `siril-cli`. It crashes the same way when it cannot connect at all.

darkroom avoids this by keeping its own copy of the spectra for the fields it processes ([`spcc_local`](configuration.md#spccs-star-spectra-spcc_local)): it fetches them itself, retrying and moving on to the next server when one fails, and runs SPCC on the copy. So this error now only appears when darkroom's fetch failed on every server too (its output then says "could not download the SPCC star spectra" and lists each server's error), or with `spcc_local = false`. Once a field has been fetched, its colour calibration needs no network. To fetch a field ahead of time, while online, run `darkroom catalog fetch --work DIR`; `darkroom doctor --work DIR` says whether the copy covers the field.

When Siril does download the spectra itself, the `color` stage recognises the crash and says which error the server gave:

- HTTP 500, 502 or 504: Zenodo is having problems. On 2026-11-18 it returned these for several hours.
- HTTP 429: Zenodo is rate-limiting your machine, usually after several colour calibrations in a row. Wait a few minutes.

Nothing is wrong with the image. Run the stage again later. darkroom does not skip colour calibration, and the catalogue fallback for plate solving does not cover this, because the spectra are not available from the ESA archive in the form Siril reads.

## The master dark does not match the subs

The Seestar's sensor is not cooled, so its temperature follows the night. During the M31 session it fell from 21.4 °C to 8.6 °C (median 10.9 °C), while the only master dark had been taken at 21.5 °C, before the session started. darkroom warns when the dark is more than 5 °C from the subs.

For that data it mattered little. A stack without the dark was 2% noisier in the background and kept some isolated hot pixels. On M13, the dark (6.8 °C off) still gave about 4% less noise than no dark at all. When `init` warns, also make a `dark = "none"` stack and compare. Darks taken at the end of a session, when the sensor has cooled, would match better.

## StarNet v2.0.2 is blocked by Gatekeeper

This applies only to the old StarNet v2.0.2. StarNet2 2.6, which `darkroom get-starnet` installs, is signed and runs natively on Apple Silicon.

The v2.0.2 command-line version for macOS is an unsigned Intel binary that runs under Rosetta. macOS Gatekeeper blocks it until you choose to allow it, and that is your decision to make.

Clearing the quarantine flag on the StarNet folder is what allows it to run:

```sh
xattr -dr com.apple.quarantine path/to/StarNetv2CLI_MacOS
```

`doctor` reports the flag and never clears it, and it never runs a quarantined StarNet (that would pop up the macOS warning). Until StarNet is allowed, the `cluster` look and `darkroom star-reduce` work without it, or install StarNet2 with `darkroom get-starnet`.

## The subs folder is read-only

Siril refuses to `cd` into a folder it cannot write to, even though darkroom never writes there. When the subs folder is read-only (as in the Docker image, where it is mounted read-only), `stack` makes `<work>/process/subs/` with a link to each sub and points Siril at that instead. Nothing needs doing; this is here in case you see that folder.

## "Python quit unexpectedly" every time Siril runs

On macOS 27 with Siril 1.4.5, every `siril-cli` run can pop up a "Python quit unexpectedly" dialog, so a darkroom run brings a burst of them. The crash report names Siril's bundled Python (`Siril.app/Contents/Frameworks/Python.framework/.../Python`) and says `Code Signature Invalid` and `Launch Constraint Violation`.

When it starts, Siril runs the Python in its own virtual environment to check it, and that Python is a link to the copy inside Siril.app, which macOS refuses to run. It is a Siril bug ([free-astro/siril#1957](https://gitlab.com/free-astro/siril/-/issues/1957)). darkroom's results are not affected: Siril's stacking, plate solving and colour calibration do not use that Python, and every stage finishes normally. Siril's own Python scripts cannot run while the bug is there.

The workaround is to point Siril's virtual environment at another Python 3.12, for example Homebrew's (`brew install python@3.12`). Back the environment up first:

```sh
V="$HOME/Library/Application Support/org.siril.Siril/siril/venv"
cp -Rp "$V" "$V.backup"
ln -sfn /opt/homebrew/opt/python@3.12/bin/python3.12 "$V/bin/python3.12"
sed -i '' -e 's|^home = .*|home = /opt/homebrew/opt/python@3.12/bin|' \
          -e 's|^executable = .*|executable = /opt/homebrew/opt/python@3.12/bin/python3.12|' "$V/pyvenv.cfg"
```

On the machine darkroom is developed on, this stopped the crashes: four Siril runs and a darkroom stage afterwards produced no crash reports, and Siril kept the change. A Siril update may rebuild the environment and bring the crash back; if so, run the same commands again. Removing Homebrew's `python@3.12` breaks the link; patch upgrades do not. To undo, move `$V.backup` back to `$V`.

If you would rather not touch Siril's files, `defaults write com.apple.CrashReporter DialogType notification` turns the dialogs into notifications (`defaults delete com.apple.CrashReporter DialogType` undoes it).

## `siril-cli` cannot find the script

`siril-cli` starts in `~/Pictures`, so a script passed by a relative path is not found, and so are relative paths inside it. darkroom passes everything as absolute paths. This only matters if you run the scripts in `siril/` by hand.

## Siril settings changed after a run

siril-cli saves its settings when a script ends, so the `set16bits`, `set32bits` and `setext` lines in darkroom's scripts, and the focal length and pixel size of the plate solve, used to stay in Siril's preferences. darkroom now runs every Siril script on a throwaway copy of those settings (`siril-cli -i`), so your own `config.1.4.ini` is never written; Siril still finds its download cache, `spcc_mirrors.json` and the SPCC database where they are. If you ran an older darkroom, check Siril's preferences once: it may have left processed images at 16-bit, the FITS extension at `.fit`, and the focal length and pixel size of your last target.

## A stage stops with NaN or infinite values

`denoise`, `deconv` and `finish` check their result and refuse to write an image with NaN or infinite pixels ("the result has N NaN or infinite value(s); nothing was written"), instead of passing garbage to the next stage. Look at the previous stage's output first. The cases darkroom knows of are handled without an error, but an image with large empty areas, such as a mosaic stacked with `mosaic_crop = "none"` or a stack framed with `max`, is still a poor input: crop the stack to the area with data (`mosaic_crop = "rotate"` or `"rect"`, or `framing = "min"`) and run again. The known cases: `deconv` leaves tiles of one level as they are, `denoise` logs a warning for a channel that is mostly one level, and `finish` logs when it skips the yellow/blue step (no pixel inside the target mask) or takes the sky colour from the faintest part of the mask (a nebula that fills the frame).

## The shell splits paths with spaces

The Seestar names its folders with spaces (`M 31_sub`). Quote paths in the shell. In zsh, an unquoted variable holding several arguments is passed as one argument, so write the arguments out.

## An unexpected error with a one-line message

darkroom reports an unexpected error (a folder it cannot write to, a value it cannot parse) as one line and exits with code 1. Set `DARKROOM_DEBUG=1` to see the Python traceback instead, for a bug report.
