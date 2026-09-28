#!/usr/bin/env sh
# README preview: a small palette-optimised GIF cut from the rendered MP4.
# Needs a full ffmpeg on PATH (the one bundled with Remotion lacks palettegen).
set -eu
cd "$(dirname "$0")/../../docs"
ffmpeg -loglevel error -y -i teleporter-demo.mp4 \
  -vf "fps=10,scale=640:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=80:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  -loop 0 teleporter-demo-preview.gif
ls -l teleporter-demo-preview.gif
