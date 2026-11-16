#!/bin/zsh
# M31 (Andromeda), ZWO Seestar S30 Pro: the exact command sequence used for
# this dataset. Edit the four variables, then run:  zsh examples/m31.sh
#
# Optional tool locations (defaults are the standard macOS installs):
#   export DARKROOM_SIRIL=/path/to/siril-cli
#   export DARKROOM_GRAXPERT=/path/to/GraXpert
#   export DARKROOM_GRAXPERT_MODEL=/path/to/denoise-ai-models/3.0.2/model.onnx
set -euo pipefail

SUBS="/path/to/M 31_sub"                 # folder with the 30 s light subs (*.fit)
DARK="/path/to/MasterDark_30s.fit"       # master dark
WORK="/path/to/M31_work"                 # work dir (needs ~31 GB free during stacking)
export DARKROOM_STARNET_DIR="/path/to/StarNetv2CLI_MacOS"

darkroom stack   --subs "$SUBS" --dark "$DARK" --work "$WORK"
darkroom bge     --work "$WORK" --smoothing 0.1
darkroom color   --work "$WORK" --sensor "Sony IMX585" --filter "UV/IR Block" --focal 160 --pixel 2.9 --catalog gaia
darkroom denoise --work "$WORK" --strength 0.6
darkroom stretch --work "$WORK" --asinh 25 --shadows -2.5 --target-bg 0.10 --sat 0.35
darkroom starnet --work "$WORK"
darkroom finish  --work "$WORK" --gamma 1.6 --star-strength 0.95 --mid 0.45 --sat 1.2 \
                 --yb 1.8 --sharpen 0.8 --crop 0,512,1710,2565

# Same thing in one go (all settings above are the defaults):
#   darkroom run --subs "$SUBS" --dark "$DARK" --work "$WORK" --crop 0,512,1710,2565
#
# The calibrated/registered frames in $WORK/process are only needed by the
# stack stage; delete them afterwards to free the space:
#   rm -rf "$WORK/process"
