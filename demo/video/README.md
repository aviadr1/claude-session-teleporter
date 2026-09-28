# Promo video

A 29-second, 1920×1080, 30 fps promo for claude-session-teleporter, built with
[Remotion](https://www.remotion.dev/) (videos written as React components).
The rendered files are committed at `docs/teleporter-demo.mp4` and
`docs/teleporter-demo-preview.gif`.

This project lives outside the Python package. The wheel and sdist use
allowlists in `pyproject.toml`, so nothing here is shipped.

## Every line of terminal text is real output

The video never types out tool output by hand. `capture/capture.py`:

1. builds a fresh fake Claude Code store with `docs/demo_fixture.py`, the same
   demo store behind `docs/demo.gif`, under a scratch directory. The path must
   contain `cst-scratch`, and the fixture refuses any directory that isn't new
   or empty.
2. points `HOME`, `USERPROFILE`, `APPDATA` and `CLAUDE_SESSIONS_ROOT` at that
   directory, then asks the tool where it will read and write. It stops unless
   every path is inside the scratch directory, so it cannot reach a real
   `~/.claude` or `%APPDATA%\Claude`.
3. runs the README's demo flow (`partitions`, `copy --from work`,
   `copy --from work --apply`, `partitions`, `sessions -p personal`) with the
   repo's `claude_sessions.py`, and writes each command's exact stdout and
   stderr to `src/captures/`, with the scratch path shown as `<demo-home>`.

`src/captures.ts` imports those files verbatim. Each scene picks lines out of
them by content, for example "the line containing `═══▶`". If a lookup finds
nothing, it throws and the render fails. Animation changes only colour,
position and how much of a line has appeared so far. It never changes a
character. The captions, the end card and the stylised app window are
illustration, not tool output.

The fixture's timestamps are relative to when you run it, so regenerating
changes the dates on screen and nothing else.

## Regenerate, preview, render

```bash
cd demo/video
npm ci

python capture/capture.py          # optional: fresh captures (defaults to ~/cst-scratch/video-capture)
npm run studio                     # live preview in Remotion Studio
npm run render                     # -> ../../docs/teleporter-demo.mp4 (H.264, CRF 22)
npm run gif                        # -> ../../docs/teleporter-demo-preview.gif
```

Remotion downloads its own headless Chrome on first render. `npm run gif`
needs a full `ffmpeg` on `PATH`, because the ffmpeg bundled with Remotion lacks
`palettegen`. On Windows, run it from WSL:
`wsl --exec sh demo/video/make-gif.sh`.

## Layout

| Path | What |
| --- | --- |
| `capture/` | fixture builder and capture script |
| `src/captures/` | captured tool output (generated, committed) |
| `src/captures.ts` | loads the captures and finds the lines each scene uses |
| `src/scenes/` | the five scenes: the pain, `partitions`, the dry run, `--apply`, the end card |
| `src/components/` | terminal, stylised app window, captions, background |

Fonts: JetBrains Mono NL (the full build, because the Google Fonts subsets lack
the box-drawing and symbol glyphs the tool prints) and Inter. Both are OFL and
installed from npm.
