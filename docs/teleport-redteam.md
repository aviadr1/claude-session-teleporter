# Claude ↔ Codex first red-team pass

This is the historical evidence for the first pass. Its text-only tool policy
is superseded by the [native-tool and round-trip correction](teleport-native-tools.md);
that report records the current behavior and verification.

Baseline: `268eea0` (PR #8), 2026-09-28. Scope: native history conversion,
Codex discovery, continuation through real clients, and destination write
safety. Reused the existing filesystem fixtures and real-client/loopback API
harness. No personal conversations or credentials were submitted to a model.

## Confirmed defects and corrections

| Priority | Reproducer and impact before the fix | Correction and regression evidence |
| --- | --- | --- |
| P1 | Publish a transcript, replace or edit it from another consumer, then fail the metadata write. Cleanup unconditionally unlinked the consumer's new work. | Compare the published file's device, inode, size and modification time before cleanup; preserve detected replacements/edits and tolerate files already removed. `test_failed_desktop_publication_preserves_replaced_transcript`, `test_cleanup_preserves_a_consumers_changes`. |
| P1 | Put an instruction in a Claude/Codex tool result, then convert. Its text became a user-role message, upgrading untrusted evidence to user authority. | Render historical tool evidence as labeled assistant text. Split mixed Claude user/text/tool-result blocks without changing their order. `test_tool_output_never_becomes_a_user_instruction`, `test_mixed_claude_content_keeps_user_text_separate_from_tool_output`, and both real-client successful-continuation probes. |
| P1 | Configure Codex with a custom provider, import, and call ordinary `thread/list`. The import was missing. The original probe hid this by requesting every provider. | Omit the source-independent `model_provider` field so Codex resolves the destination configuration. The real probe now uses default listing filters, including after restart. |
| P2 | Request Claude desktop registration when only the transcript exists, as can happen after an interrupted import. The command returned success without creating the missing metadata. | Detect partial destination state and exit with an explicit error while preserving all existing files. `test_retry_of_partial_desktop_import_is_not_reported_successful`. Recovery/registration of an existing CLI import is not silently attempted. |
| P2 | Import assistant-only history. Codex could read it by ID but omitted it even from an all-provider list. | Add a clearly labeled synthetic import-context preface when history starts with an assistant. Real listing/resume/request-capture tests cover both ordinary and assistant-first histories. |

Every fix was preceded by an observed failing regression or real-client
reproducer. The rollback tests use real files with a failure injected at the
publication boundary; they do not simulate the transaction logic.

## Stronger checks on the harness and fixes

The original real-client harness returned an intentional API error after
capturing a request. That established loading and request composition but did
not establish a successful continuation or its persistence. The harness now
also serves minimal successful Messages/Responses event streams from loopback.
The real clients consume those streams and perform all session persistence.

- Codex: ordinary provider-filtered discovery, displayed turns, restart, resume,
  a completed synthetic turn, another restart, and a subsequent model request.
- Claude: resume, complete a synthetic turn, exit, resume again, and inspect the
  subsequent model request. Historical tool text remains assistant-role content.
- Both: original user/assistant text and the new synthetic answer survive;
  imported evidence creates no foreign executable tool calls.
- Cleanup: both replacement and in-place edits survive a later publication
  failure; a consumer's deletion remains deleted. The unchanged-file rollback
  regression continues to pass.
- Existing native Windows and Windows-to-WSL publication checks remain part of
  validation; the tests still exercise the production publication helper.

Run locally with installed client binaries:

```bash
RUN_CLIENT_PROBES=1 uv run pytest -q -m 'not drift'
```

Client versions exercised: Codex CLI 0.151.0 and Claude Code 2.1.238. Synthetic
API responses prove client loading/persistence, not the quality of a real
model's reasoning or acceptance by a production model service.

## Remaining boundaries

The app-server history/list API and both native CLI workflows are exercised.
Actual graphical desktop open/send is still unverified. Claude desktop
metadata is covered by the destination-store fixtures, not a running desktop
app. Keep the PR draft pending that check.

Private formats can change. The existing local `sdk-cli` drift failure is still
present on the untouched base commit and is not suppressed. The rollback
ownership check preserves detected concurrent changes, but is not a lock on
external clients: imports should finish before the destination is opened.
Abrupt process termination can leave partial files; rerunning now reports that
state instead of falsely reporting success. This is not an ACID transaction
across the client stores, and the tool does not perform automatic recovery.


## Frozen-candidate verification

Implementation commit `9f7cea9` passed 119 Linux tests including five real-client
probes, with 2 skips. Native Windows and Python 3.10 each passed 114 tests with
7 skips. Those runs deselected the 7 existing live-store drift checks described
above. The separate actual Windows-to-WSL publication probe passed. All nine
[CI checks](https://github.com/aviadr1/claude-session-teleporter/actions/runs/36420111947)
passed on that implementation: the Linux/Windows/macOS matrix, bare interpreter
and package build. The following documentation update changes no runtime or
test code.
