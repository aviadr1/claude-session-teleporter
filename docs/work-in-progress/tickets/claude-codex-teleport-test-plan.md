# Claude ↔ Codex teleport validation

- [x] CLI journeys in both directions: dry run, apply, reload, stable repeat; source bytes unchanged.
- [x] Preserve ordered conversation text and tool evidence; never emit executable historical tools or copy permissions/instructions.
- [x] Claude branch selection; Codex compaction replacement context; malformed/unsupported history fails before writes.
- [x] Desktop: Codex display events and index discovery; Claude destination metadata, org connectors, tombstones, native/WSL placement.
- [x] Failure safety: existing paths, archived Codex ID, partial write rollback, invalid cwd and incomplete source.
- [x] Real binaries in temporary homes: Codex list/read/resume after restart and loopback request capture; Claude resume and loopback request capture. No credentials or remote model requests.
- [x] Existing tests, format drift checks and standalone packaging.

Results: Linux 106 passed (including two real-client probes), 2 skipped;
Windows 104 passed, 4 skipped; Python 3.10 104 passed, 4 skipped. All exclude
7 live-store drift checks. The separate Windows-to-WSL publication run passed
alongside the feature suite (31 passed, 1 non-applicable direction skipped).
The full Linux suite has one existing sdk-cli drift failure, reproduced on the
original commit; see docs/teleport-investigation.md. Graphical desktop open/send
is not claimed by the backend/metadata checks above. Wheel/sdist builds pass.
