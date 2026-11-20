# check=skip=FromPlatformFlagConstDisallowed
# darkroom in a Linux container: Siril 1.4.5 + GraXpert 3.1.0rc2 + darkroom.
#
# StarNet is NOT in this image: its licence does not allow redistribution. Inside the
# container, `darkroom get-starnet` downloads it from the official site after you read
# and accept its licence, into the /cache volume.
#
# Build:  docker build --platform linux/amd64 -t darkroom .
# Run:    docker/darkroom-docker --subs DIR --work DIR -- <darkroom command>   (see docs/docker.md)
#
# x86-64 only (hence the fixed --platform below): StarNet2 and GraXpert ship Linux builds
# for x86-64 only. On Apple Silicon
# Docker runs this image under emulation, which is slow; Mac users should install natively.
#
# The container runs as an unprivileged user. docker/darkroom-docker passes your own uid and
# gid with --user, so files in the work folder are yours; any uid works, because /cache and
# /work are writable by everyone (sticky, like /tmp).

# debian:trixie-slim, pinned by the digest of its multi-platform index (update both together)
ARG BASE=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ARG SIRIL_URL=https://free-astro.org/download/Siril-1.4.5-x86_64.AppImage
ARG SIRIL_SHA256=99b371b5edae28db8e66862acbcf483606d77e4fbbdf89613f6f02ffce0d5ecb
ARG GRAXPERT_URL=https://github.com/Steffenhir/GraXpert/releases/download/3.1.0rc2/graxpert-linux-amd64.zip
ARG GRAXPERT_SHA256=f651be473fdd6522b2e8e47d3acfcadedbf3cf2be90552aae9333396fc355cf9

# ---- stage 1: fetch and unpack the tools (nothing from here but /opt reaches the image)
FROM --platform=linux/amd64 ${BASE} AS tools
ARG SIRIL_URL SIRIL_SHA256 GRAXPERT_URL GRAXPERT_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip squashfs-tools python3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/fetch
RUN curl -fsSL -o siril.AppImage "$SIRIL_URL" \
 && echo "$SIRIL_SHA256  siril.AppImage" | sha256sum -c - \
 && curl -fsSL -o graxpert.zip "$GRAXPERT_URL" \
 && echo "$GRAXPERT_SHA256  graxpert.zip" | sha256sum -c -
# An AppImage is an ELF runtime followed by a squashfs image. It cannot run itself under
# emulation, so unpack the squashfs directly; it starts where the ELF section headers end.
RUN off=$(python3 -c "import struct;d=open('siril.AppImage','rb').read(80);o=struct.unpack_from('<Q',d,0x28)[0];e,n=struct.unpack_from('<HH',d,0x3A);print(o+e*n)") \
 && unsquashfs -q -o "$off" -d /opt/siril siril.AppImage \
 && unzip -q graxpert.zip -d /opt/graxpert

# ---- stage 2: the image
FROM --platform=linux/amd64 ${BASE}
LABEL org.opencontainers.image.title="darkroom" \
      org.opencontainers.image.description="Astrophotography editing from the command line, built on Siril, GraXpert and StarNet." \
      org.opencontainers.image.source="https://github.com/tks98/darkroom" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
# GraXpert's command line still loads its Tk interface, so it needs the X11 client
# libraries even though no display is used.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv \
      libx11-6 libxft2 libxss1 libxext6 libxrender1 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=tools /opt/siril /opt/siril
COPY --from=tools /opt/graxpert /opt/graxpert
COPY docker/siril-cli /usr/local/bin/siril-cli
COPY docker/entrypoint /usr/local/bin/darkroom-entrypoint
COPY docker/NOTICE /usr/share/doc/darkroom-image/NOTICE

COPY pyproject.toml README.md LICENSE /src/darkroom/
COPY darkroom /src/darkroom/darkroom
COPY siril /src/darkroom/siril
COPY presets /src/darkroom/presets
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir /src/darkroom \
 && rm -rf /src/darkroom \
 && mkdir -p /cache /work /subs /darks && chmod 1777 /cache /work \
 && groupadd --gid 1000 darkroom \
 && useradd --uid 1000 --gid 1000 --home-dir /cache --no-create-home --shell /usr/sbin/nologin darkroom

# Everything that is downloaded at run time (GraXpert's models, StarNet after you accept
# its licence, the fixed-batch denoise model) lives under HOME, which is the /cache volume.
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/cache \
    DARKROOM_SIRIL=/usr/local/bin/siril-cli \
    DARKROOM_GRAXPERT=/opt/graxpert/GraXpert-linux/GraXpert \
    PYTHONUNBUFFERED=1
VOLUME /cache
WORKDIR /work
USER 1000:1000
ENTRYPOINT ["darkroom-entrypoint"]
CMD ["--help"]
