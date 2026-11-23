# darkroom

Astrophotography editing from the command line, built on Siril, GraXpert and StarNet. Use it directly or through an agent.

darkroom is a command-line pipeline written in Python. You point it at a folder of sub-frames from a night of imaging, and it calibrates and stacks them in [Siril](https://siril.org), removes the sky gradient and the noise with [GraXpert](https://graxpert.com)'s models, calibrates the colour against Gaia star data, separates the stars from the target with [StarNet](https://www.starnetastro.com), and does the final blend and finishing itself.

I wrote it to process a night of M31 from a ZWO Seestar S30 Pro, then turned those scripts into something I could reuse on the next target. The package reproduced that first image byte for byte before any of it was generalised.

![M31, the Andromeda Galaxy, with satellite galaxies M32 (below the core) and M110 (upper right). The galaxy runs diagonally from upper left to lower right, with a cream-white core, blue outer arms and brown dust lanes, on a dark sky in a dense field of small stars.](docs/images/m31_final.jpg)

M31 with M32 and M110. ZWO Seestar S30 Pro (160 mm focal length, Sony IMX585 one-shot-colour sensor, IR-cut filter, equatorial mode) at gain 200. Two nights of 30 s subs, 15 and 20 November 2026: 734 kept, 6 h 7 min in total. The second night had a bright Moon, so its frames are noisier, and the frames it took after M31 dropped below 35° and into dawn were left out before stacking. Calibrated with the Seestar's own 5-frame master dark; no flats or bias. Finished with the galaxy look and the optional deconvolution stage at strength 0.5, then a GHS stretch, a darker sky, stronger local contrast and a cosmetic colour pass that turns the purple disk blue and the yellow core white (the settings are in the [gallery](docs/gallery.md#m31-the-andromeda-galaxy)); more targets and full-size crops are in the [gallery](docs/gallery.md).

## Compared with the Seestar's own stack

![Side-by-side comparison of the same field of M31. Left, labelled "Seestar in-app stack (1 night)": a grey-white galaxy with a soft core and faint, low-contrast dust lanes. Right, labelled "darkroom (2 nights)": the same galaxy with a cream-white core, blue outer arms, clearer brown dust lanes, smaller stars and a darker, more neutral background.](docs/images/m31_seestar_vs_darkroom.jpg)

On the left is the stack the Seestar app built during the first night (402 frames), resampled onto darkroom's field through the two images' plate solutions. The app stacks one session at a time, so it cannot combine the two nights. On the right is darkroom's two-night result. Most of the difference comes from three things: the colour is calibrated against Gaia star colours, the noise is removed before the stretch, and the galaxy and the stars are processed as separate layers, so the stars can be made smaller without touching the galaxy. Much of the colour on the right is a deliberate cosmetic choice (the blue disk and white core in particular), described in [configuration](docs/configuration.md#colour-and-sharpening).

![Three panels of the same M31 field. Left, "one 30 s sub": a grey frame where only the bright core and a few stars are visible. Middle, "stacked, quick stretch": the full galaxy and its satellites appear, mostly neutral in colour. Right, "finished": the final processed image.](docs/images/m31_progression.jpg)

## How it works

Seven stages, each reading the previous one's output from a work directory:

1. `stack`: Siril subtracts the dark (and, for a cooled camera, the bias and flats: master files or folders of raw frames), aligns the frames, drops the blurriest ones and those with stretched stars, and stacks the rest.
2. `bge`: GraXpert removes the sky gradient.
3. `color`: Siril plate-solves the image and calibrates the colour against Gaia DR3.
4. `denoise`: GraXpert's denoise model, run on the Apple GPU.
5. `stretch`: Siril brings the linear data up to a visible image.
6. `starnet`: StarNet separates the stars from the target.
7. `finish`: enhances the target, shrinks the stars, recombines, neutralises the sky and crops.

An optional `deconv` stage between `denoise` and `stretch` runs GraXpert's deconvolution model to sharpen the linear image. It is off by default and needs GraXpert's model, which it has GraXpert download the first time; see [configuration](docs/configuration.md#deconv).

A stage that is already up to date is skipped, so changing a finishing setting and running again takes about a second. [How it works](docs/pipeline.md) has the details and the numbers from the M31 run.

## Quick start

On a Mac you need Python 3.11 or later. `setup.sh` installs darkroom and then runs `darkroom setup`, which downloads [Siril](https://siril.org) and [GraXpert](https://graxpert.com) from their official releases if you do not have them, fetches GraXpert's denoise model, and offers to install [StarNet2](https://www.starnetastro.com) after you accept its licence. On Linux or Windows, use the [Docker image](docs/docker.md) instead.

```sh
git clone https://github.com/tks98/darkroom.git
cd darkroom
bash setup.sh
source .venv/bin/activate

darkroom init --work m31 --subs path/to/lights --dark-library path/to/darklibrary
darkroom run  --work m31
```

Setup ends with `darkroom doctor`, which checks every tool and prints a fix for anything missing. `init` reads the FITS headers, picks a master dark and writes a `darkroom.toml` you can edit. `run` does the rest. The first time, it is worth going one stage at a time with `--to STAGE` and looking at each result.

## Documentation

- [Installing](docs/install.md): requirements, `setup.sh`, `darkroom doctor`, environment variables, tests.
- [Running darkroom in Docker](docs/docker.md): the Linux image, for Linux, Windows or anywhere you would rather not install Siril and GraXpert.
- [Using darkroom](docs/usage.md): `init` and `run`, single stages, what to check after each stage, comparing variants, disk space.
- [Configuration](docs/configuration.md): every setting with its default, dark selection, bias and flats, framing, star reduction, cropping, and the galaxy, cluster and nebula looks.
- [How it works](docs/pipeline.md): what each stage does, with numbers from M31.
- [Troubleshooting](docs/troubleshooting.md): GraXpert on the GPU, the catalogue server, mismatched darks, StarNet and Gatekeeper, Siril's Python crashing on macOS, the SPCC server.
- [Using darkroom with an AI agent](docs/agents.md): the agent skill, for Claude Code or any agent that loads skills, and `AGENTS.md`.
- [Gallery](docs/gallery.md): M31 and M13, with the settings used and full-size crops.
- [History](docs/history.md): how darkroom was built and checked.

## Limitations

darkroom has been tested end to end on two targets (M31 and M13) from one camera (the Seestar S30 Pro), its mosaic stacking and the nebula look on two mosaics from the original S30, its calibration with raw darks, bias frames and flats on two targets from a ZWO ASI294MC Pro, and on one platform (macOS on Apple Silicon). Each look was made from one or two images, and the cluster look from only 7.5 minutes of data.

It handles one-shot-colour data. The lights can be calibrated with a master dark, and with bias frames and flats, each given as a master file or a folder of raw frames (see [Configuration](docs/configuration.md#bias-flats-and-folders-of-raw-frames)). Mono and LRGB data are not supported. Automatic dark selection has only been tested against a library built from copies of one real master dark. Not yet tested: the CPU denoise path, installing from a wheel, `setup.sh` on a fresh machine, and frame-selection percentages other than 90.

## Licence

Copyright (C) 2026 tks98. Released under the GNU General Public License, version 3 or later; see [LICENSE](LICENSE). It is GPL because the denoise stage reimplements GraXpert's GPL-3 `denoise()` algorithm.

## Credits

darkroom drives other people's work, and none of it is redistributed here. [Siril](https://siril.org) does the calibration, stacking, plate solving, colour calibration and stretching. [GraXpert](https://graxpert.com) provides the background extraction and the denoise model and algorithm. [StarNet](https://www.starnetastro.com) removes the stars. Star positions and colours come from the European Space Agency mission [Gaia](https://www.cosmos.esa.int/gaia), processed by the Gaia Data Processing and Analysis Consortium, through the [ESA Gaia archive](https://gea.esac.esa.int/archive/).
