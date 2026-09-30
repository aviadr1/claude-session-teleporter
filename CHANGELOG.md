# Changelog

## 1.3.0 — 2026-10-01

### Added

- Add `claude-sessions ui`, a local browser interface that discovers Claude
  Desktop, Claude Code, Codex, and reachable WSL sessions without a file picker.
- Filter sessions by one or more source agents, inspect a read-only conversation
  preview without losing selection, then transfer selected sessions together to
  one destination after reviewing a dry-run.
- Show last activity and project metadata in the session list, and include UI
  screenshots in the README.
- Discover Claude Desktop sessions installed through the Microsoft Store.

### Security

- Keep the UI on loopback, protect API calls with a one-use launch token, reject
  foreign origins, load no remote assets, and recheck source files before a
  confirmed batch transfer. Original sessions remain in place.

## 1.2.0 — 2026-09-28

### Added

- Teleport sessions between Claude and Codex with `teleport`, including native
  session resumption and optional Claude desktop registration.
- Preserve supported conversation content through Claude → Codex → Claude and
  Codex → Claude → Codex round trips. Completed tool calls and results retain
  their native roles, IDs, arguments, output and order; historical tools are not
  rerun. Checked conversion metadata retains format-specific distinctions.
- Assign Codex imports to an exact matching destination project, creating one
  when needed. `--codex-project auto` is the default; an explicit project ID or
  unique name selects a project, and `--codex-project none` opts out.
- Repair project membership for an existing active import with `codex-project`.
  `--codex-bin` and `--codex-sqlite-home` support desktop-specific installations.
- Add recorded terminal demos to the README.

### Fixed

- Keep recognized Codex ambient browser context outside imported Claude user
  prompts, while preserving it for a return trip to Codex.
- Reject incomplete or unsupported tool histories before publication, including
  interleaved results that Claude's loader discards.
- Preserve existing imports and archived sessions on repeat runs; report partial
  imports and recoverable project-registration failures without replacing history.

### Changed

- Separate Claude/Codex decoding and encoding, read-only import planning, file
  publication, desktop metadata, project registration and CLI presentation.
  Installation remains one Python file with no runtime dependencies.

### Compatibility and limitations

- Default Codex imports now require a Codex executable with native project APIs.
  Use `--codex-bin` to select a compatible desktop-bundled executable, or
  `--codex-project none` for transcript-only imports. Dry runs never start Codex.
- Backend project membership is verified. Some Codex desktop builds maintain a
  separate project registry, so automatic sidebar grouping remains unresolved on
  those builds even when backend membership is correct.
- Round-trip fidelity covers supported conversation content. Private reasoning
  and source system instructions are omitted; images and unsupported content use
  placeholders. Clients that discard conversion metadata can reduce fidelity.
  Credentials, permissions and running processes do not transfer.

## 1.1.0 — 2026-09-28

- First successful PyPI release, with the `claude-sessions` command.
- Discover Claude sessions, copy them across account/organization partitions,
  remap destination connectors, and adopt/eject sessions between WSL and Windows.
- Package the standard-library-only script with Python 3.10+ support and
  tag-triggered PyPI trusted publishing.
