# Configuration

Each work directory has a `darkroom.toml`, written by `darkroom init`. A setting can come from five places. The first one that sets it wins:

1. a command-line flag
2. `darkroom.toml`
3. the look preset (`[target] look`)
4. the camera preset (`[target] camera`)
5. the built-in default

Unknown keys and bad values are errors that name the file and the key. Relative paths are relative to the config file.

`examples/m31.toml` is the configuration behind the M31 image in the README, and `examples/m13.toml` is the one the `cluster` look came from.

## [target]

| Key | Default | Meaning |
|---|---|---|
| `name` | from the FITS `OBJECT` | target name, for your own reference |
| `subs` | none | folder with the light subs: the FITS files (`.fit`, `.fits` or `.fts`, any case) that match the camera preset's `sub_glob` (`Light_*.fit` for the shipped presets), or every FITS file there when none matches. Other files are ignored. darkroom links the chosen subs into `<work>/process/subs` and Siril works there, so nothing is written to this folder |
| `dark` | none | `"auto"` to pick from `dark_library`, `"none"`, the path of a master dark, or a folder of raw darks |
| `dark_library` | none | folder of master darks, used when `dark = "auto"` |
| `bias` | `"none"` | the path of a master bias, or a folder of raw bias frames (flag `--bias`) |
| `flats` | `"none"` | the path of a master flat, or a folder of raw flats (flag `--flats`) |
| `camera` | detected by `init` | camera preset name, or the path of your own `.toml` |
| `look` | `galaxy` | look preset name, or the path of your own `.toml` |

### Picking a master dark

With `dark = "auto"`, darkroom looks in the dark library for masters with the same exposure as the subs, keeps the ones with the same gain, and takes the one whose temperature is closest to the subs' median sensor temperature. Ties go to the newest. It prints which dark it chose and warns when the temperature is more than 5 °C off. The library layout is the Seestar's: `<library>/<exposure>s/MasterDark_*.fit`.

The Seestar's sensor is not cooled, so this matters more than it would with a cooled camera. During the M31 session the sensor went from 21.4 °C down to 8.6 °C, and the only master dark had been taken at 21.5 °C. See [master dark temperature](troubleshooting.md#the-master-dark-does-not-match-the-subs) for what that did.

### Bias, flats and folders of raw frames

A folder given as `dark`, `bias` or `flats` is stacked into `<work>/masters/master_dark.fit`, `master_bias.fit` or `master_flat.fit` before the lights are calibrated, and rebuilt only when its frames change. The flats are calibrated with the bias. The lights get the dark and the flat; the bias is subtracted from the lights only when there is no dark. See [calibration frames](usage.md#calibration-frames) for the recipe and the checks against the lights. Work directories made before these settings existed stay up to date: they are recorded only when set.


| Key | Default | Meaning |
|---|---|---|
| `framing` | `min` | `min`, `cog` or `max`; see below |
| `keep_fwhm` | 90 | percent of frames kept, sharpest first (weighted FWHM) |
| `keep_round` | 90 | percent of frames kept, roundest stars first |
| `keep_bkg` | 100 (off) | percent of frames kept, darkest sky first |
| `keep_stars` | 100 (off) | percent of frames kept, most stars first |
| `clean` | false | delete `<work>/process` after a successful stack |
| `mosaic` | false (`init` turns it on for a mosaic) | plate-solve every frame and stack them onto one canvas; see [mosaics](usage.md#mosaics) |
| `mosaic_crop` | `rotate` | how a mosaic's canvas is cropped: `rotate`, `rect` or `none`; see below |

### Framing

The Seestar re-centres the target during a session, so the frames do not line up exactly. On the M31 night they wandered by up to 300 pixels. Framing decides what to do with the edges:

![The same M31 stack framed three ways, at the same scale. "min: 1710x3632" is the narrowest. "cog: 2160x3840" is the full sensor frame. "max: 2609x4050" is the widest, with a noisy strip down its left side and ragged, partly empty edges.](images/m31_framing.jpg)

`min` keeps only the area every frame covers. It is the cleanest and the narrowest. `cog` keeps a full sensor frame centred on where the frames were on average; on M31 it was 2160×3840 instead of 1710×3632, with edge strips about 20% noisier. `max` keeps the bounding box of all frames (2609×4050). Its edges are ragged and much noisier, about 1% of it is never covered at all, its brightness scale differs from the other two, and it always needs cropping.

To compare framings without restacking from scratch:

```sh
darkroom stack --work m31 --reuse-registration --framing cog --out stack_cog
```

To adopt one, set `[stack] framing` and restack.

With `mosaic = true` the framing is always `max`, followed by a crop to the largest rectangle with data everywhere. A `framing` set to something else is ignored with a warning.

`mosaic_crop` (flag `--mosaic-crop`) decides that crop. `rotate`, the default, tries every angle and turns the image when the largest rectangle at the best angle keeps at least 10% more pixels than the largest upright one; otherwise it crops upright. A turned image no longer has north up, and it is resampled once more (cubic spline). `rect` always crops upright, as darkroom did before `rotate` existed, and keeps the orientation. `none` leaves the whole canvas, empty parts and all, which the later stages do not expect; use it only to crop by hand. Work directories stacked before this setting existed count as up to date, so to re-crop one with `rotate`, restack it (`darkroom stack --reuse-registration` is enough).

### Frame selection

`keep_fwhm` and `keep_round` drop the blurriest frames and the ones with the most elongated stars. Frames that are fully clouded fail registration and drop out by themselves. `keep_bkg` and `keep_stars` can also drop frames with a bright sky or few stars, which is what thin cloud does to a frame.

Compare before you rely on them. On the M13 data, where thin cloud crossed several frames, dropping the 3 stacked frames with the brightest sky and fewest stars (15 down to 12) left the gradient unchanged and made the stack 6% noisier. Siril's rejection and normalisation had already dealt with the cloud.

## [bge]

| Key | Default | Meaning |
|---|---|---|
| `smoothing` | 0.1 | GraXpert background smoothing |

If `bge_background.fits` shows the outline of the target, the model has taken real signal with it; raise `smoothing`.

## [color]

| Key | Default | Meaning |
|---|---|---|
| `sensor` | `Sony IMX585` | sensor name as Siril's SPCC lists it |
| `filter` | `UV/IR Block` | filter name as Siril's SPCC lists it |
| `focal` | 160 | focal length in mm, for plate solving |
| `pixel` | 2.9 | pixel size in µm, for plate solving |
| `catalog` | `gaia` | Siril's plate-solve catalogue |
| `whiteref` | `Average Spiral Galaxy` | SPCC white reference |
| `catalog_fallback` | true | fetch Gaia DR3 from ESA when Siril's catalogue server is down |
| `narrowband` | `"none"` | SPCC narrowband mode: wavelength/bandwidth in nm for red, green and blue, e.g. `"656.28/12,500.7/12,500.7/12"`; replaces `filter` |
| `spcc_local` | true | keep a local copy of the star spectra SPCC needs for the field (fetched once) and run SPCC on it, instead of letting Siril download them each time |

The camera preset sets `sensor`, `filter`, `focal` and `pixel`, and `init` writes the values from the FITS header. The Seestar's IRCUT filter maps to `UV/IR Block` and its LP filter to `ZWO Seestar LP`.

### Dual-band filters: `narrowband`

SPCC models the filter with a transmission curve from Siril's database. A dual-band filter that is not in it (an "HaO3" filter on an ASIAIR, say) can be described by its bands instead: `narrowband` switches SPCC to its narrowband mode and gives the wavelength and bandwidth of the band each colour channel sees. For hydrogen-alpha and oxygen III on a colour camera, red sees Hα at 656.28 nm and green and blue both see OIII at 500.7 nm:

```toml
[color]
narrowband = "656.28/12,500.7/12,500.7/12"
```

Use your filter's bandwidths if you know them; the result depends little on them. The flag is `--narrowband`, and `"none"` switches it off. When it is set, `filter` is ignored. On the Seestar's LP filter, narrowband mode and the filter curve gave the same colour to within 0.3% (see [history](history.md#colour-through-the-lp-filter)).

### SPCC's star spectra: `spcc_local`

SPCC needs the Gaia DR3 spectra of the stars in the field. Siril downloads them from its own servers on every run, Zenodo first, and Siril 1.4.5 crashes when that server answers with an error (see [troubleshooting](troubleshooting.md#colour-calibration-stops-with-siril-could-not-download-the-star-spectra)). With `spcc_local = true`, the default, the `color` stage first makes sure darkroom's local copy covers the field, fetching what is missing from the same servers (with retries, and the next server when one fails), and then runs SPCC on that copy, offline. A field is fetched once; later runs, and other targets in the same part of the sky, use what is there. A Seestar field takes about 15 MB.

The copy is in `<cache>/gaia-xpsampled` (`~/Library/Caches/darkroom` on a Mac, `~/.cache/darkroom` on Linux, `$DARKROOM_CACHE` when that is set). It holds Siril's catalogue files, `siril_cat1_healpix8_xpsamp_<n>.dat`, with only the parts of the sky darkroom has fetched; `coverage.json` lists them. Deleting the folder is safe: it is fetched again when needed. darkroom points Siril at it for the run only, through a throwaway copy of Siril's settings, so your own Siril settings do not change.

If the fetch fails on every server, Siril downloads the spectra itself, as it does with `spcc_local = false` (flag `--no-spcc-local`). The colour is the same either way: on M31 both gave the same factors and the same `spcc.fit`, pixel for pixel.

## [denoise]

| Key | Default | Meaning |
|---|---|---|
| `strength` | 0.6 | 0 to 1 |
| `cpu` | false | run on the CPU instead of the Apple GPU (about 15 times slower) |

## [deconv]

Optional deconvolution with GraXpert's object deconvolution model, between `denoise` and `stretch`. It is off by default. It needs GraXpert's model (1.0.1, 279 MB), which `darkroom deconv` has GraXpert download the first time, or `darkroom fetch-models --deconv`; set `DARKROOM_DECONV_MODEL` if the file is somewhere else.

| Key | Default | Meaning |
|---|---|---|
| `strength` | 0 (off) | 0 to 1. At 0 the stage is skipped and `stretch` reads `denoised.fits` |
| `psf` | `auto` | star FWHM in pixels, or `"auto"` to measure it from `denoised.fits` |
| `cpu` | false | run on the CPU instead of the GPU |

Flags: `darkroom deconv --strength 0.5 --psf 3.2 --cpu`; `darkroom run --cpu` also covers this stage. Turning `strength` from 0 to a value, or back, reruns `stretch` and everything after it. Work directories made before this stage existed stay up to date while it is off. Start around 0.3 to 0.5 and compare 1:1 crops before and after: dark rings around bright stars or a crunchy sky mean the strength is too high.

## [stretch]

| Key | Default | Meaning |
|---|---|---|
| `method` | `asinh` | the first stretch: `asinh` (Siril's `asinh -human`) or `ghs` (Siril's generalised hyperbolic stretch, `ght`) |
| `asinh` | 25 | Siril arcsinh stretch factor (`asinh` method only) |
| `ghs_d` | 4 | `ghs` stretch strength, 0 to 10 |
| `ghs_b` | 12 | `ghs` local intensity, -5 to 15; higher values concentrate the stretch around `ghs_sp` |
| `ghs_hp` | 0.9 | `ghs` highlight protection, 0 to 1; above this level the stretch is linear |
| `ghs_sp` | `auto` | `ghs` symmetry point, 0 to `ghs_hp`, or `"auto"` for the sky level of the input |
| `balance` | `"1,1,1"` | R,G,B multipliers on the linear image before the stretch, each 0.5 to 2 |
| `shadows` | -2.5 | autostretch shadow clipping, in sigma |
| `target_bg` | 0.10 | autostretch target background level |
| `sat` | 0.35 | Siril saturation amount |

A low `asinh` gives a softer, flatter image with a brighter core. Higher values keep the core resolved and the star colours visible, but too high mottles the sky with colour noise. The galaxy and nebula looks use 25; the cluster look uses 100.

`method = "ghs"` replaces the arcsinh step with `ght -D= -B= -SP= -HP= -human`; the autostretch, saturation and saves after it are the same. With `ghs_sp = "auto"` darkroom measures the sky level of the linear input before the script runs: the median, over the pixels that are not 0 in every channel, of the mean of R, G and B (after `balance`). It prints the value and writes it to `logs/stretch.log`. On the two-night M31 stack with SPCC colour, GHS (D 4, b 12, HP 0.9, SP at the sky level of 0.0023) kept the core from blowing out to white and showed the dust lanes all the way to the centre.

`balance` multiplies R, G and B before the stretch (a Siril `ccm` line; it is left out at 1,1,1). It is a cosmetic shift on top of SPCC, not measured colour. On the same M31 stack `"0.92,0.90,1.18"` gave a blue-lavender outer disk. Pushed further it goes wrong: `"0.78,1,1.32"` gave a teal cast, and `"0.88,0.86,1.26"` with a higher `[finish] sat` and `yb` gave a hard purple edge where the finish galaxy mask ends.

The `galaxy` look keeps `asinh` and `balance = "1,1,1"`. For a galaxy with a bright core, `method = "ghs"` with `balance = "0.92,0.90,1.18"` is worth trying as a variant. Changing `method`, `balance` or a `ghs_*` setting (while `method = "ghs"`) reruns `stretch` and everything after it. Work directories stretched before these settings existed stay up to date while `method = "asinh"` and `balance = "1,1,1"`.

## [starnet]

| Key | Default | Meaning |
|---|---|---|
| `dir` | see below | folder with the StarNet command line and its weights |

darkroom looks for StarNet in this order: `--starnet-dir`, `[starnet] dir`, `$DARKROOM_STARNET_DIR`, then the newest StarNet2 installed by `darkroom get-starnet`. A folder with `starnet2` in it is StarNet2 2.6; one with `starnet++` is the older v2.0.2.

## [finish]

| Key | Default | Meaning |
|---|---|---|
| `stars` | `reduce` | `reduce`: StarNet removes the stars, the rest is enhanced, the stars go back smaller. `keep`: no star removal, and StarNet is not run |
| `gamma` | 1.6 | above 1 shrinks stars (star layer only) |
| `star_strength` | 0.95 | brightness of the star layer |
| `mid` | 0.45 | midtone; below 0.5 brightens |
| `sat` | 1.2 | saturation |
| `yb` | 1.8 | yellow and blue contrast inside the target (1 is off) |
| `core` | 0.3 | compress the brightest part of the target, keeping its detail (0 is off) |
| `blue_shift` | 0 | turn purple hues toward blue, 0 (off) to 1 (cosmetic) |
| `core_white` | 0 | take colour out of the brightest part of the image, 0 (off) to 1 (cosmetic) |
| `sharpen` | 0.8 | sharpening of the target |
| `sharpen_radius` | 2.0 | sharpening radius in pixels |
| `black` | 0.02 | black point |
| `erode` | 0 | erosion passes on the star layer |
| `neutral` | true | make the sky colour neutral |
| `crop` | `none` | `none`, pixels `"x,y,w,h"`, or a shape like `"2:3"` |
| `crop_center` | the target | `"x,y"` centre for a shape crop |
| `crop_scale` | 1.0 | fraction of the largest box of that shape |

With `stars = "keep"`, only `mid`, `sat`, `black`, `neutral` and the crop settings apply, to the whole image. The others work on the star layer or inside the target and need StarNet.

### Shrinking stars

The star layer is dimmed with a gamma curve, which takes more off a star's faint halo than off its bright centre. Stars get smaller and fainter, but they all stay:

![The same patch of star field at full size with three star settings. "gamma 1.0" leaves the stars as stacked. "gamma 1.6", the default, makes them visibly smaller and dimmer. "gamma 2.4" thins the field out noticeably.](images/m31_gamma.jpg)

Past about 2.0 the field starts to look thinned out. `erode` shrinks stars with a minimum filter instead; any value above 0 deletes the faint 1 to 2 pixel stars entirely, which looks fake, so leave it at 0. It is there for completeness.

### Colour and sharpening

`yb` pushes yellow and blue apart inside the target only, which makes a galaxy's warm core and bluer arms easier to tell apart. It is cosmetic, not measured colour. On M31, 1.8 looked right; 2.6 turned the core green-yellow and the arms purple. `sharpen` applies an unsharp mask to the target's brightness, with the sky masked off so its noise is left alone. Too much puts halos along dust lanes.

`core` is for a bright galaxy core that the stretch has pushed close to white. It splits the target's brightness into a smooth base and the detail on top of it, lowers the base where it is above 55% brightness, and keeps the detail, so the core fades smoothly instead of flattening into a white disc. Colours keep their ratios, and nothing below the threshold changes. On M31 at 0.3, no pixel in the centre is clipped any more (before, 7% of the centre 60 pixels across was at 98% brightness or more), M110's core is tamed the same way, and about 2% of the image changes. At 0.4 the core starts to look flat and greyish. It cannot show structure the data does not have: at the Seestar's 4 arcseconds per pixel, M31's core is a smooth bulge.

`blue_shift` and `core_white` are a last colour pass on the finished image, done in Oklab, a colour space in which hue and lightness can be changed separately. Both are cosmetic and off by default. `blue_shift` turns hues in the purple band (Oklab hue 290 to 360 degrees, strongest at 325) toward blue (275); other hues, such as a yellow core or brown dust lanes, are left alone. `core_white` reduces colour where Oklab lightness is above 0.5, reaching the full amount at 0.8, so a bright core goes white while the fainter disk keeps its colour. Lightness is unchanged by both.

On the two-night M31 stack (GHS, `balance = "0.92,0.90,1.18"`, `black = 0.06`, `sharpen = 0.8`, `sharpen_radius = 25`), the outer disk was purple (Oklab hue 320) and the core strongly yellow. `blue_shift = 1` and `core_white = 0.75` moved the disk to blue and the core to cream-white, close to the look of popular processed M31 images, and the sky stayed neutral. A balance shift cannot do this: taking red out to turn purple into blue lets green take over, which gave a green-teal cast and a yellow-green core.

### Cropping

`crop = "2:3"` takes the largest box of that shape that fits, centred on the brightest part of the blurred image (the starless image, or the stretched one when stars are kept). For a target that is small in the frame, `crop_scale` shrinks that box: M13 used `crop = "1:1"` with `crop_scale = 0.45`. `crop_center = "x,y"` overrides the centre when the brightest point is the wrong place, and `"x,y,w,h"` crops by pixels.

## Value checks

Values are checked when the settings are read, before any stage runs: `[color] focal` and `pixel` above 0; `[bge] smoothing` and `[denoise] strength` from 0 to 1; `[stretch] asinh` above 0, `shadows` from -10 to 0, `target_bg` between 0 and 1, `sat` at least -1, `method` `asinh` or `ghs`, `ghs_d` from 0 to 10, `ghs_b` from -5 to 15, `ghs_hp` from 0 to 1, `ghs_sp` `"auto"` or from 0 to `ghs_hp`, `balance` three numbers from 0.5 to 2; `[finish] gamma` and `sharpen_radius` above 0, `mid` between 0 and 1, `black` from 0 to below 1, `sat`, `yb` and `sharpen` at least 0, `core`, `star_strength`, `blue_shift` and `core_white` from 0 to 1, `erode` a whole number of at least 0. NaN and infinity are refused. The `[color]` names cannot contain a double quote.

## Looks

A look is a preset for `[stretch]` and `[finish]` (and the SPCC white reference). Three are shipped, in `presets/looks/`:

- `galaxy`, the default, comes from M31. StarNet removes the stars, the galaxy is enhanced, and the stars go back smaller.
- `cluster` comes from M13 and is for targets made of stars. StarNet would remove the target itself and star reduction would dim it, so this look keeps the stars and skips StarNet. It stretches harder (`asinh = 100`) to keep the core resolved, and uses `sat = 1.3` and `black = 0.04`.
- `nebula` comes from two Seestar S30 mosaics of emission nebulae, the Rosette and the Flame and Horsehead area, taken through the Seestar's dual-band LP filter. It turns the yellow/blue push off (`yb = 1`), because on the Rosette it turned the hydrogen-alpha red to orange. It stretches to a brighter background (`target_bg = 0.15`, `shadows = -2.8`) so the faint outer gas shows, and uses more saturation (`sat = 1.5`; 1.6 added colour noise to the sky), stronger star shrinking (`gamma = 1.8`) for star-rich Milky Way fields, a darker sky (`black = 0.04`), gentler sharpening (0.5) and `core = 0.2`.

Each look was made from one or two images, and the cluster look from only 7.5 minutes of data. The nebula look's colour calibration used Siril's normal (broadband) mode with the LP filter's curve; it has not been tuned for a dual-band filter yet. If you are not sure which one a target needs, run the galaxy look once and open `starless.tif`: if the target has gone from it, use `stars = "keep"`.

To make your own, copy a file from `presets/looks/`, change it, and point `look` at its path.

## Cameras

Three camera presets are shipped: `seestar-s30-pro` (160 mm, Sony IMX585), `seestar-s30` for the original S30 (150 mm, Siril's `ZWO Seestar S30` sensor entry), and `zwo-asi294mc-pro`, a cooled colour camera (Sony IMX294, 4.63 µm pixels, Bayer RGGB). `init` detects the Seestars from the `CREATOR` keyword in the FITS header and the ASI294MC Pro from `INSTRUME`; a preset whose text equals the header value wins, otherwise the longest matching text, so S30 Pro subs never get the S30 preset. Besides the `[color]` values, each records the Bayer pattern, which files count as subs (`Light_*.fit`), how master darks are named for `dark = "auto"`, and the filter-name mapping. The ASI294MC Pro preset has no focal length, since that depends on the telescope: `init` writes the subs' `FOCALLEN`, and without one you set `[color] focal` yourself. It has no filter map either, since filter names are whatever the capture software was told.

In a camera preset, `[camera]` takes `name`, `match_keyword`, `match`, `bayer` and `sub_glob`, and `[darks]` takes `glob` and `filename` (a regular expression); any other key is an error. A `sub_glob` that ends in `.fit` also matches `.fits` and `.fts`, in any case. With `dark = "auto"`, darks of another size or Bayer pattern than the subs are skipped, and the dark that is picked is checked like one given by name. `EXPOSURE` is read when the header has no `EXPTIME`.
