# Records docs/demo.gif with https://github.com/charmbracelet/vhs.
# Run from the repo root on Linux or WSL:  vhs docs/demo.tape
#
# Everything shown is real output from the tool, run against the demo store
# built by docs/demo_fixture.py (never your own sessions). tests/test_demo.py
# replays the same commands so the GIF can't drift from the tool.

Output docs/demo.gif

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 15
Set LineHeight 1.15
Set Width 1480
Set Height 880
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set CursorBlink false

Hide
Type "rm -rf /tmp/teleporter-demo && python3 docs/demo_fixture.py /tmp/teleporter-demo/home >/dev/null"
Enter
Type "python3 -m venv /tmp/teleporter-demo/venv && /tmp/teleporter-demo/venv/bin/pip install -q . >/dev/null 2>&1"
Enter
Sleep 20s
Type "export HOME=/tmp/teleporter-demo/home APPDATA=/tmp/teleporter-demo/home/AppData/Roaming PATH=/tmp/teleporter-demo/venv/bin:$PATH PS1='$ ' && cd /tmp && clear"
Enter
Sleep 1s
Show

Type "# Signed into 'personal'. The 'work' sessions vanished from the app."
Sleep 800ms
Enter
Type "claude-sessions partitions"
Sleep 500ms
Enter
Sleep 5s

Hide
Type "clear"
Enter
Show
Type "claude-sessions copy --from work     # dry run: shows the plan, writes nothing"
Sleep 500ms
Enter
Sleep 9s

Hide
Type "clear"
Enter
Show
Type "claude-sessions copy --from work --apply"
Sleep 500ms
Enter
Sleep 4s
Type "claude-sessions sessions -p personal"
Sleep 500ms
Enter
Sleep 6s
