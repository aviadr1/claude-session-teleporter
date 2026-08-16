# Claude Session Teleporter

> **Oh, you _can_ take it with you.**
> Out of quota, not out of context.

Teleport Claude Code desktop sessions between account/org partitions.

If you use Claude Code under more than one organization — a work org and a
personal Max plan, say — the desktop app shows you **only the partition you are
currently signed into**. Sessions from your other org are still on disk, they
are simply invisible. This tool finds them, and copies them into the partition
you are signed into so you can carry on.

```
$ claude_sessions.py partitions

session store: C:\Users\you\AppData\Roaming\Claude\claude-code-sessions
transcripts:   C:\Users\you\.claude\projects

PARTITION    ORG UUID                              ACCOUNT   ALL  UNARCH  DEL  LAST ACTIVITY     QUOTA LEFT        CONNECTORS
───────────  ────────────────────────────────────  ────────  ───  ──────  ───  ────────────────  ────────────────  ──────────────────────────────────
  work       3c426532-1eaa-4e6f-93c1-4d30abca7b89  1eb44d48   22       9    2  2026-08-16 13:15  ░░░░░░░░░░░░ 2%   Datadog, Linear, Sentry, visualize
● personal   762f7f2a-1cab-4c8a-98d1-d53bf5e8872c  1eb44d48    3       3    0  2026-08-16 13:36  ████████████ 99%  Datadog, Linear, Sentry, visualize
```

## How Claude Code stores sessions

Everything is local. Nothing about a session lives in the cloud, and nothing
syncs between machines.

| What | Where |
|---|---|
| Session metadata | `%APPDATA%/Claude/claude-code-sessions/<accountUuid>/<orgUuid>/local_<id>.json` |
| Transcript | `~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl` |
| Deletion tombstone | `.../<orgUuid>/deleted_<id>` |

Two consequences drive this whole tool:

1. **Metadata is partitioned by account *and* org; transcripts are not.** Two
   orgs under the same login get separate metadata folders but share one
   transcript pool. So moving a session between partitions means copying a small
   JSON file — the conversation itself never moves or gets duplicated.

2. **The app caches its session index in memory.** Files written while it is
   running are not noticed; there is no filesystem watcher and no reload hook.
   Restart Claude after copying.

## The part that makes a naive `cp` wrong

Session metadata is **not org-portable as-is**. Two fields are org-scoped:

- `remoteMcpServersConfig` — the *same* connector has a **different UUID in each
  org**. Linear might be `01812872…` in your work org and `4b57c823…` in your
  personal one.
- `enabledMcpTools` — keyed `"<serverUuid>:<toolName>"`, so every one of those
  keys inherits the stale UUID.

Copy the file as-is and the session lands pointing at connectors that do not
exist in the destination org. This tool remaps them by connector *name*, builds
the destination's name→UUID map from its own native sessions, and drops
connectors the destination org does not have:

```
remapped Linear: 01812872 ▶ 4b57c823
remapped Sentry: e5c4f439 ▶ be036bca
dropped  Datadog          (no equivalent in the destination)
stripped stale sshRemoteProcessId
cleared  previous crash state
```

## Install

Single file, standard library only, Python 3.10+. **No dependencies, ever** —
that is a design constraint, not an accident. Drop the file anywhere and run it.

```bash
curl -o ~/.claude/tools/claude_sessions.py \
  https://raw.githubusercontent.com/aviadr1/claude-session-teleporter/main/claude_sessions.py
```

Windows is the primary target (that is where the paths were verified). macOS and
Linux paths are implemented but untested — set `CLAUDE_SESSIONS_ROOT` to
override if detection is wrong.

## Usage

Start here — a full walkthrough, printed to your terminal:

```bash
claude_sessions.py guide      # the whole story, start to finish
claude_sessions.py --help     # traditional help, with examples
claude_sessions.py copy -h    # per-command help, incl. safety and port fixes
```

Then the commands themselves:

```bash
# what partitions exist, and how much plan quota each has left
claude_sessions.py partitions

# name one so you stop reading UUIDs
claude_sessions.py label 3c426532 work

# unarchived sessions (flags: A=archived  !=transcript missing  R=ssh/remote)
claude_sessions.py sessions -p work
claude_sessions.py sessions -p work --all

# which partition is "active"? three defensible answers
claude_sessions.py active

# dry run: copy everything unarchived from work into the signed-in partition
claude_sessions.py copy --from work

# just one session, then actually do it
claude_sessions.py copy --from work -s 5651c527 --apply
```

`copy` prints a direction diagram, the per-session plan, and the port fixes it
would apply, then stops. Nothing is written without `--apply`.

### Let Claude drive it

```bash
claude_sessions.py skill              # print the SKILL.md
claude_sessions.py skill --install    # write it to ~/.claude/skills/
```

Installs a Claude Code skill that teaches Claude when this applies (sessions
"missing" after an org switch), the storage model, the dry-run-first workflow,
and the restart-required caveat — so it stops guessing and stops reaching for
`cp`. Restart Claude Code afterwards to pick it up.

### "Active" is ambiguous, so `active` gives you all three

1. **Signed in** — from `~/.claude.json`. Authoritative: the only partition the
   app will show you.
2. **Last active** — most recent session activity on disk.
3. **Most quota** — parsed from `plan-usage-history.json`, which records
   five-hour (`fh`) and seven-day (`sd`) usage percentages per org. Often the
   real reason you switched orgs in the first place.

## Safety

`copy` is built so that a mistake cannot cost you a session:

- **Never overwrites.** Uses exclusive file creation; there is deliberately no
  `--force` flag.
- **Never resurrects a deletion.** Skips any session with a `deleted_<id>`
  tombstone in the destination.
- **Never touches the source.** Copy only; the source partition is opened
  read-only.
- **Never duplicates transcripts.** They are shared by design.
- **Refuses sessions with no transcript on disk** — there would be nothing to
  continue.
- **Refuses cross-account copies** unless you pass `--allow-cross-account`.
- **Dry-run by default.**

Imports are recorded in `~/.claude/session-copy-ledger.json` so that copied
sessions are excluded when building the destination's connector map — otherwise
the foreign UUIDs you just imported would poison the next copy.

## Caveats

- Sessions flagged `R` are ssh/remote; they resume only if that host is
  reachable.
- The destination org needs its own native session before the connector map can
  be built. With none, MCP config is stripped and the app repopulates it.
- Reverse-engineered from on-disk formats, which Anthropic can change without
  notice. Verified against Claude Code 2.1.229 on Windows.
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
