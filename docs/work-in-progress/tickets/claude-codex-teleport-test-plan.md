# Claude ↔ Codex teleport validation

- [x] CLI journeys in both directions: dry run, apply, reload, stable repeat; source bytes unchanged.
- [x] Preserve ordered conversation text and native completed tool exchanges; never replay historical tools or copy permissions/instructions.
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


## Red-team follow-up

- [x] Cleanup preserves replaced/edited/deleted destination files after failure.
- [x] Mixed tool/user blocks preserve order without promoting tool output to user authority.
- [x] Default Codex discovery respects the configured provider and assistant-first imports.
- [x] Partial desktop imports fail explicitly instead of claiming success.
- [x] Both real clients complete synthetic turns and retain original/new history across restart.
- [x] Final red-team candidate: full Linux, native Windows, Python 3.10, Windows-to-WSL, and CI checks.

Detailed reproducers and verification boundaries: docs/teleport-redteam.md.

Frozen implementation `9f7cea9`: Linux 119 passed (5 real-client probes),
2 skipped; native Windows and Python 3.10 each 114 passed, 7 skipped;
7 live-store drift checks deselected in each run. The separate actual
Windows-to-WSL publication probe passed. All 9 CI checks passed in run
https://github.com/aviadr1/claude-session-teleporter/actions/runs/36420111947.

## Native tools and round-trip correction

- [x] CLI round trips in both directions, repeated three times: preserve native call/result pairs, argument JSON, custom inputs, error flags, phases, and ordered content.
- [x] Reject orphan, pending, duplicate call/result histories before publication.
- [x] Real Codex backend: native tool cards, native model request items, completed turn and restart, then return conversion.
- [x] Real Claude: native tool_use/tool_result request blocks, completed turn and restart, then return conversion.
- [x] Stale conversion metadata cannot restore obsolete content after a native record changes.
- [x] Keep synthetic assistant-first prefaces from accumulating across round trips.

- [x] Replay Claude parallel-result sibling layout through the CLI and real Codex continuation.
- [x] Reject malformed identifiers and verify native history remains usable without conversion metadata.

Frozen native-tool implementation `f081c23`: Linux 141 passed (five real-client cases), Python 3.10 and native Windows 136 passed each; Windows-to-WSL probe passed. See [the current evidence and limits](../../teleport-native-tools.md).

- [x] Real Claude interruption probe found discarded late tool output; two fail-first CLI regressions now reject nonadjacent result groups before publication.

Final interruption correction `57458ed`: Linux 143 passed, Python 3.10 and native Windows 138 passed each; package build passed. The Windows-to-WSL publication path is unchanged from the verified `f081c23` implementation.


## Ambient browser context correction

- [x] CLI import displays only the actual user request; two round trips restore the exact Codex model-context text and keep display events clean.
- [x] Negative controls preserve quoted wrappers, ordinary headings, unknown/malformed wrappers, and native Claude text.
- [x] Installed Claude loader sends a clean user prompt to the loopback Messages endpoint.
- [x] Linux: 151 passed, 2 skipped with real-client probes; Python 3.10 and native Windows: 146 passed, 7 skipped each. The seven independent live-store drift checks remain excluded as previously documented.

## Destination project preservation (T8)

- [x] Public CLI dry run does not launch Codex or create destination state.
- [x] Exact/secondary roots, explicit IDs, duplicate names and ambiguous roots.
- [x] Real app-server import creates/reuses one project and remains discoverable
  in provider-filtered DB-only lists after restart (default and custom provider).
- [x] A different target home does not inherit the caller's SQLite override.
- [x] Repair and repeated import preserve conversation; conflicting existing
  membership is refused; archive and foreign-home inputs are rejected.
- [x] Inject a failed native metadata endpoint after publication, retain the
  transcript, then repair through the public CLI without duplicate projects.

## Default placement and client boundaries

- [x] Default dry run reports automatic project placement without starting Codex.
- [x] Missing Codex fails before publication by default; explicit `none` imports
  the transcript without launching Codex. These regressions failed before the fix.
- [x] Real create/reuse project probes omit `--codex-project` to exercise the default.
- [x] Existing native-loader, round-trip, Claude desktop and publication tests
  pass after separating client encoders, import planning, execution and output.
