"""
Capture the real terminal output the promo video shows.

Builds a fresh fake Claude Code store with docs/demo_fixture.py (the same
demo store behind docs/demo.gif) under a scratch directory, points every location the tool reads (home, APPDATA, the session
store override) at that directory, checks the tool agrees before it runs a
single command, then runs the README demo flow and writes each command's exact
stdout+stderr to ../src/captures/.

It never touches a real store: the scratch path must contain "cst-scratch",
the fixture refuses any directory that isn't new or empty, and the resolved
paths are checked against it before anything runs. The scratch path itself is
replaced with <demo-home> in the captures, so they don't embed a machine's
home directory; the video never shows those lines.

    python demo/video/capture/capture.py [SCRATCH_DIR]

SCRATCH_DIR defaults to ~/cst-scratch/video-capture and is wiped first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TOOL = REPO / "claude_sessions.py"
OUT = HERE.parent / "src" / "captures"

# (file stem, argv) - the README's "What it looks like" flow, in order.
STEPS: list[tuple[str, list[str]]] = [
    ("01-partitions", ["partitions"]),
    ("02-copy-dry-run", ["copy", "--from", "work"]),
    ("03-copy-apply", ["copy", "--from", "work", "--apply"]),
    ("04-partitions-after", ["partitions"]),
    ("05-sessions-personal", ["sessions", "-p", "personal"]),
]


def scratch_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    appdata = home / "AppData" / "Roaming"
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),  # what Path.home() reads on Windows
        APPDATA=str(appdata),
        CLAUDE_SESSIONS_ROOT=str(appdata / "Claude" / "claude-code-sessions"),
        PYTHONIOENCODING="utf-8",  # a UTF-8 terminal, so the box glyphs survive a pipe
        PYTHONUTF8="1",
    )
    return env


def check_paths(env: dict[str, str], home: Path) -> None:
    """Ask the tool itself where it will read and write; refuse anything outside home."""
    probe = (
        "import sys, json; sys.path.insert(0, sys.argv[1]); import claude_sessions as c; "
        "print(json.dumps([str(c.sessions_root()), str(c.PROJECTS_DIR), str(c.CLAUDE_JSON), "
        "str(c.LABELS_PATH), str(c.LEDGER_PATH), str(c.SKILL_DIR)]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, str(REPO)], env=env, check=True, capture_output=True, text=True
    ).stdout
    for p in json.loads(out):
        resolved = Path(p).resolve()
        assert resolved.is_relative_to(home.resolve()), f"tool would touch {resolved}, outside {home}"
        assert "cst-scratch" in str(resolved), resolved
    print("paths ok: every location the tool uses is under", home)


def normalize(text: str, home: Path) -> str:
    """Unix newlines, and the scratch path shown as <demo-home>."""
    text = text.replace("\r\n", "\n")
    for form in {str(home), home.as_posix()}:
        text = text.replace(form, "<demo-home>")
    return text


def main() -> int:
    home = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "cst-scratch" / "video-capture")
    home = home.resolve()
    assert "cst-scratch" in str(home), f"refusing a scratch dir without cst-scratch in it: {home}"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)

    subprocess.run([sys.executable, str(REPO / "docs" / "demo_fixture.py"), str(home)], check=True)
    env = scratch_env(home)
    check_paths(env, home)

    OUT.mkdir(parents=True, exist_ok=True)
    transcript: list[str] = []
    for stem, argv in STEPS:
        proc = subprocess.run(
            [sys.executable, str(TOOL), *argv],
            env=env,
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0, f"{argv} exited {proc.returncode}:\n{proc.stdout}"
        text = normalize(proc.stdout, home)
        (OUT / f"{stem}.txt").write_text(text, encoding="utf-8", newline="\n")
        transcript.append(f"$ claude-sessions {' '.join(argv)}\n{text}")
        print(f"captured {stem}.txt ({len(text.splitlines())} lines)")

    (OUT / "transcript.txt").write_text("".join(transcript), encoding="utf-8", newline="\n")
    (OUT / "commands.json").write_text(
        json.dumps({stem: "claude-sessions " + " ".join(argv) for stem, argv in STEPS}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
