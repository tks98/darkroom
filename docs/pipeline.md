# How it works

darkroom runs seven stages. Each reads the previous stage's file from the work directory and writes its own, so any stage can be redone without the ones before it. Logs from the external tools go to `<work>/logs/`.

| Stage | Tool | Reads | Writes |
|---|---|---|---|
| `stack` | Siril | subs, master dark | `stack.fit` |
| `bge` | GraXpert | `stack.fit` | `bge.fits`, `bge_background.fits` |
| `color` | Siril | `bge.fits` | `spcc.fit` |
| `denoise` | GraXpert's model, run with onnxruntime | `spcc.fit` | `denoised.fits` |
| `stretch` | Siril | `denoised.fits` | `stretched.fit`, `stretched.tif`, `stretched.jpg` |
| `starnet` | StarNet v2 | `stretched.tif` | `starless.tif` |
| `finish` | Python | `stretched.tif`, `starless.tif` | `final.tif`, `final.jpg` |

The numbers below come from the M31 run: 404 subs of 30 seconds from a Seestar S30 Pro, on an Apple Silicon Mac.

## stack

Siril subtracts the master dark, fixes hot and cold pixels found in the dark, and converts the colour-filter data to RGB. It then registers the frames in two passes (the first picks the best reference frame), keeps the best 90% by star sharpness and the best 90% by star roundness, and stacks with winsorized sigma rejection, weighting each frame by its sharpness.

For M31, 2 frames failed registration and 346 of 404 were kept, 2 h 53 min in all. The stack took about 3 minutes. The calibrated and registered frames in `process/` took 31 GB.

## bge

GraXpert's AI background extraction models the sky glow and subtracts it.

![Three panels of M31. "stack": the galaxy on a sky that is slightly brighter toward the lower right. "background model": a smooth grey gradient, dark at the upper left and light at the lower right, with its contrast exaggerated so it can be seen. "after bge": the same galaxy on an even sky.](images/m31_bge.jpg)

The model should be a smooth gradient. If it shows the outline of the target, it has taken real signal with it; raise `[bge] smoothing`. After this stage the M31 sky was even to within a fraction of the noise across the frame.

## color

Siril plate-solves the image to find exactly which stars are in it, then calibrates the colour against those stars' measured spectra from the Gaia DR3 catalogue (spectrophotometric colour calibration, or SPCC). It uses the sensor and filter profiles from the config.

For M31 the solve gave 3.68 arcseconds per pixel, and SPCC used 1,891 stars. Siril flagged the blue-channel fit as imprecise, which the logs show and the README mentions.

Siril downloads its star lists from VizieR. When VizieR is unreachable, this stage fetches the list from ESA's Gaia archive instead and retries. See [the catalogue server](troubleshooting.md#plate-solving-fails-because-the-catalogue-server-is-down).

## denoise

This runs GraXpert's AI denoise model (version 3.0.2) on the linear image, through onnxruntime on the Apple GPU. GraXpert's own GPU path fails on macOS 27 ([details](troubleshooting.md#graxperts-gpu-denoise-fails-on-macos)), so darkroom reimplements GraXpert's tiling and blending around the same model.

For M31 it took 23 seconds, against about 5 minutes on the CPU. It halved the pixel noise (the robust sigma of neighbouring-pixel differences went from 0.000100 to 0.000048). The output matches GraXpert's own CPU result to within 1e-5, apart from a constant offset that GraXpert adds and the stretch removes.

## stretch

Siril applies an arcsinh stretch, which brightens faint parts while keeping star colour, then a linked autostretch and a little saturation. This is where the image goes from linear data to something you can look at.

## starnet

StarNet v2 splits the stretched image into a starless image. The star layer is worked out later as the difference. With `[finish] stars = "keep"` (the cluster look) this stage is skipped.

## finish

With stars removed, `finish` works on two layers:

- The starless layer gets a black point, a midtone lift and saturation. Inside the target only, it then gets a yellow and blue contrast push and sharpening.
- The star layer is shrunk with a gamma curve.

The two are combined with a screen blend, the sky is made neutral, and the result is cropped. [Configuration](configuration.md#finish) covers each setting with examples.

The sky colour is measured as the median of every pixel outside the target. An earlier version measured only the darker half of those pixels. That is biased toward whichever colour channel is noisiest, and on M13 it added a blue cast to a sky that was already neutral. See [history](history.md#phase-5-a-second-target).

## Without StarNet

`darkroom star-reduce IN OUT` is a fallback that needs no StarNet. It builds a soft mask of star-sized bright spots and shrinks them with a minimum filter. It works on a stretched FITS file and is weaker than the StarNet path, since it cannot separate stars that sit on top of the galaxy.
