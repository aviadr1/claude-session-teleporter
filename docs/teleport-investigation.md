# Claude ↔ Codex compatibility investigation

Investigated 2026-09-28 with **Codex CLI 0.151.0** and **Claude Code 2.1.238**.
The implementation remains one standard-library Python file. No personal
session was imported, changed, or submitted to a model during development.

The subsequent [red-team pass](teleport-redteam.md) found and corrected five
issues, including a provider-filtering blind spot in the initial probe. It also
added successful client continuation and persistence checks. The experiments
below describe the initial investigation. The later [native-tool and round-trip
correction](teleport-native-tools.md) replaces the initial text-only tool conversion.

## What was probed

1. Inspected the original teleporter, its safety invariants, the installed CLI
   help and Codex's generated experimental JSON schemas.
2. Read record shapes from local Claude and Codex transcripts without printing
   their conversation text. Sampled the 30 most recently modified files in each
   local store. One Claude file contained no main-chain conversation. A Codex
   fork contained both its own header and inherited parent metadata.
3. Created synthetic histories in temporary client homes. Probed Codex
   `thread/list`, `thread/read`, `thread/resume`, and outgoing Responses requests;
   probed Claude `--resume` and outgoing Messages requests. The API endpoints
   were loopback HTTP servers with dummy credentials, returning a deliberate
   error after capturing the request. These prove history loading and request
   composition, not the quality of a subsequent model answer.
4. Repeated Codex discovery/read after restarting its app server. A fresh
   server's `useStateDbOnly` listing also discovered the imported file through
   normal backfill. The tool does not edit Codex's SQLite database.

The reproducible checks are in `tests/test_client_probes.py`:

```bash
RUN_CLIENT_PROBES=1 uv run pytest tests/test_client_probes.py -v
```

The tests require the installed binaries and run entirely against synthetic
histories, temporary homes and a local request recorder. Normal CI skips them.

## Approaches and outcomes

| Approach | Evidence | Decision |
| --- | --- | --- |
| Copy Claude desktop metadata to Codex | The original metadata describes Claude-specific partitions, connectors and transcript paths; Codex uses a different rollout format. | Cannot represent a cross-client session. |
| Write only Codex `response_item` messages | Codex resumed the rollout, but `thread/read` returned no turns and `thread/list` omitted it. | Insufficient for desktop support. |
| Codex `thread/start` + `thread/inject_items` | The installed server accepted items, but returned no visible turns; the created thread was absent from the tested list. | Useful for model context; insufficient for this desktop import without additional work. |
| Native messages plus matching Codex display events | Discovery, displayed user/assistant turns, restart/resume and outgoing history all passed. | Implemented. |
| Native Claude linked messages | Claude resumed a synthetic transcript and sent both imported roles/texts to the loopback endpoint. | Implemented. |
| One large transcript prompt | Considered as a fallback, not run as a separate experiment. Loses native turn presentation and makes every historical role plain quoted context. | Not the primary import format. |

[OpenAI's app-server documentation](https://learn.chatgpt.com/docs/app-server)
describes reading/listing/resuming threads and injecting model-visible items.
The installed protocol explicitly labels `thread/resume.history` as unstable
and for Codex Cloud, so it was not used. Native rollout details remain a private
format contract checked by the optional real-client tests.
[The CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
documents resuming by session ID.

## Conversion contract

- A teleport creates an independent fork; the original remains unchanged.
- Retain active user and assistant text in order. Follow Claude's latest main
  parent chain and results explicitly linked to its parallel tool calls, without
  mixing abandoned conversation branches or subagents. For Codex compaction,
  use the latest `replacement_history` and subsequent response items.
- Preserve completed tools as native calls and results, with original names and
  IDs. Conversion metadata restores format-specific distinctions on return.
  No historical tool is registered or rerun; pending and ambiguous pairs fail
  before writing. See [the native-tool contract](teleport-native-tools.md).
- Omit private reasoning, encrypted reasoning, source system/developer
  instructions and runtime permissions. Unsupported media/items receive visible
  placeholders and a count in the conversion plan. No silent truncation.
- A summary after compaction can replace older turns. This transfers resumable
  context, not a complete archive of every pre-compaction event. Retain the
  source if the full original timeline matters.
- Fail before writing on broken JSON, missing Claude parents, cycles, Codex
  rollback events, compactions without replacement context, or unavailable cwd.
- Never overwrite, update, or append to an existing imported session. Its
  deterministic ID stays stable if the source later grows. Archive detection
  prevents reintroducing an archived Codex import; Claude desktop tombstones
  prevent reintroducing a deleted one. No background synchronization is implied.
- Stage complete files and publish using exclusive hard links on POSIX, or
  Windows rename (which refuses existing destinations). On publication
  failure, remove only this operation's unchanged published files; preserve detected
  changes by other consumers. Unsupported filesystem
  operations fail rather than fall back to overwriting.

## Desktop and host boundaries

Codex's backend discovery, displayed history and request composition were
verified. Actual graphical sidebar refresh was **not** automated. Use the
Codex home belonging to the intended desktop host, then restart the app; if
needed, resume the printed ID once with that host's CLI.

Claude desktop imports reuse the existing destination-partition connector
mapping and metadata builder. Native and WSL placement, permission reset,
tombstones, and transaction failure are covered by filesystem tests. The real
Claude CLI loads the resulting transcript shape. A graphical Claude desktop
open/send was **not** verified on this machine, which has no populated Claude
desktop partition at the expected store location.

A Windows app's `.codex` and a WSL CLI's `.codex` are distinct. `--target-home`
selects a store; `--cwd` explicitly selects the destination's path spelling.
For Claude WSL desktop metadata, `--target-host wsl:Ubuntu` uses the existing
host-discovery/path-mapping implementation. Project files, worktrees, git
changes, credentials, connector authorization and running processes do not
teleport. The destination working tree must already exist.

## Existing drift finding

The repository's existing `test_entrypoint_values_are_known` detects an
unrecognized Claude `sdk-cli` entrypoint in this machine's older local history.
It fails identically on an untouched checkout of the original commit. This
change does not reinterpret SDK sessions as human CLI sessions or weaken that
existing drift guard. The isolated feature/packaging suites and real-client
probes are independent of that baseline failure.


## Windows filesystem probes

Native Windows Python reproduced failures on long output paths; extended-path
handling fixes them. A separate real Windows-to-WSL probe reproduced WinError 50
for hard-link publication through `\\wsl.localhost\Ubuntu`. Windows exclusive
rename handles that share while retaining the no-overwrite invariant. The
opt-in `test_real_windows_to_wsl_publication` creates only a temporary synthetic
file; set `RUN_CLIENT_PROBES=1` and `TELEPORT_WSL_PROBE_ROOT` to a reachable WSL
temporary directory in the Windows process environment to repeat it.

## Codex desktop project membership

The desktop-bundled **0.158.0-alpha.2.1** protocol adds `project/list`,
`project/create`, and `thread/metadata/update.projectId`. Its generated schema
and synthetic real-server probes establish that cwd alone does not assign a
saved desktop project. `--codex-project auto` now resolves exact destination
roots or creates a project, registers the destination provider through native
resume, and verifies membership through `thread/read`. No model turn is sent.
Project registration is now the default and requires those APIs; explicit
`--codex-project none` imports only the transcript without a Codex executable. `codex-project` repairs placement independently.

Unlike the original 0.151 probe, this newer server can index a missing provider
as an empty string. A filesystem scan discovers the rollout but a provider-filtered
DB-only list omits it until native resume supplies the effective destination
provider. The project probes verify that list after restart, including a custom
provider, and preserve active conversation items through repair.

Run the expanded probes with `RUN_CLIENT_PROBES=1` and
`CODEX_PROJECT_TEST_BIN=/absolute/path/to/desktop-bundled/codex`.
They use temporary homes, real project APIs and synthetic transcripts. No GUI
sidebar assertion is made: persisted project membership and backend discovery
are verified, while the app can still cache its sidebar. The operator's previously
imported example had been archived before repair; it was left archived.

A subsequent check of the running desktop **26.924.2738.0** found a further UI
boundary: a project created through its backend was present in `project/list`
but absent from the app tool's `list_projects`. The installed app maintains a
separate local project cache and gates thread-assignment synchronization; its
migration checkpoint was unfinished. Therefore this feature guarantees native
backend membership, not automatic sidebar grouping on that desktop build.
Refreshing alone is not a verified remedy. The tool does not rewrite live global
settings to bypass the app's project handling.


## Implementation boundaries

The distributable remains one standard-library Python file. Cross-client code
has separate layers:

- `decode_claude_session` / `decode_codex_session` understand each source format;
  `PortableSession` and its validators define the shared conversion contract.
- `encode_claude_session` / `encode_codex_session` own native destination rows.
  Tool argument projection is shared without calling a Claude encoder from Codex.
- `plan_teleport` builds a `TeleportPlan` without writes or client startup.
  `plan_claude_desktop_metadata` owns Claude's partition/connector integration.
- `_publish_teleport` owns filesystem transactions. `CodexProjectClient` and
  `assign_codex_project` own Codex RPC and membership. `apply_teleport` coordinates
  those effects; CLI functions handle presentation and error reporting.

Existing CLI, round-trip, publication-failure, desktop, and real-client tests
exercise these boundaries. The default project tests omit the project option;
transcript-only test journeys explicitly opt out so CI remains independent of
installed client executables.
