# Invariants

This tool writes into the private, undocumented, reverse-engineered state of an
application the user depends on. That earns a higher bar than "the tests pass":
every rule this file states is enforced by a test in `tests/`, and the test name
is listed next to it.

Three kinds of rule live here.

**Safety invariants** are promises about damage. They are what makes it
reasonable to point this tool at a real session store. If one of them breaks,
someone loses work, so they are tested by asserting the *absence* of an effect -
bytes unchanged, files still present, directory contents identical.

**Format invariants** are claims about how Claude Code stores things. They were
learned by reading real session files, not from documentation, so they can be
falsified by any Anthropic release. They are tested against fixtures captured
from real data, and `test_format_drift.py` re-checks them against the live store
when one is present, so drift surfaces as a failing test rather than a corrupt
session.

**Packaging invariants** protect the install story. The tool is a single
standard-library file on purpose; the packaging exists to make that convenient,
never to make it untrue.

---

## Safety

### S1 - No write command writes anything without `--apply`

`copy`, `adopt` and `eject` are dry runs by default. A dry run prints its plan
and touches nothing: no metadata, no transcript, no ledger.

> `test_dry_run_writes_nothing[copy]`, `[adopt]`, `[eject]`

### S2 - An existing session is never overwritten

All metadata writes use exclusive create (`open(..., "x")`). There is
deliberately no `--force`. If a session with the same id appears in the
destination between the plan and the write, it is left alone and reported.

> `test_never_overwrites_existing_session`, `test_exclusive_create_is_used`

### S3 - A deleted session is never resurrected

A `deleted_<id>` tombstone in the destination means the user deleted that
session there. Both `copy` and `adopt` skip it.

> `test_tombstone_blocks_copy`, `test_tombstone_blocks_adopt`

### S4 - The source is never modified

Nothing is written, moved, renamed or deleted in the source partition or on the
source host. Teleporting is always a copy.

> `test_source_partition_untouched`, `test_adopt_never_writes_into_wsl`

### S5 - `copy` never touches transcripts

Partitions share one transcript pool, so moving a session between orgs is a
metadata-only operation. Conversations are never copied or duplicated.

> `test_copy_does_not_touch_transcripts`

### S6 - `copy` refuses a session whose transcript is missing

There is nothing to continue, so importing it would only produce a broken entry.

> `test_copy_skips_session_without_transcript`

### S7 - Cross-account copies require `--allow-cross-account`

Crossing orgs is routine; crossing accounts is not, and is usually a mistake.

> `test_cross_account_refused_by_default`, `test_cross_account_allowed_with_flag`

### S8 - `adopt` never writes into the distro

Adopting is metadata-only on the Windows side. The transcript stays in WSL,
unread except to describe it, and un-rewritten.

> `test_adopt_never_writes_into_wsl`

### S9 - `adopt` is idempotent

The desktop uuid is derived (uuid5) from the host name and the WSL session id,
so adopting twice cannot produce two entries for one conversation.

> `test_adopt_uuid_is_deterministic`, `test_adopt_twice_is_a_noop`

### S10 - An adopted session never inherits a permission bypass

`adopt` clones a real session from the destination org to inherit its
connectors. It resets `permissionMode` to `auto` regardless of what the donor
was set to. A session this tool created must not arrive pre-authorised to skip
tool approval.

> `test_adopt_resets_permission_mode`

### S11 - `eject` is the only command that forks, and it says so

`eject` writes a second transcript; the Windows session keeps its own. It uses
exclusive create too, and refuses a destination that already exists rather than
merging into it.

> `test_eject_writes_second_transcript`, `test_eject_refuses_existing_target`

### S12 - `eject` refuses a working directory WSL cannot reach

Only drive paths have a spelling inside the distro (`C:\x` -> `/mnt/c/x`). A UNC
path or anything else is refused rather than guessed at.

> `test_eject_refuses_unmappable_cwd`

### S13 - An ambiguous selector is an error, never a guess

Partition, host and session selectors all refuse to pick for you.

> `test_ambiguous_selectors_die`, `test_unknown_selectors_die`

---

## Format

### F1 - Project directory encoding is "every non-alphanumeric becomes a dash"

    C:\Users\a\GitHub\repo        -> C--Users-a-GitHub-repo
    /home/a/projects/repo         -> -home-a-projects-repo
    ...\repo\.claude\worktrees\wt -> ...-repo--claude-worktrees-wt

> `test_encode_cwd`, `test_encode_cwd_matches_real_store`

### F2 - A transcript is located by the directory it is in, never by re-encoding a cwd

This is the subtle one. A session can move - into a git worktree, or into a
different repo entirely - and Claude Code **re-homes the transcript** when it
does, so the directory names where the session *ended up*. Records written
before the move still carry the old `cwd`, so re-encoding any `cwd` read out of
a transcript can produce a directory that does not exist.

Anything that needs a transcript's path must use `CliSession.project_dir`, taken
verbatim from the filesystem.

> `test_moved_session_locates_by_directory`, `test_adopt_path_exists_for_moved_session`

### F3 - `cwd` is where a session ended, `originCwd` is where it began

The reconstruction mirrors the split the app itself makes. `cwd` is the last
recorded directory that agrees with the project directory; `originCwd` is the
first one seen. A session that visits a subdirectory and returns has not moved.

> `test_cwd_and_origin_cwd`, `test_round_trip_is_not_a_move`

### F4 - WSL and Windows path mappings round-trip

    C:\Users\me\x  <-> /mnt/c/Users/me/x          same files, over drvfs
    /home/me/x      -> \\wsl.localhost\D\home\me\x  same files, over 9P

> `test_path_mapping_round_trips`, `test_win_to_wsl_rejects_non_drive_paths`

### F5 - Connector uuids are org-scoped; tool keys must never dangle

`remoteMcpServersConfig` gives the same connector a different uuid in each org,
and `enabledMcpTools` is keyed `"<serverUuid>:<toolName>"`. After any port,
every tool key points at a connector present in the destination. Keys naming a
connector the destination lacks are dropped, not carried.

> `test_connector_uuids_remapped_by_name`, `test_no_dangling_tool_keys_after_port`,
> `test_dropped_connector_drops_its_tool_keys`

### F6 - Tool keys can name a connector the session itself no longer carries

The app does not prune `enabledMcpTools` when connectors change, so a session's
own `remoteMcpServersConfig` is not a sufficient source of names. Repair works
against every connector uuid known on the machine.

> `test_stale_tool_key_repaired_from_global_map`

### F7 - `entrypoint` distinguishes a CLI session from an app session

Two different things leave transcripts in the same WSL directory: a session
started by running `claude` at a terminal inside the distro (`entrypoint: cli`),
and a session started in the Windows app that merely uses the distro as its
environment (`entrypoint: claude-desktop`). Only the former is a candidate for
`adopt`.

> `test_origin_from_entrypoint`, `test_only_cli_sessions_are_adoptable`

### F8 - WSL sessions are not "remote"

WSL reuses the ssh fields (`sshRemoteTranscriptPath`), so a naive reading calls
every WSL session remote. `wslConfig` distinguishes them, and they are reported
as `W`, not `R`.

> `test_wsl_session_is_not_remote`

### F9 - A WSL session's transcript resolves before the app mirrors it

The app mirrors a remote transcript to `~/.claude/projects/ssh-<cliSessionId>/`
only once the session has been opened. A freshly adopted session has no mirror,
and must still resolve through `sshRemoteTranscriptPath` - otherwise `copy`
would refuse to move it between orgs under S6.

> `test_wsl_transcript_resolves_without_mirror`

---

---

## Packaging

`pyproject.toml` exists so the test dependency has somewhere to live and so a
release can be built. It must not quietly change what the tool *is*: a single
standard-library file you can `curl` onto a machine and run.

Until there was a pyproject, nothing could break that by accident. Now
something can, so these are pinned too.

### P1 - The tool imports only the standard library

No third-party import, and no sibling module either - a second file would break
the copy-one-file install just as thoroughly as a dependency would. Checked by
parsing the source, and end-to-end by running the file alone in a temp
directory with nothing installed.

> `test_tool_imports_only_the_standard_library`, `test_tool_is_a_single_file`,
> `test_tool_runs_from_a_bare_interpreter`, plus the `bare-interpreter` CI job

### P2 - The published package declares no runtime dependencies

`dependencies = []`, no optional extras, and the built wheel carries no
`Requires-Dist`. `pytest` lives in a dependency-group, which is not installed
for users.

> `test_no_runtime_dependencies`, `test_test_dependencies_are_a_dev_group_not_runtime`,
> plus the `build` CI job asserting it against the actual wheel

### P3 - One version, in two places, kept equal

Users see `__version__`; a release publishes the `pyproject.toml` version. Drift
means bug reports quoting a version that was never released. The publish
workflow additionally refuses to run when the release tag disagrees with either.

> `test_version_matches_the_module`, and the version-check step in
> `python-publish.yml`

### P4 - The console script resolves, and the wheel is one module

`claude-sessions = claude_sessions:main` has to name something callable, and the
wheel must contain that module and nothing else.

> `test_console_script_entry_point_resolves`, `test_wheel_ships_the_tool_and_nothing_else`

---

## Testing against drift

`tests/test_format_drift.py` is the early-warning system. When a real Claude
Code store is present on the machine, it re-checks F1, F2, F3, F7 and F9 against
every transcript and session file actually on disk. It skips cleanly when there
is no store, so CI stays green on a bare runner.

If Anthropic changes a format, that file fails first - before anyone points the
tool at their sessions.
