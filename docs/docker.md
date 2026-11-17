# Running darkroom in Docker

The Docker image runs darkroom on Linux, and on Windows or macOS through Docker Desktop, without installing Siril or GraXpert yourself. It contains darkroom, Siril 1.4.5 and GraXpert 3.1.0rc2 on Debian 13.

StarNet is not in the image. Its licence does not allow it to be redistributed, so you download it once inside the container with `darkroom get-starnet`, which shows you its licence and installs it only if you accept. GraXpert's AI models are also downloaded on first use. Both are kept in a Docker volume, so this happens once.

On a Mac, prefer the [native install](install.md). The image is x86-64 only, because StarNet2 and GraXpert publish Linux builds for x86-64 only, so on Apple Silicon Docker has to emulate it. It also cannot use the Apple GPU, so denoising runs on the CPU. See [Speed](#speed).

## Build the image

```sh
git clone https://github.com/tks98/darkroom.git
cd darkroom
docker build --platform linux/amd64 -t darkroom .
```

The build downloads Siril and GraXpert from their official release pages and checks each file against a SHA-256 hash pinned in the `Dockerfile`. The image is about 2.6 GB, most of it GraXpert.

## Run it

`docker/darkroom-docker` runs the image with your folders mounted. Inside the container they appear as `/subs` (your light frames, read-only), `/darks` (your master darks, read-only) and `/work` (the work directory), so use those paths in darkroom commands and in `darkroom.toml`:

```sh
docker/darkroom-docker --subs ~/astro/M31_sub --darks ~/astro/darklibrary --work ~/astro/m31 -- \
    init --work /work --subs /subs --dark-library /darks

docker/darkroom-docker --work ~/astro/m31 -- get-starnet

docker/darkroom-docker --subs ~/astro/M31_sub --darks ~/astro/darklibrary --work ~/astro/m31 -- \
    run --work /work
```

Everything after `--` is passed to `darkroom`. Results land in the work folder on your machine as usual. On Linux the wrapper runs the container as your user, so the files are yours.

`docker/darkroom-docker --work ~/astro/m31 -- doctor` checks the setup inside the container.

## StarNet

`darkroom get-starnet` finds the current StarNet2 command-line build for Linux on the official StarNet site, downloads it, prints its full licence, and asks you to type `accept`. Only then does it install it, into the cache volume. It records the version, the download address and when you accepted in an `accepted.json` file next to it. If you do not accept, it deletes the download and installs nothing.

StarNet2's licence lets you use it free of charge for astrophotography processing. It does not allow redistributing it or using its output to train or test other software. Read it in full when `get-starnet` shows it.

On a machine without internet access, download the Linux command-line zip from the official StarNet site yourself, put it in the work folder, and install it with `get-starnet --zip /work/<file>.zip`. You still have to accept the licence.

## The cache volume

The image sets its home directory to `/cache`, which the wrapper mounts as the Docker volume `darkroom-cache`. It holds GraXpert's models (about 600 MB), StarNet (about 200 MB), the GPU copy of the denoise model, and Siril's downloaded star catalogues. To start over, remove the volume:

```sh
docker volume rm darkroom-cache
```

## Speed

The M31 run (404 subs of 30 s) took these times. The Docker column is the image running under emulation on an Apple Silicon Mac; on a real x86-64 Linux machine it should be several times faster, but that has not been measured.

| Stage | Mac, native | Docker, emulated |
|---|---|---|
| stack | 2.7 min | 7.0 min |
| bge | 9 s | 30 s |
| color | 22 s | crashes (see below) |
| denoise | 23 to 38 s (GPU) | 14.7 min (CPU) |
| stretch | 1 s | 2 s |
| starnet | 10 s (StarNet2) | 1.8 min (StarNet2) |
| finish | 2 s | 4 s |

Denoising is the slow part: the image has no GPU support, so it runs on the CPU. The result is the same either way: the container's denoised image differed from the Mac's GPU result by 0.0000006 on average.

The finished image from the container, made with StarNet2 2.6.2 and CPU denoising, differed from the native Mac result (StarNet v2.0.2, GPU denoising) by 0.04 of an 8-bit level on average; side by side they look the same. The container kept the same 346 of 404 frames and chose the same crop.

## Colour calibration crashes under emulation

On an Apple Silicon Mac, Siril's colour calibration (SPCC) crashes inside the container with a segmentation fault, at the photometry step, every time and also with a single thread. Plate solving just before it works. The same step works natively on the Mac, so this looks like a problem with Siril under Docker's x86-64 emulation, but it has not yet been tested on a real x86-64 Linux machine, so a Siril problem on Linux is not ruled out.

Until that is known, run the `color` stage natively on a Mac with darkroom installed, against the same work folder, and continue in the container:

```sh
darkroom color --work ~/astro/m31
docker/darkroom-docker --work ~/astro/m31 -- run --work /work --from denoise
```

## What is in the image

`/usr/share/doc/darkroom-image/NOTICE` inside the image lists every component, its licence and where to get its source. Siril and GraXpert are GPL-3; GraXpert's AI models are CC BY-NC-SA 4.0 and are downloaded by GraXpert from its own server, not included.

Two details of the image, in case you build your own:

- Siril is shipped by its authors as an AppImage, which cannot run itself under emulation. The build unpacks it instead, and `/usr/local/bin/siril-cli` starts Siril through the AppImage's own launcher, because Siril run directly on Debian 13's newer C library crashes.
- GraXpert's command line still loads its graphical toolkit, so the image includes the X11 client libraries, although no display is used. Without an NVIDIA GPU it logs a CUDA error at startup and carries on with the CPU; the error is harmless.
