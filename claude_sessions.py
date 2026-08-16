#!/usr/bin/env python3
"""
claude_sessions.py - inspect and migrate Claude Code sessions across
account/org partitions, and between WSL and the Windows desktop app.

Claude Code stores sessions entirely on local disk, in two pieces:

  metadata    %APPDATA%/Claude/claude-code-sessions/<accountUuid>/<orgUuid>/local_<id>.json
  transcript  ~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl

The desktop app only shows the partition you are currently signed into, so
sessions belonging to another org look "missing" even though they are right
there on disk. Transcripts are NOT partitioned - they are shared across every
partition on the machine - so moving a session between partitions means copying
its metadata JSON and nothing else.

The metadata is not org-portable as-is. Two fields are org-scoped:

  remoteMcpServersConfig   the same connector ("Linear") has a DIFFERENT uuid
                           in each org
  enabledMcpTools          keyed "<serverUuid>:<toolName>"

so a naive file copy carries dead connector uuids into the destination. This
tool remaps them by connector name and drops connectors the destination org
does not have. It also strips stale runtime state (crash info, ssh pid).

WSL is a second axis entirely. A Claude Code CLI running inside a WSL distro
keeps its own `~/.claude/projects` inside the distro and has NO desktop metadata
- so those sessions are invisible to the Windows app no matter which org you are
signed into. The desktop app can drive them: a session with

  wslConfig                {"distro": "Ubuntu"}
  sshRemoteTranscriptPath  /home/<user>/.claude/projects/<enc>/<cliSessionId>.jsonl

runs `claude` inside the distro against the transcript that is already there.
`adopt` writes exactly that metadata, so a WSL CLI session shows up in the app
without the transcript moving or being rewritten. `eject` goes the other way,
placing a Windows session's transcript where the WSL CLI will find it.

Commands:
  partitions   list every account/org partition on this machine
  hosts        list this machine plus every WSL distro with Claude Code in it
  sessions     list unarchived sessions (--all to include archived)
  active       explain which partition is "active", three ways
  copy         copy sessions between partitions (dry-run by default)
  adopt        surface WSL CLI sessions in the desktop app (dry-run by default)
  eject        put a desktop session's transcript where the WSL CLI finds it
  label        give a partition a human-readable name
  guide        print a start-to-finish walkthrough
  skill        print or install a Claude Code skill for this tool

Run `--help` for examples, or `guide` for the long version.

Safety invariants for `copy`:
  * never overwrites an existing session in the destination (no --force exists)
  * never writes over a `deleted_<id>` tombstone (a session you deleted there)
  * never modifies or removes anything in the source partition
  * never touches transcripts - they are shared, not duplicated
  * refuses a session whose transcript is missing (nothing to continue)
  * dry-run is the default; --apply is required to write

`adopt` and `eject` inherit all of those, and additionally never write into the
source host: adopt only writes desktop metadata, eject only writes into WSL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------


def sessions_root() -> Path:
    override = os.environ.get("CLAUDE_SESSIONS_ROOT")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows
        return Path(appdata) / "Claude" / "claude-code-sessions"
    mac = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    if mac.exists():
        return mac
    return Path.home() / ".config" / "Claude" / "claude-code-sessions"


PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_JSON = Path.home() / ".claude.json"
LABELS_PATH = Path.home() / ".claude" / "partition-labels.json"
LEDGER_PATH = Path.home() / ".claude" / "session-copy-ledger.json"

# fields that describe a dead process or a past crash - never carry them over
VOLATILE_FIELDS = ("sshRemoteProcessId",)
ERROR_FIELDS = ("error", "errorAt", "errorCategory")

__version__ = "1.1.0"
SKILL_DIR = Path.home() / ".claude" / "skills" / "claude-session-teleporter"

# distros that ship with Docker Desktop and never host a Claude Code install
SKIP_DISTROS = {"docker-desktop", "docker-desktop-data"}

# fields that describe where a session ran; adopt/eject own them explicitly and
# must not inherit them from the template session it clones
PLACEMENT_FIELDS = (
    "sessionId", "cliSessionId", "cwd", "originCwd", "createdAt", "lastActivityAt",
    "lastFocusedAt", "title", "titleSource", "isArchived", "branch", "sourceBranch",
    "worktreePath", "worktreeName", "writtenBranches", "prs", "promptSuggestion",
    "completedTurns", "seenCommentIds", "wslConfig", "sshRemoteTranscriptPath",
)

# namespace for deriving a stable desktop uuid from a WSL cli session id, so
# re-running adopt is idempotent instead of duplicating the session every time
ADOPT_NS = _uuid.UUID("6f3c1e0a-4b2d-5e7a-9c11-8a0d5f2b3c44")


# ---------------------------------------------------------------------------
# glyphs (degrade to pure ASCII when the terminal cannot encode box drawing)
# ---------------------------------------------------------------------------


class Glyphs:
    def __init__(self, unicode_ok: bool):
        self.unicode = unicode_ok
        if unicode_ok:
            self.tl, self.tr, self.bl, self.br = "┌", "┐", "└", "┘"
            self.h, self.v = "─", "│"
            self.arrow, self.beam = "▶", "═"
            self.full, self.empty = "█", "░"
            self.dot, self.check, self.cross, self.warn = "●", "✓", "✗", "!"
            self.mid, self.ell = "·", "…"
        else:
            self.tl = self.tr = self.bl = self.br = "+"
            self.h, self.v = "-", "|"
            self.arrow, self.beam = ">", "="
            self.full, self.empty = "#", "."
            self.dot, self.check, self.cross, self.warn = "*", "+", "x", "!"
            self.mid, self.ell = "-", "..."


def make_glyphs(force_ascii: bool) -> Glyphs:
    if force_ascii:
        return Glyphs(False)
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "┌█▶".encode(enc)
    except (UnicodeEncodeError, LookupError):
        return Glyphs(False)
    return Glyphs(True)


G = Glyphs(True)  # replaced in main()


# ---------------------------------------------------------------------------
# hosts: this machine, plus every WSL distro with a Claude Code install
# ---------------------------------------------------------------------------


def encode_cwd(cwd: str) -> str:
    """
    Claude Code's project-directory encoding: every character that is not
    alphanumeric becomes a dash. Verified against both universes:

      C:\\Users\\a\\GitHub\\repo          -> C--Users-a-GitHub-repo
      /home/a/projects/repo             -> -home-a-projects-repo
      ...\\repo\\.claude\\worktrees\\wt    -> ...-repo--claude-worktrees-wt
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def win_to_wsl_path(win: str) -> str | None:
    """C:\\Users\\a\\x -> /mnt/c/Users/a/x. None if it is not a drive path."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", win)
    if not m:
        return None
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}".rstrip("/")


def wsl_to_win_path(posix: str, distro: str) -> str:
    """
    Map a path inside a distro to something Windows can open.

    /mnt/c/Users/a/x  -> C:\\Users\\a\\x          (the same files, natively)
    /home/a/x         -> \\\\wsl.localhost\\<d>\\home\\a\\x  (the same files, over 9P)
    """
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", posix)
    if m:
        return f"{m.group(1).upper()}:\\" + m.group(2).replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}" + posix.replace("/", "\\")


@dataclass
class Host:
    """A place Claude Code transcripts live. Either this machine, or a distro."""

    kind: str  # "windows" | "wsl"
    distro: str | None = None
    user: str | None = None
    posix_home: str | None = None  # /home/aviadr1, WSL only
    mount: Path | None = None  # where this host's root is reachable from here

    @property
    def name(self) -> str:
        return self.distro and f"wsl:{self.distro}" or "windows"

    @property
    def is_wsl(self) -> bool:
        return self.kind == "wsl"

    @property
    def root(self) -> Path:
        """Where this host's filesystem root is reachable from this process."""
        if self.mount is not None:
            return self.mount
        return Path(f"\\\\wsl.localhost\\{self.distro}") if self.is_wsl else Path(Path.home().anchor)

    @property
    def home(self) -> Path:
        """The host's home directory, as a path this process can open."""
        if not self.is_wsl:
            return Path.home()
        return self.root / (self.posix_home or "").lstrip("/")

    @property
    def projects(self) -> Path:
        # windows reads through the module-level constant so there is exactly one
        # place that decides where this machine's transcripts live
        return PROJECTS_DIR if not self.is_wsl else self.home / ".claude" / "projects"

    def transcript_path(self, cwd: str, cli_id: str) -> Path:
        """Where a transcript for `cwd` belongs on this host, as a Windows path."""
        return self.projects / encode_cwd(cwd) / f"{cli_id}.jsonl"

    def posix_transcript_path(self, project_dir: str, cli_id: str) -> str:
        """
        The same location, spelled the way the distro sees it.

        Takes the project directory verbatim rather than re-encoding a cwd: a
        session that moved into a git worktree keeps records naming the parent
        repo, so re-encoding its cwd yields a directory that does not exist.
        """
        return f"{self.posix_home}/.claude/projects/{project_dir}/{cli_id}.jsonl"


WINDOWS_HOST = Host(kind="windows")


def _wsl(*args: str, timeout: int = 20) -> str | None:
    """Run wsl.exe and return stdout, or None if WSL is unavailable."""
    try:
        out = subprocess.run(
            ["wsl.exe", *args], capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    raw = out.stdout
    # wsl.exe -l emits UTF-16LE; everything through `-e` is plain utf-8
    text = raw.decode("utf-16-le", errors="replace") if b"\x00" in raw[:40] else raw.decode(
        "utf-8", errors="replace"
    )
    return text.replace("\x00", "")


_HOST_CACHE: list[Host] | None = None


def discover_hosts() -> list[Host]:
    """This machine, plus every running-capable distro that has a ~/.claude."""
    global _HOST_CACHE
    if _HOST_CACHE is not None:
        return _HOST_CACHE
    hosts = [WINDOWS_HOST]
    listing = _wsl("-l", "-q")
    for line in (listing or "").splitlines():
        distro = line.strip()
        if not distro or distro in SKIP_DISTROS:
            continue
        info = _wsl("-d", distro, "-e", "sh", "-c", "echo $HOME; id -un")
        if not info:
            continue
        parts = [p.strip() for p in info.splitlines() if p.strip()]
        if not parts:
            continue
        host = Host(kind="wsl", distro=distro, posix_home=parts[0], user=parts[1] if len(parts) > 1 else None)
        if host.projects.exists():
            hosts.append(host)
    _HOST_CACHE = hosts
    return hosts


def resolve_host(sel: str) -> Host:
    sel = sel.strip()
    low = sel.lower()
    if low in ("windows", "win", "local", "host"):
        return WINDOWS_HOST
    if low.startswith("wsl:"):
        low = low[4:]
    hosts = [h for h in discover_hosts() if h.is_wsl]
    if not hosts:
        die("no WSL distro on this machine has a Claude Code install (~/.claude/projects)")
    if low in ("wsl", ""):
        if len(hosts) > 1:
            die("several distros qualify; name one: " + ", ".join(h.name for h in hosts))
        return hosts[0]
    cands = [h for h in hosts if (h.distro or "").lower() == low]
    if not cands:
        cands = [h for h in hosts if low in (h.distro or "").lower()]
    if not cands:
        die(f"no WSL distro matches {sel!r}; known: " + ", ".join(h.name for h in hosts))
    if len(cands) > 1:
        die(f"{sel!r} is ambiguous: " + ", ".join(h.name for h in cands))
    return cands[0]


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


@dataclass
class Session:
    uuid: str  # bare uuid, no local_ prefix
    path: Path
    data: dict
    transcript: Path | None = None

    @property
    def session_id(self) -> str:
        return f"local_{self.uuid}"

    @property
    def title(self) -> str:
        return self.data.get("title") or "(untitled)"

    @property
    def archived(self) -> bool:
        return bool(self.data.get("isArchived"))

    @property
    def last_activity(self) -> int:
        return int(self.data.get("lastActivityAt") or self.data.get("createdAt") or 0)

    @property
    def cwd(self) -> str:
        return self.data.get("cwd") or ""

    @property
    def branch(self) -> str:
        return self.data.get("branch") or self.data.get("worktreeName") or ""

    @property
    def cli_session_id(self) -> str:
        return self.data.get("cliSessionId") or ""

    @property
    def wsl_distro(self) -> str | None:
        """The distro this session runs `claude` inside, if it is a WSL session."""
        cfg = self.data.get("wslConfig")
        return (cfg or {}).get("distro") if isinstance(cfg, dict) else None

    @property
    def remote_transcript(self) -> str:
        return self.data.get("sshRemoteTranscriptPath") or ""

    @property
    def is_wsl(self) -> bool:
        return bool(self.wsl_distro)

    @property
    def is_remote(self) -> bool:
        """ssh to another machine. WSL uses the same fields but is not remote."""
        if self.is_wsl:
            return False
        return bool(self.data.get("sshRemoteTranscriptPath") or self.data.get("sshRemoteProcessId"))

    @property
    def servers(self) -> dict[str, str]:
        """name -> uuid for this session's remote MCP connectors."""
        return {
            s.get("name"): s.get("uuid")
            for s in (self.data.get("remoteMcpServersConfig") or [])
            if s.get("name") and s.get("uuid")
        }


@dataclass
class Partition:
    account: str
    org: str
    path: Path
    sessions: list[Session] = field(default_factory=list)
    tombstones: set[str] = field(default_factory=set)
    imported: set[str] = field(default_factory=set)  # session uuids copied in by this tool
    label: str | None = None
    signed_in: bool = False
    usage: dict | None = None
    usage_at: int = 0

    @property
    def key(self) -> str:
        return f"{self.account}/{self.org}"

    @property
    def short(self) -> str:
        return self.org.split("-")[0]

    @property
    def name(self) -> str:
        return self.label or self.short

    @property
    def unarchived(self) -> list[Session]:
        return [s for s in self.sessions if not s.archived]

    @property
    def last_activity(self) -> int:
        acts = [s.last_activity for s in self.sessions] + [self.usage_at]
        return max(acts) if acts else 0

    @property
    def headroom(self) -> int | None:
        """Percent of plan quota left, by the tightest window. None if unknown."""
        if not self.usage:
            return None
        worst = max(int(self.usage.get(k, 0) or 0) for k in ("fh", "sd"))
        return max(0, 100 - worst)

    @property
    def connectors(self) -> dict[str, str]:
        """
        name -> uuid for THIS org's remote MCP connectors.

        Decided by MAJORITY, not by recency. Sessions imported from another org
        carry that org's uuids and would poison the map. The ledger catches the
        ones this tool copied, but cannot know about a copy made by hand, or
        made before the ledger existed - and taking the most recently active
        session would let a single such import redefine the whole org, sending
        every subsequent copy at connectors that do not exist here.

        One stray session cannot outvote the org it landed in. Recency only
        breaks a genuine tie.
        """
        votes: dict[str, dict[str, list[int]]] = {}  # name -> uuid -> [count, latest]
        for s in self.sessions:
            if s.uuid in self.imported:
                continue
            for name, uuid in s.servers.items():
                tally = votes.setdefault(name, {}).setdefault(uuid, [0, 0])
                tally[0] += 1
                tally[1] = max(tally[1], s.last_activity)
        return {
            name: max(by_uuid.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]
            for name, by_uuid in votes.items()
        }

    @property
    def contested_connectors(self) -> dict[str, dict[str, int]]:
        """
        name -> {uuid: session count} for names this org disagrees with itself
        about. Non-empty means unrecorded imports are sitting in this partition.
        """
        votes: dict[str, dict[str, int]] = {}
        for s in self.sessions:
            if s.uuid in self.imported:
                continue
            for name, uuid in s.servers.items():
                votes.setdefault(name, {})[uuid] = votes.setdefault(name, {}).get(uuid, 0) + 1
        return {n: v for n, v in votes.items() if len(v) > 1}


# ---------------------------------------------------------------------------
# small json stores
# ---------------------------------------------------------------------------


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_ledger() -> list[dict]:
    return _read_json(LEDGER_PATH, {}).get("copies", [])


def record_copies(entries: list[dict]) -> None:
    led = _read_json(LEDGER_PATH, {"copies": []})
    led.setdefault("copies", []).extend(entries)
    _write_json(LEDGER_PATH, led)


def signed_in_identity() -> dict:
    return _read_json(CLAUDE_JSON, {}).get("oauthAccount") or {}


def latest_usage_by_org() -> dict:
    out: dict[str, dict] = {}
    samples = _read_json(sessions_root().parent / "plan-usage-history.json", {}).get("samples", [])
    for s in samples:
        org, t = s.get("org"), int(s.get("t") or 0)
        if not org:
            continue
        if org not in out or t > out[org]["t"]:
            out[org] = {"t": t, "u": s.get("u") or {}}
    return out


def transcript_index() -> dict[str, Path]:
    idx: dict[str, Path] = {}
    if PROJECTS_DIR.exists():
        for p in PROJECTS_DIR.glob("*/*.jsonl"):
            idx.setdefault(p.stem, p)
    return idx


# ---------------------------------------------------------------------------
# reading a session out of a transcript, for hosts that have no metadata
# ---------------------------------------------------------------------------


@dataclass
class CliSession:
    """
    A session as it exists on a host with no desktop metadata - a CLI session
    inside WSL. Everything here is reconstructed from the transcript itself.
    """

    host: Host
    transcript: Path
    cli_id: str
    project_dir: str = ""  # the encoded directory the transcript actually sits in
    cwd: str = ""          # where it ended up
    origin_cwd: str = ""   # where it started, when the session moved
    moved: bool = False
    entrypoint: str = ""            # how the session was CREATED: cli | claude-desktop
    entrypoints: set = field(default_factory=set)  # everything that ever drove it
    title: str = ""
    branch: str = ""
    version: str = ""
    created_at: int = 0
    last_activity: int = 0
    last_prompt: str = ""
    turns: int = 0

    @property
    def uuid(self) -> str:
        """A stable desktop uuid for this transcript, so adopt is idempotent."""
        return str(_uuid.uuid5(ADOPT_NS, f"{self.host.name}:{self.cli_id}"))

    @property
    def session_id(self) -> str:
        return f"local_{self.uuid}"

    @property
    def born_in_cli(self) -> bool:
        """Created by someone running `claude` at a terminal inside the distro."""
        return self.entrypoint == "cli"

    @property
    def origin(self) -> str:
        """cli, desktop, or cli>desktop for a CLI session later opened in the app."""
        short = {"cli": "cli", "claude-desktop": "desktop"}
        first = short.get(self.entrypoint, self.entrypoint or "?")
        others = {short.get(e, e) for e in self.entrypoints} - {first}
        return f"{first}>{'/'.join(sorted(others))}" if others else first


def _ms(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def read_cli_session(host: Host, path: Path, deep: bool = True) -> CliSession | None:
    """
    Reconstruct a CliSession from a transcript.

    Cheap by default: the head gives cwd/branch/version/createdAt, and mtime
    gives last activity. `deep` additionally scans for the newest ai-title and
    last-prompt, which is what makes the listing readable - guarded by a
    substring test so most lines never reach the json parser.
    """
    sess = CliSession(host=host, transcript=path, cli_id=path.stem, project_dir=path.parent.name)
    try:
        sess.last_activity = int(path.stat().st_mtime * 1000)
    except OSError:
        return None
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    cwds: list[str] = []
    with fh:
        for i, line in enumerate(fh):
            if i > 4000 and not deep:
                break
            if '"cwd"' in line:
                rec = _loads(line)
                if rec and rec.get("cwd"):
                    if rec["cwd"] not in cwds:
                        cwds.append(rec["cwd"])
                    if not sess.created_at:
                        sess.created_at = _ms(rec.get("timestamp") or "")
                    sess.branch = rec.get("gitBranch") or sess.branch
                    sess.version = rec.get("version") or sess.version
                    # who drove this turn: `cli` is someone at a terminal inside
                    # the distro, `claude-desktop` is the Windows app using the
                    # distro as an environment. Entirely different origins that
                    # happen to leave their transcripts in the same place.
                    ep = rec.get("entrypoint")
                    if ep:
                        sess.entrypoints.add(ep)
                        if not sess.entrypoint:
                            sess.entrypoint = ep
                    # counted here rather than by matching the raw line: these
                    # records are already parsed, and a substring guard would
                    # depend on the writer's json spacing
                    if rec.get("type") == "user" and (rec.get("origin") or {}).get("kind") == "human":
                        sess.turns += 1
            if not deep:
                # stop as soon as a cwd agrees with the directory - for a moved
                # session that is not the first one we see
                if any(encode_cwd(c) == sess.project_dir for c in cwds):
                    break
                continue
            if '"ai-title"' in line:
                rec = _loads(line)
                if rec and rec.get("type") == "ai-title":
                    sess.title = rec.get("aiTitle") or sess.title
            elif '"last-prompt"' in line:
                rec = _loads(line)
                if rec and rec.get("type") == "last-prompt":
                    sess.last_prompt = rec.get("lastPrompt") or sess.last_prompt
    if not cwds:
        return None
    # A session can move: started in one repo, ended up in a worktree or another
    # repo entirely. Claude Code re-homes the transcript when that happens, so
    # the directory names the CURRENT cwd. Prefer the cwd that agrees with it,
    # and keep the first one as the origin - the same split the app itself makes.
    sess.origin_cwd = cwds[0]
    sess.cwd = next(
        (c for c in reversed(cwds) if encode_cwd(c) == sess.project_dir), cwds[-1]
    )
    # visiting a subdirectory and coming back is not a move; ending up somewhere
    # else is - that is the case worth telling the user about
    sess.moved = sess.origin_cwd != sess.cwd
    if not sess.title:
        sess.title = sess.last_prompt.strip().splitlines()[0][:70] if sess.last_prompt else "(untitled)"
    return sess


def _loads(line: str) -> dict | None:
    try:
        rec = json.loads(line)
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def scan_cli_sessions(host: Host, deep: bool = True) -> list[CliSession]:
    """Every transcript on `host`, newest first."""
    if not host.projects.exists():
        die(f"{host.name} has no transcripts at {host.projects}")
    out = []
    for p in sorted(host.projects.glob("*/*.jsonl")):
        s = read_cli_session(host, p, deep=deep)
        if s:
            out.append(s)
    out.sort(key=lambda s: -s.last_activity)
    return out


def resolve_cli_sessions(pool: list[CliSession], sels: list[str]) -> list[CliSession]:
    out = []
    for sel in sels:
        cands = [x for x in pool if x.cli_id.startswith(sel)]
        if not cands:
            cands = [x for x in pool if sel.lower() in x.title.lower()]
        if not cands:
            die(f"no session matches {sel!r}")
        if len(cands) > 1:
            die(f"session {sel!r} is ambiguous: " + ", ".join(f"{c.cli_id[:8]} {c.title}" for c in cands))
        out.append(cands[0])
    return out


def wsl_transcript(sess: Session) -> Path | None:
    """
    Resolve a WSL session's transcript, which lives inside the distro.

    The desktop app mirrors it to ~/.claude/projects/ssh-<cliSessionId>/ once
    the session has been opened, but a freshly adopted session has no mirror
    yet - without this it would look like a session with no transcript and
    `copy` would refuse to move it between orgs.
    """
    distro, remote = sess.wsl_distro, sess.remote_transcript
    if not distro or not remote:
        return None
    p = Path(wsl_to_win_path(remote, distro))
    try:
        return p if p.exists() else None
    except OSError:
        return None


def load_partitions() -> list[Partition]:
    root = sessions_root()
    if not root.exists():
        die(f"session store not found: {root}")
    labels = _read_json(LABELS_PATH, {})
    ident = signed_in_identity()
    usage = latest_usage_by_org()
    tindex = transcript_index()
    ledger = load_ledger()

    parts: list[Partition] = []
    for acct_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for org_dir in sorted(p for p in acct_dir.iterdir() if p.is_dir()):
            part = Partition(account=acct_dir.name, org=org_dir.name, path=org_dir)
            part.label = labels.get(org_dir.name) or labels.get(part.key)
            part.signed_in = (
                ident.get("accountUuid") == acct_dir.name
                and ident.get("organizationUuid") == org_dir.name
            )
            if part.signed_in and not part.label:
                part.label = ident.get("organizationName")
            u = usage.get(org_dir.name)
            if u:
                part.usage, part.usage_at = u["u"], u["t"]
            part.imported = {e["uuid"] for e in ledger if e.get("dest") == org_dir.name}

            for fp in sorted(org_dir.glob("local_*.json")):
                data = _read_json(fp, None)
                if data is None:
                    warn(f"unreadable session, skipped: {fp.name}")
                    continue
                sess = Session(uuid=fp.stem[len("local_") :], path=fp, data=data)
                sess.transcript = tindex.get(sess.cli_session_id) or wsl_transcript(sess)
                part.sessions.append(sess)

            for fp in org_dir.glob("deleted_*"):
                part.tombstones.add(fp.name[len("deleted_") :])

            parts.append(part)
    return parts


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------


def die(msg: str):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def warn(msg: str) -> None:
    sys.stdout.flush()  # keep warnings in sequence with the report they annotate
    print(f"{G.warn} {msg}", file=sys.stderr)
    sys.stderr.flush()


def ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "-"


def trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - len(G.ell)] + G.ell


def bar(pct: int | None, width: int = 20) -> str:
    if pct is None:
        return "?" * width
    filled = round(width * pct / 100)
    return G.full * filled + G.empty * (width - filled)


def table(headers: list[str], rows: list[list[str]], aligns: str | None = None) -> str:
    if not rows:
        rows = [["-"] * len(headers)]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    aligns = aligns or "l" * len(headers)

    def fmt(cells):
        return "  ".join(
            c.rjust(widths[i]) if aligns[i] == "r" else c.ljust(widths[i]) for i, c in enumerate(cells)
        ).rstrip()

    sep = "  ".join(G.h * w for w in widths)
    return "\n".join([fmt(headers), sep] + [fmt(r) for r in rows])


def box(lines: list[str], width: int) -> list[str]:
    out = [G.tl + G.h * (width + 2) + G.tr]
    out += [f"{G.v} {ln.ljust(width)} {G.v}" for ln in lines]
    out.append(G.bl + G.h * (width + 2) + G.br)
    return out


def side_by_side(left: list[str], right: list[str], joiner: list[str]) -> str:
    h = max(len(left), len(right), len(joiner))
    left = left + [" " * len(left[0])] * (h - len(left))
    right = right + [" " * len(right[0])] * (h - len(right))
    joiner = joiner + [" " * len(joiner[0])] * (h - len(joiner))
    return "\n".join(a + b + c for a, b, c in zip(left, joiner, right))


def partition_card(p: Partition, role: str, width: int = 34) -> list[str]:
    head = role + (f"   {G.dot} signed in" if p.signed_in else "")
    hr = int(p.headroom) if p.headroom is not None else None
    lines = [
        head,
        G.h * width,
        p.name,
        f"org  {p.org}",
        f"acct {p.account[:8]}{G.ell}",
        "",
        f"{len(p.sessions)} sessions {G.mid} {len(p.unarchived)} unarchived",
        f"last active {ts(p.last_activity)}",
        f"connectors  {', '.join(sorted(p.connectors)) or '(none)'}",
        "",
        f"quota left {bar(hr, 16)} {f'{hr}%' if hr is not None else '  ?'}",
    ]
    return box([trunc(l, width) for l in lines], width)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def resolve_partition(parts: list[Partition], sel: str) -> Partition:
    sel = sel.strip()
    low = sel.lower()
    if low in ("active", "signed-in", "signedin", "current"):
        hits = [p for p in parts if p.signed_in]
        if not hits:
            die("no signed-in partition found (is the desktop app logged in?)")
        return hits[0]
    if low == "last-active":
        return max(parts, key=lambda p: p.last_activity)
    if low == "most-quota":
        known = [p for p in parts if p.headroom is not None]
        if not known:
            die("no quota data available")
        return max(known, key=lambda p: p.headroom or 0)

    cands = [p for p in parts if (p.label or "").lower() == low]
    if not cands:
        cands = [p for p in parts if p.org.startswith(sel) or p.key.startswith(sel)]
    if not cands:
        cands = [p for p in parts if sel in p.org or sel.lower() in (p.label or "").lower()]
    if not cands:
        die(f"no partition matches {sel!r}")
    if len(cands) > 1:
        die(f"{sel!r} is ambiguous: " + ", ".join(p.key for p in cands))
    return cands[0]


def resolve_sessions(part: Partition, sels: list[str]) -> list[Session]:
    out = []
    for sel in sels:
        s = sel[len("local_") :] if sel.startswith("local_") else sel
        cands = [x for x in part.sessions if x.uuid.startswith(s)]
        if not cands:
            cands = [x for x in part.sessions if s.lower() in x.title.lower()]
        if not cands:
            die(f"no session in {part.name} matches {sel!r}")
        if len(cands) > 1:
            die(f"session {sel!r} is ambiguous: " + ", ".join(f"{c.uuid[:8]} {c.title}" for c in cands))
        out.append(cands[0])
    return out


# ---------------------------------------------------------------------------
# the org-portability rewrite
# ---------------------------------------------------------------------------


def connector_names(parts: list[Partition]) -> dict[str, str]:
    """
    uuid -> connector name, learned from every partition on this machine.

    Needed because a session's enabledMcpTools can outlive the connector list
    it was written against: keys keep pointing at an org whose uuids are not in
    that session's own remoteMcpServersConfig any more. Without a global map
    those uuids are unnameable and cannot be repaired.
    """
    out: dict[str, str] = {}
    for p in parts:
        for s in p.sessions:
            for name, uuid in s.servers.items():
                out.setdefault(uuid, name)
    return out


def normalize_enabled_tools(
    data: dict, dst_by_name: dict[str, str], known: dict[str, str], notes: list[str]
) -> None:
    """
    Point every enabledMcpTools key at a connector that exists in `dst`.

    Keys are "<serverUuid>:<toolName>". A key already pointing into dst is left
    alone; one naming a connector dst also has is remapped by name; anything
    unrecognisable is dropped rather than carried over as a dead reference.
    """
    tools = data.get("enabledMcpTools")
    if not isinstance(tools, dict):
        return
    live = set(dst_by_name.values())
    new: dict[str, object] = {}
    remapped: dict[str, tuple[str, str]] = {}
    dropped: dict[str, str] = {}
    for key, val in tools.items():
        uuid, sep, tool = key.partition(":")
        if not sep or uuid in live:
            new[key] = val
            continue
        name = known.get(uuid)
        target = dst_by_name.get(name) if name else None
        if target:
            new[f"{target}:{tool}"] = val
            remapped[uuid] = (name or "?", target)
        else:
            dropped[uuid] = name or "unknown connector"
    data["enabledMcpTools"] = new
    for old, (name, target) in remapped.items():
        notes.append(f"remapped {name} tool keys: {old[:8]} {G.arrow} {target[:8]}")
    for old, name in dropped.items():
        notes.append(f"dropped stale tool keys for {name} ({old[:8]})")


def port_session(sess: Session, dst: Partition, keep_error: bool, known: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """
    Rewrite a session payload so it is valid in `dst`'s org.
    Returns (payload, human-readable notes).
    """
    data = json.loads(json.dumps(sess.data))  # deep copy
    notes: list[str] = []
    dst_by_name = dst.connectors

    remap: dict[str, str] = {}   # old uuid -> new uuid
    dropped: dict[str, str] = {} # dropped uuid -> name

    servers = data.get("remoteMcpServersConfig") or []
    if servers and not dst_by_name:
        data.pop("remoteMcpServersConfig", None)
        data.pop("enabledMcpTools", None)
        notes.append("destination has no known connectors; MCP config stripped (app will repopulate)")
        return _strip_volatile(data, keep_error, notes), notes

    kept = []
    for srv in servers:
        name, old = srv.get("name"), srv.get("uuid")
        new = dst_by_name.get(name)
        if not new:
            dropped[old] = name
            continue
        if new != old:
            remap[old] = new
            srv["uuid"] = new
        kept.append(srv)
    if servers:
        data["remoteMcpServersConfig"] = kept

    # Name every uuid the tool keys might mention: what this machine knows
    # globally, plus this session's own list. Keys can name a connector the
    # session itself no longer carries, so the session list alone is not enough.
    names = dict(known or {})
    for uuid, name in {u: n for n, u in sess.servers.items()}.items():
        names.setdefault(uuid, name)
    for uuid, name in dropped.items():
        names.setdefault(uuid, name)
    normalize_enabled_tools(data, dst_by_name, names, notes)

    for old, new in remap.items():
        name = next((n for n, u in dst_by_name.items() if u == new), "?")
        notes.append(f"remapped {name}: {old[:8]} {G.arrow} {new[:8]}")
    for old, name in dropped.items():
        notes.append(f"dropped {name} ({old[:8]}) - not present in {dst.name}")

    return _strip_volatile(data, keep_error, notes), notes


def _strip_volatile(data: dict, keep_error: bool, notes: list[str]) -> dict:
    for f in VOLATILE_FIELDS:
        if data.pop(f, None) is not None:
            notes.append(f"stripped stale {f}")
    if not keep_error:
        if any(f in data for f in ERROR_FIELDS):
            notes.append("cleared previous crash state")
        for f in ERROR_FIELDS:
            data.pop(f, None)
    return data


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_partitions(args) -> int:
    parts = load_partitions()
    print(f"session store: {sessions_root()}")
    print(f"transcripts:   {PROJECTS_DIR}\n")
    rows = []
    for p in parts:
        hr = p.headroom
        rows.append(
            [
                (G.dot if p.signed_in else " ") + " " + p.name,
                p.org,
                p.account[:8],
                str(len(p.sessions)),
                str(len(p.unarchived)),
                str(len(p.tombstones)),
                ts(p.last_activity),
                f"{bar(hr, 12)} {hr if hr is not None else '?'}%",
                ", ".join(sorted(p.connectors)) or "-",
            ]
        )
    print(
        table(
            ["PARTITION", "ORG UUID", "ACCOUNT", "ALL", "UNARCH", "DEL", "LAST ACTIVITY", "QUOTA LEFT", "CONNECTORS"],
            rows,
            aligns="lllrrrlll",
        )
    )
    unlabeled = [p for p in parts if not p.label]
    if unlabeled:
        print(f"\ntip: name a partition with  label {unlabeled[0].short} <name>")
    return 0


def cmd_hosts(args) -> int:
    hosts = discover_hosts()
    parts = load_partitions()
    adopted = {
        s.cli_session_id
        for p in parts
        for s in p.sessions
        if s.is_wsl
    }
    print("A host is a place transcripts live. Desktop metadata exists only on")
    print("windows - WSL sessions stay invisible to the app until you adopt them.\n")
    rows = []
    for h in hosts:
        if h.is_wsl:
            cli = scan_cli_sessions(h, deep=False)
            new = sum(1 for c in cli if c.cli_id not in adopted)
            rows.append([h.name, str(h.projects), str(len(cli)), str(new), h.posix_home or "-"])
        else:
            n = len(list(PROJECTS_DIR.glob("*/*.jsonl"))) if PROJECTS_DIR.exists() else 0
            rows.append([h.name, str(PROJECTS_DIR), str(n), "-", str(Path.home())])
    print(table(["HOST", "TRANSCRIPTS", "SESSIONS", "UNADOPTED", "HOME"], rows, aligns="llrrl"))
    wsl = [h for h in hosts if h.is_wsl]
    if not wsl:
        print("\nNo WSL distro here has a Claude Code install.")
    else:
        print(f"\ntip: see what is over there with  sessions -H {wsl[0].name}")
        print(f"     bring it into the app with     adopt --from {wsl[0].name}")
    return 0


def cmd_sessions(args) -> int:
    if args.host and args.host.lower() not in ("windows", "win", "local", "host"):
        return _sessions_on_host(args)
    parts = load_partitions()
    if args.partition:
        parts = [resolve_partition(parts, args.partition)]
    for p in parts:
        sel = sorted(p.sessions if args.all else p.unarchived, key=lambda s: -s.last_activity)
        head = f"{p.name}  ({p.org})" + (f"  {G.dot} signed in" if p.signed_in else "")
        print(head)
        print(G.h * len(head))
        rows = []
        for s in sel:
            flags = (
                ("A" if s.archived else " ")
                + (" " if s.transcript else "!")
                + ("W" if s.is_wsl else "R" if s.is_remote else " ")
            )
            rows.append(
                [
                    flags,
                    s.uuid[:8],
                    ts(s.last_activity),
                    trunc(s.title, 54),
                    trunc(s.branch or Path(s.cwd).name, 34),
                    s.wsl_distro or "",
                ]
            )
        print(table(["FLG", "ID", "LAST ACTIVITY", "TITLE", "BRANCH/DIR", "WSL"], rows))
        print(
            f"({len(sel)} {'all' if args.all else 'unarchived'} of {len(p.sessions)})"
            f"   flags: A=archived  !=transcript missing  R=ssh/remote  W=runs in WSL\n"
        )
    return 0


def _sessions_on_host(args) -> int:
    """List CLI sessions on a host that has no desktop metadata at all."""
    host = resolve_host(args.host)
    parts = load_partitions()
    adopted: dict[str, list[str]] = {}
    for p in parts:
        for s in p.sessions:
            if s.is_wsl and s.wsl_distro == host.distro:
                adopted.setdefault(s.cli_session_id, []).append(p.name)

    every = scan_cli_sessions(host)
    cli = every
    if args.cli:
        cli = [c for c in cli if c.born_in_cli]
    elif args.desktop:
        cli = [c for c in cli if not c.born_in_cli]
    shown = cli[: args.limit] if args.limit else cli

    head = f"{host.name}  ({host.projects})"
    print(head)
    print(G.h * len(head))
    rows = []
    for c in shown:
        where = adopted.get(c.cli_id)
        rows.append(
            [
                " " if where else "+",
                c.origin,
                c.cli_id[:8],
                ts(c.last_activity),
                trunc(c.title, 44),
                trunc(c.branch or Path(c.cwd.replace("/", os.sep)).name, 26),
                trunc(", ".join(where), 18) if where else "-",
            ]
        )
    print(table(
        ["FLG", "ORIGIN", "ID", "LAST ACTIVITY", "TITLE", "BRANCH/DIR", "ADOPTED IN"], rows
    ))

    born_cli = sum(1 for c in every if c.born_in_cli)
    scope = "started by the CLI in the distro" if args.cli else (
        "started by the Windows app" if args.desktop else "all origins"
    )
    print(
        f"(showing {len(shown)} of {len(cli)} {scope}; {len(every)} transcripts here in total)"
        f"   flags: +=no desktop metadata yet\n"
    )
    print("ORIGIN is who created the session, which is not the same as where it ran:")
    print(f"  cli      {born_cli:>3}  you ran `claude` at a terminal inside {host.name}")
    print(f"  desktop  {len(every) - born_cli:>3}  started in the Windows app, using {host.name} as its environment")
    print("  cli>desktop   started at the terminal, later opened in the app too")
    if not args.desktop:
        print(f"\nCLI sessions marked + have no desktop metadata - the app cannot see them")
        print(f"in ANY org. Run  adopt --from {host.name}  to change that.")
    return 0


def cmd_active(args) -> int:
    parts = load_partitions()
    ident = signed_in_identity()
    print('There are three defensible readings of "active":\n')

    signed = next((p for p in parts if p.signed_in), None)
    print(f"1. signed in    {f'{signed.name}  {signed.org}' if signed else '(none - app logged out?)'}")
    if ident:
        print(f"                {ident.get('emailAddress')} {G.mid} {ident.get('organizationName')}")
    print("                authoritative: the only partition the app will show you")

    last = max(parts, key=lambda p: p.last_activity) if parts else None
    print(f"\n2. last active  {f'{last.name}  {last.org}' if last else '-'}")
    print(f"                most recent session activity: {ts(last.last_activity) if last else '-'}")

    known = [p for p in parts if p.headroom is not None]
    best = max(known, key=lambda p: p.headroom or 0) if known else None
    print(f"\n3. most quota   {f'{best.name}  {best.org}' if best else '(no usage data)'}")
    for p in parts:
        if p.usage:
            fh, sd = int(p.usage.get("fh", 0)), int(p.usage.get("sd", 0))
            print(
                f"                {p.name:<12} 5h {bar(fh, 14)} {fh:>3}%   7d {bar(sd, 14)} {sd:>3}%"
            )
    print("\ncopy defaults to the signed-in partition as the destination.")
    return 0


COPY = "COPY"
SKIP_EXISTS = "skip: already there"
SKIP_TOMB = "skip: deleted there"
SKIP_ARCHIVED = "skip: archived"
SKIP_NO_TRANSCRIPT = "skip: no transcript"


def cmd_copy(args) -> int:
    parts = load_partitions()
    if len(parts) < 2:
        die("only one partition on this machine - nothing to copy between")

    dst = resolve_partition(parts, args.to or "active")
    if args.source:
        src = resolve_partition(parts, args.source)
    else:
        cands = [p for p in parts if p.key != dst.key and p.unarchived]
        if not cands:
            die("no other partition has unarchived sessions")
        if len(cands) > 1:
            die("several sources qualify; pick one with --from: " + ", ".join(p.name for p in cands))
        src = cands[0]

    if src.key == dst.key:
        die("source and destination are the same partition")
    if src.account != dst.account and not args.allow_cross_account:
        die(
            f"different accounts ({src.account[:8]} vs {dst.account[:8]}). "
            f"Pass --allow-cross-account if you really mean it."
        )

    known = connector_names(parts)
    pool = resolve_sessions(src, args.session) if args.session else src.sessions
    plan = []
    for s in pool:
        if s.archived and not args.include_archived:
            plan.append((s, SKIP_ARCHIVED, []))
        elif (dst.path / f"{s.session_id}.json").exists():
            plan.append((s, SKIP_EXISTS, []))
        elif s.uuid in dst.tombstones:
            plan.append((s, SKIP_TOMB, []))
        elif not s.transcript:
            plan.append((s, SKIP_NO_TRANSCRIPT, []))
        else:
            _, notes = port_session(s, dst, args.keep_error, known)
            plan.append((s, COPY, notes))
    plan.sort(key=lambda x: (x[1] != COPY, -x[0].last_activity))
    todo = [(s, n) for s, st, n in plan if st == COPY]

    # ---- direction diagram ----
    left, right = partition_card(src, "SOURCE"), partition_card(dst, "TARGET")
    beam = f" {G.beam * 3} {len(todo)} {G.beam * 3}{G.arrow} "
    joiner = [" " * len(beam)] * len(left)
    joiner[len(left) // 2] = beam
    print()
    print(side_by_side(left, right, joiner))
    print()

    # ---- direction sanity ----
    notes = []
    if not dst.signed_in:
        who = next((p.name for p in parts if p.signed_in), "(none)")
        notes.append(
            f"destination is NOT the signed-in partition - copies stay invisible until you "
            f"sign into org {dst.short}. Signed in right now: {who}."
        )
    if src.headroom is not None and dst.headroom is not None and dst.headroom < src.headroom:
        notes.append(
            f"{dst.name} has less quota left ({dst.headroom}%) than {src.name} ({src.headroom}%) - "
            f"this moves work toward the more exhausted plan."
        )
    if any(s.is_remote for s, _ in todo):
        notes.append("some sessions are ssh/remote; they resume only if that host is reachable.")
    for name, tally in dst.contested_connectors.items():
        winner, count = max(tally.items(), key=lambda kv: kv[1])
        losers = sum(v for u, v in tally.items() if u != winner)
        notes.append(
            f"{dst.name} disagrees with itself about {name}: {count} session(s) say "
            f"{winner[:8]}, {losers} say otherwise. Taking the majority. This usually "
            f"means a session was copied in without the ledger recording it."
        )
    for n in notes:
        print(f"{G.warn} {n}")
    if notes:
        print()

    # ---- table ----
    rows = []
    for s, st, ns in plan:
        rows.append(
            [
                G.check if st == COPY else G.cross,
                st,
                s.uuid[:8],
                ts(s.last_activity),
                trunc(s.title, 44),
                trunc(s.branch or Path(s.cwd).name, 28),
                str(len(ns)) if st == COPY else "",
            ]
        )
    print(table([" ", "ACTION", "ID", "LAST ACTIVITY", "TITLE", "BRANCH/DIR", "FIX"], rows))

    fixes: dict[str, int] = {}
    for _, st, ns in plan:
        for n in ns:
            fixes[n] = fixes.get(n, 0) + 1
    if fixes:
        print("\nport fixes applied (FIX column counts these per session):")
        for n, c in sorted(fixes.items(), key=lambda x: -x[1]):
            print(f"  {c:>2}x  {n}")

    counts: dict[str, int] = {}
    for _, st, _ in plan:
        counts[st] = counts.get(st, 0) + 1
    print("\n" + "   ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    print("transcripts are shared on disk - none are copied or duplicated.")

    if not args.apply:
        print(f"\nDRY RUN. Nothing written. Re-run with --apply to copy {len(todo)} session(s).")
        return 0
    if not todo:
        print("\nNothing to do.")
        return 0

    dst.path.mkdir(parents=True, exist_ok=True)
    written, ledger = 0, []
    for s, _ in todo:
        target = dst.path / f"{s.session_id}.json"
        payload, _ = port_session(s, dst, args.keep_error, known)
        try:
            # exclusive create: cannot clobber an existing session, ever
            with open(target, "x", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            written += 1
            ledger.append(
                {"uuid": s.uuid, "src": src.org, "dest": dst.org, "at": int(time.time() * 1000)}
            )
            print(f"{G.check} {s.uuid[:8]}  {trunc(s.title, 60)}")
        except FileExistsError:
            print(f"{G.cross} {s.uuid[:8]}  appeared in destination mid-run, left alone")
        except OSError as exc:
            print(f"{G.cross} {s.uuid[:8]}  {exc}")
    if ledger:
        record_copies(ledger)
    print(f"\nCopied {written} session(s) into {dst.name} ({dst.org}).")
    print("The app caches its session list in memory. Switch accounts in the app to")
    print("force a reload - faster than restarting it, and it works just as well.")
    return 0


# ---------------------------------------------------------------------------
# adopt: make a WSL CLI session visible to the Windows desktop app
# ---------------------------------------------------------------------------


def adopt_template(dst: Partition) -> dict | None:
    """
    The most recent natively-created session in `dst`, used as a shape donor.

    Cloning a real session is what makes an adopted one work: it inherits this
    org's remoteMcpServersConfig and enabledMcpTools already correct, plus
    whatever defaults this build of the app expects. Sessions this tool wrote
    are excluded - they carry another org's connector uuids.
    """
    native = [
        s for s in dst.sessions
        if s.uuid not in dst.imported and not s.is_wsl and s.servers
    ]
    if not native:
        native = [s for s in dst.sessions if s.uuid not in dst.imported]
    if not native:
        return None
    donor = max(native, key=lambda s: s.last_activity)
    return json.loads(json.dumps(donor.data))


def build_adopted(
    c: CliSession,
    template: dict | None,
    dst: Partition | None = None,
    known: dict[str, str] | None = None,
) -> tuple[dict, list[str]]:
    """Desktop metadata that points the app at a transcript inside WSL."""
    notes: list[str] = []
    if template is None:
        data: dict = {
            "model": "claude-opus-5",
            "effort": "high",
            "permissionMode": "auto",
            "alwaysAllowedReasons": [],
            "sessionPermissionUpdates": [],
            "spawnSeed": {},
        }
        notes.append("no session to clone in destination; MCP config omitted (app will populate)")
    else:
        data = template
        for f in PLACEMENT_FIELDS + VOLATILE_FIELDS + ERROR_FIELDS + ("sessionSettings",):
            data.pop(f, None)
        if data.get("remoteMcpServersConfig"):
            notes.append("inherited destination org's connectors")
        # the donor is a real session in dst, but its tool keys can still name
        # another org's connector uuids - the app does not prune them
        if dst is not None:
            normalize_enabled_tools(data, dst.connectors, known or {}, notes)

    # Never inherit the donor's permission posture. A session this tool created
    # must not silently arrive pre-authorised to skip tool approval, whatever
    # the session it was cloned from happened to be set to.
    if data.get("permissionMode") not in (None, "auto"):
        notes.append(f"reset permissionMode {data['permissionMode']} {G.arrow} auto")
    data["permissionMode"] = "auto"
    data["spawnSeed"] = {}

    host = c.host
    data.update(
        {
            "sessionId": c.session_id,
            "cliSessionId": c.cli_id,
            "cwd": c.cwd,
            "originCwd": c.origin_cwd or c.cwd,
            "createdAt": c.created_at or c.last_activity,
            "lastActivityAt": c.last_activity,
            "lastFocusedAt": c.last_activity,
            "title": c.title,
            "titleSource": "auto",
            "isArchived": False,
            "wslConfig": {"distro": host.distro},
            "sshRemoteTranscriptPath": host.posix_transcript_path(c.project_dir, c.cli_id),
        }
    )
    if c.moved:
        notes.append(f"session moved during its life; resumes in {c.cwd}")
    if c.branch:
        data["branch"] = c.branch
    if c.last_prompt:
        data["promptSuggestion"] = c.last_prompt.strip().splitlines()[0][:200]
    if c.turns:
        data["completedTurns"] = c.turns
    notes.append(f"points at {host.name} transcript, which is not copied")
    return data, notes


ADOPT = "ADOPT"
SKIP_ADOPTED = "skip: already adopted"


def host_card(h: Host, role: str, extra: list[str], width: int = 34) -> list[str]:
    lines = [role, G.h * width, h.name, f"home {h.posix_home or Path.home()}", ""] + extra
    return box([trunc(l, width) for l in lines], width)


def cmd_adopt(args) -> int:
    host = resolve_host(args.source or "wsl")
    if not host.is_wsl:
        die(
            "adopt imports from a WSL distro. Windows sessions already have desktop "
            "metadata - to move one between orgs use `copy`, or to hand it to the CLI "
            "inside WSL use `eject`."
        )
    parts = load_partitions()
    dst = resolve_partition(parts, args.to or "active")

    already: dict[str, str] = {}
    for p in parts:
        for s in p.sessions:
            if s.is_wsl and s.wsl_distro == host.distro:
                already.setdefault(s.cli_session_id, p.name)

    pool = scan_cli_sessions(host)
    if args.session:
        pool = resolve_cli_sessions(pool, args.session)
    template = adopt_template(dst)
    known = connector_names(parts)

    plan = []
    for c in pool:
        if (dst.path / f"{c.session_id}.json").exists() or already.get(c.cli_id) == dst.name:
            plan.append((c, SKIP_ADOPTED, []))
        elif c.uuid in dst.tombstones:
            plan.append((c, SKIP_TOMB, []))
        elif not c.transcript.exists():
            plan.append((c, SKIP_NO_TRANSCRIPT, []))
        else:
            _, notes = build_adopted(c, json.loads(json.dumps(template)) if template else None, dst, known)
            plan.append((c, ADOPT, notes))
    plan.sort(key=lambda x: (x[1] != ADOPT, -x[0].last_activity))
    todo = [(c, n) for c, st, n in plan if st == ADOPT]

    left = host_card(
        host,
        "SOURCE (no metadata)",
        [
            f"{len(pool)} CLI sessions",
            f"{sum(1 for c in pool if c.cli_id in already)} already adopted",
            "",
            "invisible to the app until adopted",
        ],
    )
    right = partition_card(dst, "TARGET")
    beam = f" {G.beam * 3} {len(todo)} {G.beam * 3}{G.arrow} "
    joiner = [" " * len(beam)] * max(len(left), len(right))
    joiner[min(len(left), len(right)) // 2] = beam
    print()
    print(side_by_side(left, right, joiner))
    print()

    if not dst.signed_in:
        who = next((p.name for p in parts if p.signed_in), "(none)")
        warn(
            f"destination is NOT the signed-in partition - adopted sessions stay invisible "
            f"until you sign into org {dst.short}. Signed in right now: {who}."
        )
    if template is None:
        warn(f"{dst.name} has no session to clone; adopted sessions get minimal metadata.")
    print()

    rows = []
    for c, st, ns in plan:
        rows.append(
            [
                G.check if st == ADOPT else G.cross,
                st,
                c.cli_id[:8],
                ts(c.last_activity),
                trunc(c.title, 40),
                trunc(c.cwd, 34),
            ]
        )
    print(table([" ", "ACTION", "CLI ID", "LAST ACTIVITY", "TITLE", "CWD (inside WSL)"], rows))

    fixes: dict[str, int] = {}
    for _, st, ns in plan:
        for n in ns:
            fixes[n] = fixes.get(n, 0) + 1
    if fixes:
        print("\nmetadata written for each adopted session:")
        for n, cnt in sorted(fixes.items(), key=lambda x: -x[1]):
            print(f"  {cnt:>2}x  {n}")

    counts: dict[str, int] = {}
    for _, st, _ in plan:
        counts[st] = counts.get(st, 0) + 1
    print("\n" + "   ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    print(f"transcripts stay in {host.name} - nothing is copied, rewritten, or duplicated.")

    if not args.apply:
        print(f"\nDRY RUN. Nothing written. Re-run with --apply to adopt {len(todo)} session(s).")
        return 0
    if not todo:
        print("\nNothing to do.")
        return 0

    dst.path.mkdir(parents=True, exist_ok=True)
    written, ledger = 0, []
    for c, _ in todo:
        target = dst.path / f"{c.session_id}.json"
        payload, _ = build_adopted(c, json.loads(json.dumps(template)) if template else None, dst, known)
        try:
            with open(target, "x", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            written += 1
            ledger.append(
                {
                    "uuid": c.uuid,
                    "src": host.name,
                    "dest": dst.org,
                    "cliSessionId": c.cli_id,
                    "at": int(time.time() * 1000),
                }
            )
            print(f"{G.check} {c.cli_id[:8]}  {trunc(c.title, 60)}")
        except FileExistsError:
            print(f"{G.cross} {c.cli_id[:8]}  appeared in destination mid-run, left alone")
        except OSError as exc:
            print(f"{G.cross} {c.cli_id[:8]}  {exc}")
    if ledger:
        record_copies(ledger)
    print(f"\nAdopted {written} session(s) into {dst.name} ({dst.org}).")
    print("The app caches its session list in memory. Switch accounts in the app to")
    print("force a reload - faster than restarting it, and it works just as well.")
    return 0


# ---------------------------------------------------------------------------
# eject: hand a desktop session back to the CLI inside WSL
# ---------------------------------------------------------------------------


def rewrite_cwd(src: Path, dst_path: Path, new_cwd: str) -> tuple[int, int]:
    """
    Copy a transcript, rewriting the top-level `cwd` on every record.

    Only that one key is touched. Paths quoted inside the conversation are
    history - rewriting them would corrupt tool results that legitimately
    describe a Windows filesystem.
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    lines, touched = 0, 0
    with src.open(encoding="utf-8", errors="replace") as fin, open(
        dst_path, "x", encoding="utf-8", newline="\n"
    ) as fout:
        for line in fin:
            lines += 1
            if '"cwd"' in line:
                rec = _loads(line)
                if rec and rec.get("cwd"):
                    rec["cwd"] = new_cwd
                    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    touched += 1
                    continue
            fout.write(line if line.endswith("\n") else line + "\n")
    return lines, touched


def cmd_eject(args) -> int:
    parts = load_partitions()
    src_part = resolve_partition(parts, args.partition or "active")
    matches = resolve_sessions(src_part, [args.session])
    sess = matches[0]
    host = resolve_host(args.to or "wsl")

    print(f"\n{sess.uuid[:8]}  {sess.title}")
    print(f"cwd  {sess.cwd}\n")

    if sess.is_wsl:
        posix_cwd = sess.cwd
        print(f"{G.check} already a {host.name} session - its transcript never left the distro.")
        print(f"\nResume it from the CLI inside WSL:\n")
        print(f"  wsl -d {sess.wsl_distro} --cd {posix_cwd} -e claude --resume {sess.cli_session_id}")
        return 0

    if not sess.transcript:
        die("this session has no transcript on disk - there is nothing to hand over")

    posix_cwd = win_to_wsl_path(sess.cwd)
    if not posix_cwd:
        die(
            f"cannot express {sess.cwd!r} as a path inside {host.name}. Only drive paths "
            f"(C:\\...) are visible to WSL, at /mnt/<drive>/..."
        )

    target = host.transcript_path(posix_cwd, sess.cli_session_id)
    workdir = Path(sess.cwd)
    for k, v in (
        ("source transcript", str(sess.transcript)),
        ("target transcript", str(target)),
        ("cwd inside WSL", posix_cwd),
        ("working dir exists", G.check if workdir.is_dir() else f"{G.cross} not found"),
        ("already there", G.check if target.exists() else "no"),
    ):
        print(f"  {k:<19} {v}")
    print()
    warn(
        f"{posix_cwd} is the same files as {sess.cwd}, reached over the drvfs mount. "
        "It is slower than a native Linux checkout, and line endings and file modes "
        "are the Windows ones."
    )
    warn("the desktop app keeps its own copy; edits made in WSL will not show up there.")

    if target.exists():
        print(f"\n{G.check} Nothing to do - the CLI in {host.name} can already see this session.")
        print(f"\n  wsl -d {host.distro} --cd {posix_cwd} -e claude --resume {sess.cli_session_id}")
        return 0

    if not args.apply:
        print(f"\nDRY RUN. Nothing written. Re-run with --apply to copy the transcript into {host.name}.")
        return 0

    try:
        lines, touched = rewrite_cwd(sess.transcript, target, posix_cwd)
    except FileExistsError:
        die("target transcript appeared mid-run; left alone")
    except OSError as exc:
        die(f"could not write {target}: {exc}")
    print(f"\n{G.check} wrote {target}")
    print(f"  {lines} records, {touched} cwd fields rewritten to {posix_cwd}")
    print(f"\nResume it from the CLI inside WSL:\n")
    print(f"  wsl -d {host.distro} --cd {posix_cwd} -e claude --resume {sess.cli_session_id}")
    print("\nThe Windows session is untouched and still works. They are now two")
    print("independent continuations of the same history - do not run both at once.")
    return 0


SKILL_MD = '''---
name: claude-session-teleporter
description: Find and move Claude Code sessions between account/org partitions, and between WSL and the Windows desktop app. Use when the user says sessions are "missing", "gone", or "not showing up" after switching orgs or accounts, asks where Claude Code stores sessions, wants to continue a session from their other org, wants a session they started in WSL to show up in the Claude app on Windows (or the reverse), or hits a rate limit on one org and wants their work available in another.
user-invocable: true
allowed-tools:
  - Bash(python *)
  - Read
---

# Claude Session Teleporter

`{tool}` moves Claude Code sessions along two independent axes.

| axis | what differs | command |
|---|---|---|
| **partition** | account/org, same machine, same transcripts | `copy` |
| **host** | WSL vs Windows - different filesystem, different Claude Code install | `adopt` / `eject` |

Diagnose which axis before running anything. They fail in different ways and
the fixes do not substitute for each other.

## When this applies

**Partition axis.** The user signed into a different org (or account) and their
sessions vanished from the app. **The sessions are not lost.** The app shows
only the partition it is signed into; everything else is still on disk. You will
hear: sessions missing after an org switch, "where are my sessions stored", "are
they in the cloud", wanting work from the other org, or quota exhausted on one
org while work sits in another.

**Host axis.** The user ran `claude` inside WSL and the Windows app cannot see
those sessions *in any org*. That is not a partition problem - a WSL session has
no desktop metadata at all, so no amount of signing in will reveal it. You will
hear: "I started this in WSL", "my Ubuntu sessions aren't in the app", or a wish
to keep going in the app rather than the terminal.

## Mental model

1. **Everything is local.** Nothing is in the cloud, nothing syncs between
   machines. Metadata: `%APPDATA%/Claude/claude-code-sessions/<account>/<org>/local_<id>.json`.
2. **Metadata is partitioned by account and org; transcripts are not.**
   Transcripts sit in `~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl`,
   shared by every partition. `copy` moves one small JSON file - the
   conversation never moves and is never duplicated.
3. **WSL is a whole separate world.** The distro has its own `~/.claude` with
   its own transcripts and *no* metadata directory. The bridge is three fields
   the desktop app understands: `wslConfig: {"distro": ...}`, a `cwd` spelled
   the way the distro sees it, and `sshRemoteTranscriptPath` pointing at the
   transcript still inside WSL. `adopt` writes them.
4. **The app caches its session index in memory.** No filesystem watcher, no
   reload hook, so after any write nothing appears until the app re-reads disk.
   Tell the user this every time - otherwise they conclude it failed.
   **Switching accounts in the app is enough to force the reload, and is much
   faster than restarting it.** Offer that first; restarting also works.

## Workflow

Always dry run first. Every write command is dry run by default.

```bash
python {tool} partitions              # orgs on this machine, quota left in each
python {tool} hosts                   # this machine + WSL distros, with unadopted counts
```

Then, on the partition axis:

```bash
python {tool} sessions -p <partition>       # what is over there
python {tool} copy --from <partition>       # DRY RUN
python {tool} copy --from <partition> --apply
```

Or on the host axis:

```bash
python {tool} sessions -H wsl:Ubuntu        # CLI sessions inside the distro
python {tool} adopt --from wsl:Ubuntu       # DRY RUN
python {tool} adopt --from wsl:Ubuntu -s <id> --apply    # one first
python {tool} eject <id> --to wsl:Ubuntu    # the other direction, DRY RUN
```

Selectors - partition: a label, an org-uuid prefix, or `active` / `last-active` /
`most-quota`. Host: `wsl`, `wsl:<distro>`, `windows`. Session `-s`: an id prefix
or title substring, repeatable. Label a partition once so the user stops reading
UUIDs: `python {tool} label 3c426532 work`.

Do one session first to prove the round trip before doing all of them.

## Guarantees

State these when the user asks whether it is safe. All three commands:

- Never overwrite an existing session (exclusive create; there is no `--force`).
- Never resurrect something deleted in the destination (`deleted_<id>` tombstones).
- Never modify or remove anything in the source.
- Dry run by default.

`copy` additionally never duplicates transcripts and refuses sessions whose
transcript is missing, and refuses cross-account copies without
`--allow-cross-account`. `adopt` never writes into the distro, and derives the
desktop uuid from the WSL session id so re-running is idempotent rather than
duplicating.

**`eject` is the one exception: it does write a second transcript.** The Windows
session keeps its own. Say this out loud - after eject there are two independent
continuations of one history, and the user must not run both at once.

## The non-obvious parts

**Connector uuids are org-scoped.** A plain `cp` of metadata produces a broken
session: `remoteMcpServersConfig` gives the *same* connector a different UUID in
each org, and `enabledMcpTools` is keyed `"<serverUuid>:<toolName>"`. The tool
remaps by connector name and drops what the destination lacks. Tool keys can
even name an org the session itself no longer references - the app does not
prune them - so they are repaired against every connector uuid on the machine,
not just the ones in that session. Never hand-copy with `cp` or `Copy-Item`.

**A session's project directory is not always its starting directory.** If work
moved - into a git worktree, or into a different repo entirely - Claude Code
re-homes the transcript, so the directory names where it *ended up*. Locate a
transcript by the directory it actually sits in, never by re-encoding a `cwd`
read out of it. The tool sets `cwd` to where it ended and `originCwd` to where
it began, and says so in the dry run.

**`adopt` clones a real session from the destination org** to inherit that org's
connectors and whatever defaults the current app build expects. It resets
`permissionMode` to `auto` regardless of the donor: a session this tool created
must never arrive pre-authorised to skip tool approval.

**Two different things leave transcripts in the same WSL directory.** A session
the user started by running `claude` at a terminal inside the distro, and a
session they started in the Windows app that merely *uses* the distro as its
environment. Do not conflate them - the user cares about the difference. The
`entrypoint` field on transcript records is the discriminator: `cli` versus
`claude-desktop`, shown as the ORIGIN column.

```bash
python {tool} sessions -H wsl:Ubuntu --cli -n 10     # real CLI sessions only
python {tool} sessions -H wsl:Ubuntu --desktop       # app sessions running in WSL
```

Only `cli` sessions are candidates for `adopt`; the app-started ones already
have desktop metadata by definition. `cli>desktop` means it began at the
terminal and was later opened in the app too.

## Gotchas

- Flags in `sessions`: `A` archived, `!` transcript missing, `R` ssh/remote,
  `W` runs in WSL.
- `eject` only works if the working directory is reachable from the distro.
  `C:\\...` is visible there as `/mnt/c/...` - the same files over drvfs, slower,
  with Windows line endings and file modes. Anything else has no WSL spelling
  and the tool refuses.
- A session already flagged `W` needs no eject; its transcript never left the
  distro. `eject` just prints the `wsl -d ... --cd ... -e claude --resume` line.
- The destination org needs at least one native session before a connector map
  can be built. With none, MCP config is stripped and the app repopulates it.
- `~/.claude/session-copy-ledger.json` records imports so previously copied
  sessions do not poison the connector map. Do not hand-edit it.
- Formats are reverse-engineered and Anthropic can change them. If output looks
  wrong, inspect a session JSON with `Read` before acting.
'''


GUIDE = """\
{h}
 CLAUDE SESSION TELEPORTER - WALKTHROUGH
{h}

THE PROBLEM

  You signed into a different org and your sessions vanished. They are not
  gone. Claude Code stores sessions on local disk, partitioned by account AND
  org, and the desktop app shows you only the partition you are signed into.
  Everything else is sitting there, invisible.

  Nothing is in the cloud. Nothing syncs between machines.

HOW IT IS STORED

  metadata    %APPDATA%/Claude/claude-code-sessions/<account>/<org>/local_<id>.json
  transcript  ~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl
  tombstone   .../<org>/deleted_<id>

  Metadata is partitioned. Transcripts are NOT - every partition shares one
  pool. So teleporting a session copies one small JSON file. The conversation
  itself never moves and is never duplicated.

STEP 1 - SEE WHAT YOU HAVE

  $ {tool} partitions

  One row per account/org partition. The one marked {dot} is where you are
  signed in. QUOTA LEFT is parsed from your plan usage history and is usually
  the reason you switched orgs in the first place.

STEP 2 - NAME THEM

  $ {tool} label 3c426532 work

  Now "work" works anywhere a selector is accepted, instead of a uuid prefix.

STEP 3 - LOOK AT WHAT IS OVER THERE

  $ {tool} sessions -p work

  Unarchived only, newest first. Flags in the FLG column:
     A   archived
     !   transcript missing - cannot be continued
     R   ssh/remote - resumes only if that host is reachable

STEP 4 - DRY RUN

  $ {tool} copy --from work

  Prints a direction diagram, the per-session plan, and every fix it would
  apply. Writes nothing. Read it. The FIX column counts the repairs a naive
  copy would have skipped.

  Destination defaults to the partition you are signed into, which is almost
  always what you want - that is the only one the app can show you.

STEP 5 - DO IT

  $ {tool} copy --from work -s 5651 --apply     # one session first
  $ {tool} copy --from work --apply             # then the rest

  Copy one and confirm the round trip before moving everything.

STEP 6 - MAKE THE APP RE-READ DISK

  This is not optional. The app caches its session index in memory. There is
  no filesystem watcher and no reload hook, so nothing you copied will appear
  until it reads disk again. If it seems like the copy failed, this is why.

  Two ways to force it:

    switch accounts   in the app - fastest, and you land in the partition you
                      just copied into anyway
    restart Claude    the blunt instrument, works the same

  Prefer switching accounts. It is quicker and loses nothing.

WHY NOT JUST cp

  Session metadata is not org-portable. The same MCP connector has a DIFFERENT
  uuid in each org, and enabledMcpTools is keyed "<serverUuid>:<toolName>".
  Copy the file as-is and the session lands pointing at connectors that do not
  exist where it landed. This remaps them by name and drops what the
  destination org does not have.

WHAT CANNOT GO WRONG

  Never overwrites an existing session - exclusive create, and there is no
  --force flag by design. Never resurrects something you deleted in the
  destination. Never modifies the source. Never duplicates transcripts.
  Refuses sessions with no transcript. Dry run unless you pass --apply.

THE OTHER AXIS - WSL

  Everything above moves a session between ORGS on one machine. WSL is a
  different problem with a different fix.

  A `claude` running inside a WSL distro keeps its own ~/.claude/projects
  inside that distro, and writes NO desktop metadata at all. So those sessions
  are invisible to the Windows app no matter which org you sign into. Signing
  around will never find them, because there is nothing to find.

  $ {tool} hosts

  One row per place transcripts live: this machine, and each distro with a
  Claude Code install. UNADOPTED counts sessions the app cannot see at all.

  $ {tool} sessions -H wsl:Ubuntu

  What is in there. Rows marked + have no desktop metadata yet.

  $ {tool} adopt --from wsl:Ubuntu             DRY RUN
  $ {tool} adopt --from wsl:Ubuntu -s 3f81 --apply

  This writes only metadata. The transcript stays inside the distro and is
  never copied or rewritten - the app runs `claude` in WSL against the file
  that is already there, so the terminal and the app are the SAME session
  rather than two forks of it. Three fields do the work:

      wslConfig                {"distro": "Ubuntu"}
      cwd                      the path as the distro sees it
      sshRemoteTranscriptPath  the transcript, still inside the distro

  Then make the app re-read disk, same as always - switch accounts, or
  restart it.

GOING THE OTHER WAY

  $ {tool} eject 8aef --to wsl:Ubuntu

  Takes a Windows session and puts its transcript where the WSL CLI will find
  it. This only works when the working directory is reachable from the distro:
  C:\\Users\\me\\repo is visible there as /mnt/c/Users/me/repo, the same files
  over the drvfs mount. Anything else has no spelling inside WSL and is
  refused.

  Unlike copy and adopt, this one FORKS. A second transcript is written and
  the Windows session keeps its own. Resume in one place or the other, never
  both at once.

  A session already flagged W needs none of this - its transcript never left
  the distro. eject just prints the resume command.

WHEN IT LOOKS WRONG

  $ {tool} active            three readings of which partition is "active"
  $ {tool} partitions        confirm connectors were detected in both orgs
  $ {tool} hosts             confirm the distro is visible at all
  $ CLAUDE_SESSIONS_ROOT=... override store detection

  Formats here are reverse-engineered and can change without notice.

  $ {tool} skill --install   teach Claude Code to drive this for you
{h}
"""


def cmd_guide(args) -> int:
    tool = Path(sys.argv[0]).name or "claude_sessions.py"
    print(GUIDE.replace("{h}", G.h * 74).replace("{tool}", tool).replace("{dot}", G.dot), end="")
    return 0


def cmd_skill(args) -> int:
    tool = Path(sys.argv[0]).name or "claude_sessions.py"
    body = SKILL_MD.replace("{tool}", tool)
    if not args.install:
        print(body, end="")
        return 0
    target = Path(args.path) if args.path else SKILL_DIR
    target.mkdir(parents=True, exist_ok=True)
    dest = target / "SKILL.md"
    existed = dest.exists()
    dest.write_text(body, encoding="utf-8")
    print(f"{G.check} {'replaced' if existed else 'installed'} {dest}")
    print("Restart Claude Code (or start a new session) to pick up the skill.")
    return 0


def cmd_label(args) -> int:
    p = resolve_partition(load_partitions(), args.partition)
    labels = _read_json(LABELS_PATH, {})
    labels[p.org] = args.name
    _write_json(LABELS_PATH, labels)
    print(f"{G.check} {p.org} labelled {args.name!r}  ({LABELS_PATH})")
    return 0


# ---------------------------------------------------------------------------


DESCRIPTION = """\
Teleport Claude Code desktop sessions between account/org partitions.

The desktop app shows only the partition you are signed into, so sessions from
another org look missing even though they are on disk. This finds them and
copies them into the partition you are signed into.

Session metadata is partitioned by account and org; transcripts are shared, so
a copy moves one small JSON file and never duplicates a conversation.
"""

EPILOG = """\
examples:
  claude_sessions.py partitions                    what partitions exist, and quota left
  claude_sessions.py label 3c426532 work           stop reading UUIDs
  claude_sessions.py sessions -p work              unarchived sessions over there
  claude_sessions.py active                        which partition is "active", three ways
  claude_sessions.py copy --from work              DRY RUN of the whole migration
  claude_sessions.py copy --from work -s 5651 --apply    copy one, for real

  claude_sessions.py hosts                         this machine, plus every WSL distro
  claude_sessions.py sessions -H wsl:Ubuntu        CLI sessions inside the distro
  claude_sessions.py adopt --from wsl:Ubuntu       DRY RUN: make them visible in the app
  claude_sessions.py eject 8aef --to wsl:Ubuntu    hand a Windows session to the WSL CLI

  claude_sessions.py skill --install               teach Claude Code to drive this

selectors:
  partition   a label, an org-uuid prefix, or one of: active, last-active, most-quota
  host        windows, wsl, or wsl:<distro>
  session     an id prefix or a title substring (-s is repeatable)

two independent axes:
  partitions  same machine, same transcripts, different account/org -> copy
  hosts       different filesystem and different Claude Code install -> adopt/eject

note:
  The app caches its session index in memory, so copies do not appear until it
  re-reads disk. Switching accounts in the app forces that, and is faster than
  restarting it. Restarting works too.

storage:
  metadata    %APPDATA%/Claude/claude-code-sessions/<accountUuid>/<orgUuid>/local_<id>.json
  transcript  ~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl
  in WSL      \\\\wsl.localhost\\<distro>\\home\\<user>\\.claude\\projects\\...  (no metadata)

Override store detection with the CLAUDE_SESSIONS_ROOT environment variable.
https://github.com/aviadr1/claude-session-teleporter
"""

COPY_EPILOG = """\
safety:
  Never overwrites an existing session - exclusive create, and there is
  deliberately no --force flag. Never resurrects a session deleted in the
  destination. Never modifies the source. Never duplicates transcripts.
  Refuses sessions whose transcript is missing. Dry run unless --apply.

port fixes:
  Metadata is not org-portable as-is: the same MCP connector has a different
  uuid in each org, and enabledMcpTools is keyed by that uuid. Connectors are
  remapped by name, ones the destination lacks are dropped, and stale crash and
  ssh state is cleared. The dry run prints every fix before you commit to it.
"""

ADOPT_EPILOG = """\
how it works:
  A WSL session has a transcript inside the distro and no desktop metadata at
  all, so no org you sign into will ever show it. adopt writes the missing
  metadata, cloned from a real session in the destination org so this org's MCP
  connectors come out right, with three fields that point it at WSL:

    wslConfig                {"distro": "Ubuntu"}
    cwd                      the path as the distro sees it
    sshRemoteTranscriptPath  the transcript, still inside the distro

  The transcript is not copied, moved, or rewritten. The app runs `claude`
  inside the distro against the file that is already there, so the CLI and the
  app are the same session - not two forks of it.

safety:
  Never overwrites an existing session, never resurrects a tombstone, never
  writes anything into the distro. The desktop uuid is derived from the WSL
  session id, so re-running adopt is idempotent rather than duplicating.

  The app caches its session index in memory, so adopted sessions appear only
  once it re-reads disk. Switch accounts in the app to force that - faster than
  a restart, which also works.
"""

EJECT_EPILOG = """\
the constraint:
  A WSL CLI can only open the session if its working directory exists inside
  the distro. A Windows path C:\\Users\\me\\repo is visible there as
  /mnt/c/Users/me/repo - the same files, over the drvfs mount - so eject
  rewrites the cwd on every record and drops the transcript into the project
  directory that spelling produces. Paths quoted inside the conversation are
  left alone: they are history, and describe a filesystem that really was
  Windows at the time.

  Sessions already flagged W need none of this. Their transcript never left
  the distro, so eject just prints the resume command.

this one does fork:
  Unlike copy and adopt, eject writes a second transcript. The Windows session
  keeps its own. Resume in one place or the other, not both at once.
"""


def main(argv: list[str] | None = None) -> int:
    fmt = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(
        prog="claude_sessions.py",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=fmt,
    )
    ap.add_argument("--ascii", action="store_true", help="force plain ASCII output")
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    pp = sub.add_parser(
        "partitions",
        help="list account/org partitions",
        description="List every account/org partition on this machine, with session counts, "
        "plan quota remaining, and the MCP connectors each org knows about. "
        "The signed-in partition is marked.",
        formatter_class=fmt,
    )
    pp.set_defaults(fn=cmd_partitions)

    sp = sub.add_parser(
        "sessions",
        help="list sessions in a partition",
        description="List sessions, unarchived only by default. Flags: A=archived, "
        "!=transcript missing, R=ssh/remote.",
        formatter_class=fmt,
    )
    sp.add_argument("-p", "--partition", metavar="SEL", help="label, org uuid prefix, or 'active'")
    sp.add_argument(
        "-H", "--host", metavar="HOST",
        help="list CLI sessions on another host instead: 'wsl', 'wsl:Ubuntu', 'windows'",
    )
    sp.add_argument("-a", "--all", action="store_true", help="include archived sessions")
    sp.add_argument("-n", "--limit", type=int, metavar="N", help="show only the N most recent")
    origin = sp.add_mutually_exclusive_group()
    origin.add_argument(
        "--cli", action="store_true",
        help="with -H: only sessions you started with `claude` inside the distro",
    )
    origin.add_argument(
        "--desktop", action="store_true",
        help="with -H: only sessions the Windows app started, using the distro as environment",
    )
    sp.set_defaults(fn=cmd_sessions)

    hp = sub.add_parser(
        "hosts",
        help="list this machine and every WSL distro with Claude Code",
        description="List every host whose transcripts this tool can reach: this machine, plus "
        "each WSL distro that has a ~/.claude/projects. WSL sessions have no desktop metadata, "
        "so the UNADOPTED column counts sessions the app cannot see at all.",
        formatter_class=fmt,
    )
    hp.set_defaults(fn=cmd_hosts)

    ap_ = sub.add_parser(
        "active",
        help='explain which partition is "active"',
        description='"Active" is ambiguous, so all three readings are reported: the signed-in '
        "partition (authoritative), the most recently used, and the one with the most plan "
        "quota left. copy defaults to the signed-in one.",
        formatter_class=fmt,
    )
    ap_.set_defaults(fn=cmd_active)

    cp = sub.add_parser(
        "copy",
        help="copy sessions between partitions (dry run by default)",
        description="Copy sessions from one partition into another, remapping org-scoped "
        "connector uuids on the way. Prints a plan and exits unless --apply is given.",
        epilog=COPY_EPILOG,
        formatter_class=fmt,
    )
    cp.add_argument("--from", dest="source", metavar="SEL", help="source partition (default: the only other one with work)")
    cp.add_argument("--to", metavar="SEL", help="destination partition (default: the signed-in one)")
    cp.add_argument("-s", "--session", action="append", default=[], metavar="SEL", help="id prefix or title substring (repeatable)")
    cp.add_argument("--include-archived", action="store_true", help="also copy archived sessions")
    cp.add_argument("--keep-error", action="store_true", help="preserve previous crash state")
    cp.add_argument("--allow-cross-account", action="store_true", help="permit copying between accounts")
    cp.add_argument("--apply", action="store_true", help="actually write (default is dry run)")
    cp.set_defaults(fn=cmd_copy)

    adp = sub.add_parser(
        "adopt",
        help="surface WSL CLI sessions in the desktop app (dry run by default)",
        description="Write desktop metadata for sessions that a Claude Code CLI created inside "
        "a WSL distro, so the Windows app can see and resume them. The transcript stays in the "
        "distro; the app runs `claude` inside WSL against it.",
        epilog=ADOPT_EPILOG,
        formatter_class=fmt,
    )
    adp.add_argument("--from", dest="source", metavar="HOST", help="source distro (default: the only one)")
    adp.add_argument("--to", metavar="SEL", help="destination partition (default: the signed-in one)")
    adp.add_argument("-s", "--session", action="append", default=[], metavar="SEL", help="cli id prefix or title substring (repeatable)")
    adp.add_argument("--apply", action="store_true", help="actually write (default is dry run)")
    adp.set_defaults(fn=cmd_adopt)

    ep = sub.add_parser(
        "eject",
        help="hand a desktop session to the CLI inside WSL (dry run by default)",
        description="Place a Windows session's transcript where a Claude Code CLI inside WSL "
        "will find it, and print the command to resume it there. Only works for sessions whose "
        "working directory is on a drive WSL can mount.",
        epilog=EJECT_EPILOG,
        formatter_class=fmt,
    )
    ep.add_argument("session", metavar="SEL", help="session id prefix or title substring")
    ep.add_argument("-p", "--partition", metavar="SEL", help="where to look for it (default: signed-in)")
    ep.add_argument("--to", metavar="HOST", help="destination distro (default: the only one)")
    ep.add_argument("--apply", action="store_true", help="actually write (default is dry run)")
    ep.set_defaults(fn=cmd_eject)

    lp = sub.add_parser(
        "label",
        help="give a partition a readable name",
        description=f"Store a human-readable name for a partition in {LABELS_PATH}, so you can "
        "refer to it by name instead of a uuid prefix.",
        formatter_class=fmt,
    )
    lp.add_argument("partition", metavar="SEL", help="label, org uuid prefix, or 'active'")
    lp.add_argument("name", help="the name to give it, e.g. work")
    lp.set_defaults(fn=cmd_label)

    gp = sub.add_parser(
        "guide",
        help="print a full walkthrough",
        description="Print a start-to-finish walkthrough: how sessions are stored, why they "
        "look missing, and the exact sequence to get them back. Read this first.",
        formatter_class=fmt,
    )
    gp.set_defaults(fn=cmd_guide)

    kp = sub.add_parser(
        "skill",
        help="print or install a Claude Code skill for this tool",
        description="Emit a SKILL.md that teaches Claude Code how and when to drive this tool. "
        "Prints to stdout by default; --install writes it where Claude Code will find it.",
        epilog=f"default install path:\n  {SKILL_DIR / 'SKILL.md'}\n",
        formatter_class=fmt,
    )
    kp.add_argument("--install", action="store_true", help="write the skill instead of printing it")
    kp.add_argument("--path", metavar="DIR", help="install into DIR instead of the default")
    kp.set_defaults(fn=cmd_skill)

    args = ap.parse_args(argv)
    global G
    G = make_glyphs(args.ascii)
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
