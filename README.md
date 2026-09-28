# Claude Session Teleporter

[![tests](https://github.com/aviadr1/claude-session-teleporter/actions/workflows/tests.yml/badge.svg)](https://github.com/aviadr1/claude-session-teleporter/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-session-teleporter.svg)](https://pypi.org/project/claude-session-teleporter/)
[![license: MIT](https://img.shields.io/github/license/aviadr1/claude-session-teleporter)](https://github.com/aviadr1/claude-session-teleporter/blob/main/LICENSE)
[![python: 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://github.com/aviadr1/claude-session-teleporter/blob/main/pyproject.toml)
[![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)](https://github.com/aviadr1/claude-session-teleporter/blob/main/INVARIANTS.md#packaging)

> **Oh, you _can_ take it with you.**
> Out of quota, not out of context.

**Resume across Claude accounts, Windows/WSL, and now Claude ↔ Codex.**

Switch orgs or accounts in the Claude desktop app and your sessions vanish from
the list. Run `claude` inside WSL and the Windows app never shows those sessions
at all. Either way, every session is still on your disk. This tool finds them
and puts them back in the app, so you can pick up where you left off, even when
the org you were using has run out of quota.

![claude-sessions partitions finds a second org with 2% quota left, copy --from work plans the move and remaps its connectors, and --apply brings both sessions into the signed-in org](https://raw.githubusercontent.com/aviadr1/claude-session-teleporter/main/docs/demo.gif)

<sub>Real output against a demo store built by [`docs/demo_fixture.py`](https://github.com/aviadr1/claude-session-teleporter/blob/main/docs/demo_fixture.py), not your sessions. Re-record with `vhs docs/demo.tape`.</sub>

**The 30-second tour** (with sound):

https://github.com/user-attachments/assets/488d10d2-d116-4938-976c-ad175e78a814

## Quick start

With [uv](https://docs.astral.sh/uv/):

```bash
# every org on this machine, its sessions, and how much quota it has left (read-only)
uvx --from claude-session-teleporter claude-sessions partitions

# install it, then copy the other org's sessions into the one you are signed into
uv tool install claude-session-teleporter
claude-sessions copy            # dry run: prints the plan, writes nothing
claude-sessions copy --apply    # do it
```

With more than two orgs, `copy` asks you to pick one with `--from`, using a name
or UUID prefix from the `partitions` list. After `--apply`, **switch accounts in
the app** (or restart it). The app caches its session list and only re-reads
the disk when you do.

No uv? The tool is [one standard-library file](#install) you can download and run.

## Teleport between Claude and Codex

The new `teleport` command converts a local Claude transcript or Codex rollout
into an **independent, resumable fork** in the other client. It supports CLI
resumption, Codex desktop's shared session store, and optional Claude desktop
partition metadata. These are private formats: see the [probe results and
validation limits](docs/teleport-investigation.md).

```bash
# Find source transcript paths (read-only; newest 20 by default)
claude-sessions sessions --agent claude
claude-sessions sessions --agent codex -n 10

# Claude -> Codex: inspect the plan, then create the session
claude-sessions teleport /path/to/claude-session.jsonl --to codex
claude-sessions teleport /path/to/claude-session.jsonl --to codex --apply

# Claude -> Codex desktop, preserving project placement
claude-sessions teleport /path/to/claude-session.jsonl --to codex --codex-project auto --apply

# Codex -> Claude CLI
claude-sessions teleport /path/to/rollout.jsonl --to claude --apply

# Codex -> Claude CLI AND the signed-in Claude desktop partition
claude-sessions teleport /path/to/rollout.jsonl --to claude --desktop-partition active --apply

# Run from Windows to put the Claude transcript in WSL and register it in desktop
claude-sessions teleport C:/exports/rollout.jsonl --to claude --target-host wsl:Ubuntu --cwd /home/me/repo --desktop-partition active --apply
```

The command prints the new ID and `codex resume <id>` or `claude --resume <id>`.
Run it in the printed working directory using the same client home. Codex
desktop may need a restart to discover the chat; Claude desktop needs a session
list reload (switch accounts or restart). Include `--desktop-partition` on the
initial Claude import: repeats leave existing imports untouched.

Use `--target-home DIR` to select the destination `.codex` or `.claude` directory;
the defaults honor `CODEX_HOME` and `CLAUDE_CONFIG_DIR`. For listing another
store use `sessions --agent codex --home DIR` (`--all` includes archived Codex
rollouts). Windows and WSL stores are separate. When their path spellings
differ, supply `--cwd` in the destination's syntax; the working tree must
already exist. The tool does not move files or rewrite paths inside messages.

For Codex desktop **project membership**, add `--codex-project auto`. The tool
reuses a project with an exact working-directory root or creates one for that
folder. It never guesses from a parent folder; multiple exact matches require
`--codex-project ID` (or a unique project name). This uses Codex's native project
APIs and registers the destination provider without sending a model prompt.
A recent Codex executable with project APIs is required; use `--codex-bin PATH`
for the desktop-bundled executable if your CLI is older. Without this option,
imports remain independent of the Codex executable and preserve the cwd only.

To assign an already imported session without reimporting its conversation:

```bash
claude-sessions codex-project /path/to/.codex/sessions/.../rollout.jsonl --apply
```

Both commands support `--target-home`, `--codex-bin`, and
`--codex-sqlite-home DIR` for desktops whose `CODEX_SQLITE_HOME` differs from
`CODEX_HOME`. Use the destination host's paths and binary: on Windows/WSL,
the desktop may use `/mnt/c/Users/NAME/.codex` for sessions and a separate WSL
index. These paths vary by machine. A different `--target-home`
does not inherit the current host's database override.

Dry runs never start Codex or resolve/create projects. On apply, project lookup
happens before transcript publication. If later registration fails, the error
identifies the retained transcript; `codex-project` repairs placement without
replacing it. Existing assignments are preserved; archived sessions are refused.
**Desktop limitation:** persisted backend membership is verified. Some desktop
builds still keep a separate local project registry and gate synchronization of
thread assignments. On those builds, a new backend project may remain absent
from the sidebar even after refreshing; add/select the matching project in the
app. This command does not rewrite the live app's global settings or claim
verified sidebar placement. Project membership is destination-local: foreign project IDs and project settings are
not copied, and round trips resolve the destination folder again.

**What transfers:** active user/assistant text and completed native tool calls
and results, including their IDs, names, arguments, output and order. Claude
uses `tool_use`/`tool_result`; Codex uses function/custom call and output items.
Codex also receives completed tool cards under an `imported_history` display
namespace. Historical tools are not registered or rerun.

Both round trips—Claude → Codex → Claude and Codex → Claude → Codex—preserve
supported conversation content. Conversion metadata retains distinctions the
other format cannot express directly, such as custom tool input, argument JSON
formatting, error flags and assistant phases. Codex ambient browser wrappers are
kept in conversion metadata, outside Claude user prompts, and restored on return
to Codex. Quoted examples and user-authored headings are preserved. Metadata is
checked against the native
record before reuse; changed native content takes precedence. New session IDs
and message envelopes are expected, and clients that discard this metadata can
reduce return-trip fidelity. See the [native-tool investigation and tests](docs/teleport-native-tools.md).

Private reasoning and source system instructions are omitted; images and
unsupported content get placeholders. Compaction transfers the surviving
context, which may exclude older turns. The dry run reports these changes.
Tool permissions, credentials and running processes do not transfer. Pending,
orphaned or duplicate tool exchanges are rejected before writing; finish the
source turn first. Claude imports also reject results separated from their call
group by intervening turns, because its loader discards that output. Destination
tools and project instructions apply on resume.

Dry runs create nothing. Applying never edits the source or overwrites an
existing import. Repeating the command is a no-op, including for archived Codex
imports. Deleted Claude desktop imports are refused. Malformed/incomplete transcripts,
unsupported rollback history and unavailable directories are rejected. Partial desktop
imports are reported as errors; existing files are preserved for inspection.
See the [red-team findings and fixes](docs/teleport-redteam.md).

## What it looks like

This is real output from a demo store: one login with two orgs, `work` and
`personal`. You are signed into `personal`, so the app shows only its one
session. `work` holds two more, and it has 2% of its quota left.

```
$ claude-sessions partitions
PARTITION   ORG UUID                              ACCOUNT   ALL  UNARCH  DEL  LAST ACTIVITY     QUOTA LEFT        CONNECTORS
──────────  ────────────────────────────────────  ────────  ───  ──────  ───  ────────────────  ────────────────  ───────────────────────
  work      3c426532-1eaa-4e6f-93c1-4d30abca7b89  1eb44d48    3       2    0  2026-09-27 17:31  ░░░░░░░░░░░░ 2%   Datadog, Linear, Sentry
● personal  762f7f2a-1cab-4c8a-98d1-d53bf5e8872c  1eb44d48    1       1    0  2026-09-27 16:31  ███████████░ 88%  Linear, Sentry
```

A dry run shows exactly what would happen, including the connector fixes a
plain file copy would miss (see [below](#the-part-that-makes-a-naive-cp-wrong)):

```
$ claude-sessions copy --from work

┌────────────────────────────────────┐            ┌────────────────────────────────────┐
│ SOURCE                             │            │ TARGET   ● signed in               │
│ ────────────────────────────────── │            │ ────────────────────────────────── │
│ work                               │            │ personal                           │
│ org  3c426532-1eaa-4e6f-93c1-4d30… │            │ org  762f7f2a-1cab-4c8a-98d1-d53b… │
│ acct 1eb44d48…                     │            │ acct 1eb44d48…                     │
│                                    │ ═══ 2 ═══▶ │                                    │
│ 3 sessions · 2 unarchived          │            │ 1 sessions · 1 unarchived          │
│ last active 2026-09-27 17:31       │            │ last active 2026-09-27 16:31       │
│ connectors  Datadog, Linear, Sent… │            │ connectors  Linear, Sentry         │
│                                    │            │                                    │
│ quota left ░░░░░░░░░░░░░░░░ 2%     │            │ quota left ██████████████░░ 88%    │
└────────────────────────────────────┘            └────────────────────────────────────┘

   ACTION          ID        LAST ACTIVITY     TITLE                     BRANCH/DIR  FIX
─  ──────────────  ────────  ────────────────  ────────────────────────  ──────────  ───
✓  COPY            5651c527  2026-09-27 17:31  Fix flaky auth test       api         7
✓  COPY            7c0ffee0  2026-09-27 13:31  Migrate billing webhooks  api         6
✗  skip: archived  0ddba11a  2026-09-24 00:31  Old spike                 api

port fixes applied (FIX column counts these per session):
   2x  remapped Linear tool keys: 01812872 ▶ 4b57c823
   2x  remapped Sentry tool keys: e5c4f439 ▶ be036bca
   2x  dropped stale tool keys for Datadog (9d1a0b7e)
   2x  remapped Linear: 01812872 ▶ 4b57c823
   2x  remapped Sentry: e5c4f439 ▶ be036bca
   2x  dropped Datadog (9d1a0b7e) - not present in personal
   1x  cleared previous crash state

2 COPY   1 skip: archived
transcripts are shared on disk - none are copied or duplicated.

DRY RUN. Nothing written. Re-run with --apply to copy 2 session(s).
```

Apply it, and the signed-in org has all three:

```
$ claude-sessions copy --from work --apply
...
✓ 5651c527  Fix flaky auth test
✓ 7c0ffee0  Migrate billing webhooks

Copied 2 session(s) into personal (762f7f2a-1cab-4c8a-98d1-d53bf5e8872c).
The app caches its session list in memory. Switch accounts in the app to
force a reload - faster than restarting it, and it works just as well.

$ claude-sessions sessions -p personal
personal  (762f7f2a-1cab-4c8a-98d1-d53bf5e8872c)  ● signed in
─────────────────────────────────────────────────────────────
FLG  ID        LAST ACTIVITY     TITLE                     BRANCH/DIR  WSL
───  ────────  ────────────────  ────────────────────────  ──────────  ───
     5651c527  2026-09-27 17:31  Fix flaky auth test       api
     b16b00b5  2026-09-27 15:31  Blog post draft           blog
     7c0ffee0  2026-09-27 13:31  Migrate billing webhooks  api
(3 unarchived of 3)   flags: A=archived  !=transcript missing  R=ssh/remote  W=runs in WSL
```

## Two reasons a session goes missing

If you use Claude Code under more than one organization, say a work org and a
personal Max plan, the desktop app shows you **only the partition you are
currently signed into**. Sessions from your other org are still on disk. They
are just not shown. `copy` puts them into the partition you are signed into.

Sessions you started by running `claude` inside WSL are invisible for a
different reason: they have **no desktop metadata at all**, so no org you sign
into will ever show them. That is a separate axis, with a separate fix.

| axis | what differs | command |
|---|---|---|
| **partition** | account/org: same machine, same transcripts | `copy` |
| **host** | WSL vs Windows: different filesystem, different install | `adopt` / `eject` |

## How Claude Code stores sessions

Everything is local. Nothing about a session lives in the cloud, and nothing
syncs between machines.

| What | Where |
|---|---|
| Session metadata | `%APPDATA%/Claude/claude-code-sessions/<accountUuid>/<orgUuid>/local_<id>.json` |
| Transcript | `~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl` |
| Deletion tombstone | `.../<orgUuid>/deleted_<id>` |

Three consequences drive this whole tool:

1. **Metadata is partitioned by account *and* org; transcripts are not.** Two
   orgs under the same login get separate metadata folders but share one
   transcript pool. So moving a session between partitions means copying a small
   JSON file. The conversation itself never moves and is never duplicated.

2. **The app caches its session index in memory.** Files written while it is
   running are not noticed; there is no filesystem watcher and no reload hook.
   Make it re-read disk afterwards. **Switching accounts in the app is enough,
   and is faster than restarting it.** Restarting works too.

3. **WSL is a third place entirely.** A distro has its own `~/.claude` with its
   own transcripts and *no* metadata directory, so sessions started there are
   invisible to the app in every org. See [WSL](#wsl).

## The part that makes a naive `cp` wrong

Session metadata is **not org-portable as-is**. Two fields are org-scoped:

- `remoteMcpServersConfig`: the *same* connector has a **different UUID in each
  org**. Linear might be `01812872…` in your work org and `4b57c823…` in your
  personal one.
- `enabledMcpTools`: keyed `"<serverUuid>:<toolName>"`, so every one of those
  keys inherits the stale UUID.

Copy the file as-is and the session lands pointing at connectors that do not
exist in the destination org. This tool remaps them by connector *name*, builds
the destination's name→UUID map from its own native sessions, and drops
connectors the destination org does not have. It also clears the source's stale
runtime state. From the dry run above:

```
   2x  remapped Linear: 01812872 ▶ 4b57c823
   2x  remapped Sentry: e5c4f439 ▶ be036bca
   2x  dropped Datadog (9d1a0b7e) - not present in personal
   1x  cleared previous crash state
```

## Install

Single file, standard library only, Python 3.10+. **No dependencies, ever.**
That is a design constraint, not an accident, and it is
[enforced by tests](https://github.com/aviadr1/claude-session-teleporter/blob/main/INVARIANTS.md#packaging):
the tool may not import anything outside the standard library, the wheel
carries no `Requires-Dist`, and CI runs the bare file on an interpreter with
nothing installed.

**With uv** (puts `claude-sessions` on your PATH):

```bash
uv tool install claude-session-teleporter     # or: pip install claude-session-teleporter
claude-sessions --help
```

Or run it once without installing:

```bash
uvx --from claude-session-teleporter claude-sessions --help
```

For the latest `main`, install from GitHub instead:
`uv tool install git+https://github.com/aviadr1/claude-session-teleporter`.

**As a single file.** In bash (WSL, macOS, Linux, Git Bash):

```bash
mkdir -p ~/.claude/tools
curl -o ~/.claude/tools/claude_sessions.py \
  https://raw.githubusercontent.com/aviadr1/claude-session-teleporter/main/claude_sessions.py
python3 ~/.claude/tools/claude_sessions.py --help
```

In PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\tools" | Out-Null
curl.exe -o "$HOME\.claude\tools\claude_sessions.py" `
  https://raw.githubusercontent.com/aviadr1/claude-session-teleporter/main/claude_sessions.py
python "$HOME\.claude\tools\claude_sessions.py" --help
```

**The PyPI package is `claude-session-teleporter`**, and the command it installs
is `claude-sessions`. Do not install `claude-sessions` from PyPI: that name
belongs to an unrelated project.

Windows is the primary target (that is where the paths were verified). macOS and
Linux paths are implemented but untested. Set `CLAUDE_SESSIONS_ROOT` to
override if detection is wrong.

## Usage

The examples use `claude-sessions`, the installed command. With the single
file, run `python claude_sessions.py` instead.

Start here. A full walkthrough, printed to your terminal:

```bash
claude-sessions guide      # the whole story, start to finish
claude-sessions --help     # traditional help, with examples
claude-sessions copy -h    # per-command help, incl. safety and port fixes
```

Then the commands themselves:

```bash
# what partitions exist, and how much plan quota each has left
claude-sessions partitions

# name one so you stop reading UUIDs
claude-sessions label 3c426532 work

# unarchived sessions (flags: A=archived  !=transcript missing  R=ssh/remote  W=WSL)
claude-sessions sessions -p work
claude-sessions sessions -p work --all

# which partition is "active"? three defensible answers
claude-sessions active

# dry run: copy everything unarchived from work into the signed-in partition
claude-sessions copy --from work

# just one session, then actually do it
claude-sessions copy --from work -s 5651c527 --apply
```

`copy` prints a direction diagram, the per-session plan, and the port fixes it
would apply, then stops. Nothing is written without `--apply`.

### WSL

```bash
# this machine, plus every WSL distro with a Claude Code install
claude-sessions hosts

# what is in there. ORIGIN separates two things that share a directory:
#   cli      you ran `claude` at a terminal inside the distro
#   desktop  the Windows app started it, using the distro as its environment
claude-sessions sessions -H wsl:Ubuntu
claude-sessions sessions -H wsl:Ubuntu --cli -n 10

# make WSL CLI sessions visible in the app (dry run, then for real)
claude-sessions adopt --from wsl:Ubuntu
claude-sessions adopt --from wsl:Ubuntu -s 3f8137a7 --apply

# the other direction: hand a Windows session to the CLI inside WSL
claude-sessions eject 8aef0655 --to wsl:Ubuntu --apply
```

`adopt` writes **only metadata**. The transcript stays inside the distro and is
never copied or rewritten. The app runs `claude` in WSL against the file that
is already there, so the terminal and the app are the *same* session rather
than two forks of it. Three fields do the work:

```json
"wslConfig":               {"distro": "Ubuntu"},
"cwd":                     "/home/you/projects/repo",
"sshRemoteTranscriptPath": "/home/you/.claude/projects/-home-you-projects-repo/<id>.jsonl"
```

`eject` is the one command that **forks**. A Windows session's working directory
has to be reachable from the distro (`C:\you\repo` is visible there as
`/mnt/c/you/repo`, the same files over drvfs), so it writes a second transcript
with the `cwd` rewritten, and prints the `wsl … claude --resume` line. The
Windows session keeps its own. Resume in one place or the other, never both.

### Let Claude drive it

```bash
claude-sessions skill              # print the SKILL.md
claude-sessions skill --install    # write it to ~/.claude/skills/
```

Installs a Claude Code skill that teaches Claude when this applies (sessions
"missing" after an org switch), the storage model, the dry-run-first workflow,
and the cache-reload caveat, so it stops guessing and stops reaching for `cp`.
The skill records the command you ran it with, so install it from the copy you
will keep (`uv tool install` or the downloaded file), not from a one-off `uvx`.
Restart Claude Code afterwards to pick it up.

### "Active" is ambiguous, so `active` gives you all three

1. **Signed in**, from `~/.claude.json`. Authoritative: the only partition the
   app will show you.
2. **Last active**: most recent session activity on disk.
3. **Most quota**, parsed from `plan-usage-history.json`, which records
   five-hour (`fh`) and seven-day (`sd`) usage percentages per org. Often the
   real reason you switched orgs in the first place.

## Safety

Every rule below is stated formally in
**[INVARIANTS.md](https://github.com/aviadr1/claude-session-teleporter/blob/main/INVARIANTS.md)**
and enforced by a named test in `tests/`. The invariant doc lists the test that
proves each one.

`copy` is built so that a mistake cannot cost you a session:

- **Never overwrites.** Uses exclusive file creation; there is deliberately no
  `--force` flag.
- **Never resurrects a deletion.** Skips any session with a `deleted_<id>`
  tombstone in the destination.
- **Never touches the source.** Copy only; the source partition is opened
  read-only.
- **Never duplicates transcripts.** They are shared by design.
- **Refuses sessions with no transcript on disk.** There would be nothing to
  continue.
- **Refuses cross-account copies** unless you pass `--allow-cross-account`.
- **Dry-run by default.**

`adopt` inherits all of that, never writes into the distro, derives its desktop
UUID from the WSL session id so re-running is idempotent, and resets
`permissionMode` to `auto`: a session this tool created must not arrive
pre-authorised to skip tool approval.

`eject` is the exception, and says so loudly: it writes a second transcript.

Imports are recorded in `~/.claude/session-copy-ledger.json` so that copied
sessions are excluded when building the destination's connector map. That
ledger cannot know about a copy made by hand or made before it existed, so the
map is additionally decided **by majority**: one stray import cannot redefine an
org's connectors, and a disagreement is reported rather than silently resolved.

## Development

[uv](https://docs.astral.sh/uv/) handles the dev environment. The tool itself
still has no dependencies. `pytest` lives in a dependency-group, so it is never
installed for users.

```bash
uv run pytest             # the suite; uv creates the env on first run
uv run pytest -m drift    # only the checks against your real session store
uv build                  # wheel + sdist
uv run claude-sessions --help
```

`tests/test_safety.py`, `tests/test_formats.py` and `tests/test_packaging.py`
pin every invariant in
[INVARIANTS.md](https://github.com/aviadr1/claude-session-teleporter/blob/main/INVARIANTS.md)
against fixtures.

`tests/test_format_drift.py` re-checks the reverse-engineered formats against
whatever real Claude Code store is on the machine, and skips cleanly when there
is none. CI stays green on a bare runner while your own machine acts as the
canary. If Anthropic changes a format, that file fails first. It is not
theoretical: it is how the connector-map bug in `copy` was found.

The suite is checked by **mutation**, not coverage: deliberately break an
invariant and a named test must go red. If you add one, break it first.

## Caveats

- Flags in `sessions`: `A` archived, `!` transcript missing, `R` ssh/remote,
  `W` runs in WSL.
- Sessions flagged `R` are ssh/remote; they resume only if that host is
  reachable.
- The destination org needs its own native session before the connector map can
  be built. With none, MCP config is stripped and the app repopulates it.
- `eject` only works for a working directory WSL can reach, meaning a drive
  path. A UNC path has no spelling inside the distro and is refused rather than
  guessed at.
- A `/mnt/c` checkout is the same files as the Windows one, reached over drvfs:
  slower, with Windows line endings and file modes.
- Reverse-engineered from on-disk formats, which Anthropic can change without
  notice. Verified against Claude Code 2.1.233 on Windows 11 with WSL2.
- Not affiliated with Anthropic.

## Coda

> They said that what you leave behind is lost,
> that each account must keep its own domain,
> that signing out is simply what it cost,
> and two-and-twenty threads went down the drain.
>
> But nothing left. It never touched a cloud.
> It sat in JSON, filed beneath a name;
> no window showed it, nothing spoke aloud —
> invisible, and present all the same.
>
> The transcript never moves; the pointer flies.
> But copy plain, and half of you stays back:
> a ghost that calls its tools, and none replies,
> and names you knew fall silent through the crack.
>
> &nbsp;&nbsp;&nbsp;&nbsp;So teleport, and let the app restart —
> &nbsp;&nbsp;&nbsp;&nbsp;oh, you _can_ take it with you. Every part.

## License

MIT
