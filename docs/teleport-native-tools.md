# Native tool translation and round trips

This round supersedes the text-only conversion in the first investigation and
red-team pass. Flattening tool calls/results was an unnecessarily lossy design.
Changing their role to assistant text avoided one authority error but still
removed the native structure both clients support.

## Contract

The converter preserves supported ordered user/assistant text and completed
function/custom tool exchanges. Calls retain their original names, IDs and
arguments; results retain their matching IDs and output. Claude receives
`tool_use` and `tool_result` blocks. Codex receives function/custom call and
output response items, plus completed generic tool cards under the display
namespace `imported_history`. That namespace does not register or authorize
any tool. Historical Bash calls remain Bash calls; we do not pretend their
arguments are an executable Codex `exec_command` invocation.

Both Claude → Codex → Claude and Codex → Claude → Codex preserve the tested
native conversation content over three repeated cycles. This is semantic
preservation: IDs of the new session, timestamps of new work, message envelopes,
and grouping of adjacent text blocks can differ. Each teleport is still a new
independent fork, and repeating the same import still preserves the existing
destination rather than overwriting it.

Some distinctions need conversion metadata: custom raw tool input versus JSON
function input, JSON argument formatting, Claude error flags, text-array output
shapes, Codex namespaces and assistant phases. A versioned `teleporter` field
on each native row carries the shared portable representation. It contains no
source runtime configuration or private reasoning, and does not recursively
embed previous import envelopes. Clients can read the native history without
it. If another client rewrites or compacts away that metadata, return-trip
fidelity can decrease.

Restoration checks a SHA-256 digest of the native projection, validates the
portable fields, and reprojects them to check agreement. A changed native
record wins over stale metadata; contradictory metadata fails before writing.
The digest detects stale data, not authorship. Source transcripts remain
untrusted conversation data. Synthetic discovery prefaces are removed on read
and regenerated at most once, so they do not accumulate on every trip.

Private reasoning, source system/developer instructions, runtime permissions,
and media are still outside the portable contract. Media and unknown items
get visible placeholders. Compaction transfers surviving context. Pending,
orphaned, mismatched or duplicate tool exchanges fail before publication. Claude
imports also require each complete result group immediately after its call group.

## Findings and fail-first tests

1. **P1: tool structure was lost.** The first ten new CLI regressions all failed
   against the prior implementation: both round trips flattened tools, and
   incomplete/duplicate exchanges were accepted. One shared portable model now
   drives both readers, both native writers and native Codex display events.
2. **P1: Claude parallel results were omitted.** Real Claude transcripts link
   parallel results to their individual assistant call records. Some results
   are siblings, outside the final parent chain. A synthetic reproduction failed
   before the fix. The reader now includes pure result records explicitly linked
   to calls on the active chain, while excluding unrelated conversation branches.
3. **P2: metadata restoration needed stronger checks.** Tests caught invisible
   contradictory text/output and a stale Claude result being misclassified after
   a custom call. Restoration now validates native agreement, and Claude result
   kinds follow the surviving call. Edited native results are preserved.
4. **P2: malformed IDs raised Python type errors.** Three failing regressions
   used list-valued record/call/result IDs. The reader now rejects those cleanly
   before writing.

5. **P1: Claude discarded interleaved results after reporting import success.**
   After the first frozen candidate passed CI, another real CLI probe imported
   a completed call whose result came after a new user/assistant exchange. Claude
   exited successfully and sent one call and one result, but the original result
   content was absent: its loader had inserted a synthetic interruption result.
   Two new regressions (an intervening turn and a split parallel result group)
   failed before the fix. Claude projection now requires adjacent complete
   call/result groups and rejects an unsupported ordering before publication.

The previous assistant-text tests were revised to assert native tool authority.
Claude tool results live inside a user message **as `tool_result` blocks**;
that does not make them user text instructions. Codex uses dedicated output
items without a user role. Shared fixture builders now live in
`tests/teleport_support.py`, and one Claude invocation helper owns its complete
credential-free subprocess lifecycle. The original locator and portable
reader now share the cwd-selection rule.

## Meeting reality

Probed installed Codex CLI **0.151.0** and Claude Code **2.1.238**, using
isolated temporary homes and loopback model endpoints with synthetic replies.
Actual clients perform parsing, request construction, continuation and file
persistence; no personal conversation or credential is sent to a model.

- Codex preserves native function calls/results in outgoing requests. The
  tested dynamic-tool legacy display events did not appear; MCP display events
  did. The implementation therefore uses completed generic MCP tool cards for
  display and native function/custom items for model history. Tests assert both
  the successful and failed statuses after restarting the server.
- Claude sends imported `tool_use`/`tool_result` blocks in Messages requests.
  It completes a synthetic turn, exits, resumes, retains that answer and the
  native tool history, and teleports back with the original supported content.
- Codex completes a synthetic turn, restarts, retains its displayed tools and
  answer, completes another turn, and teleports back with the original content.
- Read-only structural sampling of the 30 most recent files in each local store
  found 16 Claude files whose parallel results the old parent-chain-only reader
  missed. After the fix all 24 files containing a main conversation parsed,
  preserving 7,414 tool items. Six had no main conversation. Codex parsed 29,
  preserving 2,480 tool items; one had a genuinely unfinished call. These are
  sample counts, not a guarantee about every file or future client version.

The model services are synthetic loopback boundaries. These tests prove real
client behavior and persistence, not production model-service acceptance or
answer quality. Actual graphical desktop open/send remains unverified; Claude
desktop placement/permissions use filesystem fixtures and Codex display uses
the real app-server API. The graphical check remains outstanding.

## Verification

Frozen implementation **`f081c23`**:

- Linux Python 3.14: **141 passed, 2 skipped**, including five real-client cases.
- Python 3.10: **136 passed, 7 skipped**.
- Native Windows: **136 passed, 7 skipped** using a short temporary test root.
- Separate actual Windows-to-WSL publication: **1 passed**, including refusal to overwrite.
- Package build passed; no duplicate top-level test names; no runtime dependencies added.

Those suite runs select `-m 'not drift'`. Running the seven live-store drift
checks separately produced **2 passed, 4 skipped, 1 failed**: the pre-existing
`test_entrypoint_values_are_known` still detects `sdk-cli`. The same failure
was established on untouched base `7d0bb7c` during the earlier investigation;
its guard and CLI/desktop classification have not been relaxed in this change.

```bash
RUN_CLIENT_PROBES=1 uv run --python 3.14 pytest -q -m 'not drift'
uv run --python 3.10 pytest -q -m 'not drift'
uv build
```

Native Windows uses `uv.exe run --no-project --with pytest --with tomli python
-m pytest` with the same selection. The opt-in WSL probe sets
`RUN_CLIENT_PROBES=1` and `TELEPORT_WSL_PROBE_ROOT` inside the Windows process;
WSL environment variables are not assumed to propagate to Windows.

The following commit records these results only; it changes no runtime or tests.
