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
  teleport     fork conversation history between Claude and Codex
  ui           select and transfer several local sessions in a browser
  codex-project assign an imported Codex session to a desktop project
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
import hashlib
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import time
import tempfile
import threading
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
        classic = Path(appdata) / "Claude" / "claude-code-sessions"
        if classic.exists():
            return classic
        # Microsoft Store installs redirect Roaming AppData inside the package.
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            packages = Path(local_appdata) / "Packages"
            stores = list(packages.glob("Claude_*/LocalCache/Roaming/Claude/claude-code-sessions"))
            if stores:
                return max(stores, key=lambda p: p.stat().st_mtime)
        return classic
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

__version__ = "1.3.0"
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


def transcript_cwd(cwds: list[str], project_dir: str) -> str:
    """F2/F3: the store directory distinguishes a moved project from a visit."""
    return next((cwd for cwd in reversed(cwds) if encode_cwd(cwd) == project_dir),
                cwds[-1] if cwds else '')


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
    sess.cwd = transcript_cwd(cwds, sess.project_dir)
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
    if args.agent:
        return cmd_agent_sessions(args)
    if args.home:
        die('--home requires --agent claude or --agent codex')
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
description: Find and move sessions between Claude and Codex, across Claude account/org partitions, and between WSL and the Windows desktop app. Use when the user says sessions are "missing", "gone", or "not showing up" after switching orgs or accounts, asks where Claude Code stores sessions, wants to continue a session from their other org, wants a session they started in WSL to show up in the Claude app on Windows (or the reverse), or hits a rate limit on one org and wants their work available in another.
user-invocable: true
allowed-tools:
  - Bash({allow} *)
  - Read
---

# Claude Session Teleporter

`{tool}` moves sessions along three independent axes.

| axis | what differs | command |
|---|---|---|
| **client** | Claude vs Codex, different transcript formats | `teleport` |
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
{run} partitions              # orgs on this machine, quota left in each
{run} hosts                   # this machine + WSL distros, with unadopted counts
```

Then, on the partition axis:

```bash
{run} sessions -p <partition>       # what is over there
{run} copy --from <partition>       # DRY RUN
{run} copy --from <partition> --apply
```

Or on the host axis:

```bash
{run} sessions -H wsl:Ubuntu        # CLI sessions inside the distro
{run} adopt --from wsl:Ubuntu       # DRY RUN
{run} adopt --from wsl:Ubuntu -s <id> --apply    # one first
{run} eject <id> --to wsl:Ubuntu    # the other direction, DRY RUN
```

Selectors - partition: a label, an org-uuid prefix, or `active` / `last-active` /
`most-quota`. Host: `wsl`, `wsl:<distro>`, `windows`. Session `-s`: an id prefix
or title substring, repeatable. Label a partition once so the user stops reading
UUIDs: `{run} label 3c426532 work`.

Do one session first to prove the round trip before doing all of them.

## Claude ↔ Codex

Use `{run} sessions --agent claude` or `--agent codex` to find transcript paths.
Then `{run} teleport /path/to/transcript.jsonl --to codex` (or `--to claude`).
Dry-run first; `--apply` creates an independent fork. For Claude desktop include
`--desktop-partition active` on the initial import. For another store use
`--target-home`; for another working directory use `--cwd`.
Codex imports default to `--codex-project auto`: reuse an exact destination
project root or create one. Ambiguous matches require `--codex-project ID`.
Use the desktop host's session home, optional `--codex-sqlite-home`, and a recent
`--codex-bin` with project APIs. An existing active import can be assigned with
`{run} codex-project /path/to/rollout.jsonl --apply`; this does not reimport history.
Dry runs never start Codex. Use `--codex-project none` for transcript-only imports
without a Codex executable; these preserve cwd but not project membership.

Inspect the conversion notices: completed tools retain native call/result
structure; pending or ambiguous exchanges are rejected. Both round trips retain
supported content using checked conversion metadata. Private reasoning and source
instructions are omitted; unsupported media gets placeholders. Compacted sessions
transfer active context, not every historical turn, file or running process.
Resume the printed ID with the destination CLI or refresh the desktop app.

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

**`eject` and cross-client `teleport` write a second transcript.** The Windows
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
{run} sessions -H wsl:Ubuntu --cli -n 10     # real CLI sessions only
{run} sessions -H wsl:Ubuntu --desktop       # app sessions running in WSL
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
    print("Claude <-> Codex: use sessions --agent claude (or codex) to find a transcript.")
    print(f"  {tool} teleport /path/to/transcript.jsonl --to codex   # dry run")
    print("Use --to claude for the reverse; --apply writes a new independent fork.")
    print("For Claude desktop add --desktop-partition active on the initial import.\n")
    print(GUIDE.replace("{h}", G.h * 74).replace("{tool}", tool).replace("{dot}", G.dot), end="")
    return 0


def skill_invocation() -> str:
    """
    The command the skill tells Claude to run. Claude runs it from whatever
    project it is in, so it cannot depend on the current directory: a script is
    named by its absolute path, and an installed console script, which is on
    PATH, by its name alone.
    """
    argv0 = Path(sys.argv[0])
    if argv0.suffix == ".py":
        return f"python {shlex.quote(argv0.resolve().as_posix())}"
    return argv0.stem or "claude-sessions"


def cmd_skill(args) -> int:
    tool = Path(sys.argv[0]).name or "claude_sessions.py"
    run = skill_invocation()
    body = (
        SKILL_MD.replace("{run}", run)
        .replace("{allow}", run.split()[0])
        .replace("{tool}", tool)
    )
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
Teleport Claude Code sessions across partitions/hosts, and between Claude and Codex.

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


# ---------------------------------------------------------------------------
# cross-client teleport: a fresh, resumable fork of portable conversation state
# ---------------------------------------------------------------------------

AGENT_HOMES = {'claude': ('CLAUDE_CONFIG_DIR', '.claude'), 'codex': ('CODEX_HOME', '.codex')}

TELEPORT_NS = _uuid.UUID('0daa8b13-5778-4cb5-8af4-7fa391f6b0dd')
IMPORT_PREFACE = '[Import context] Imported conversation history follows.'


@dataclass
class PortableMessage:
    role: str
    text: str = ''
    # Calls/results use Responses item shapes as the shared representation.
    # Claude's error flag is retained separately from the native Codex payload.
    tool: dict | None = None
    phase: str | None = None
    synthetic: bool = False
    codex_context: str = ''  # app-supplied prefix, retained outside Claude's user prompt


@dataclass
class PortableSession:
    agent: str
    session_id: str
    cwd: str
    timestamp: str
    messages: list[PortableMessage] = field(default_factory=list)
    notices: dict[str, int] = field(default_factory=dict)

    def note(self, description: str) -> None:
        self.notices[description] = self.notices.get(description, 0) + 1


def agent_home(agent: str) -> Path:
    if agent not in AGENT_HOMES:
        raise ValueError(f'unsupported client: {agent}')
    key, folder = AGENT_HOMES[agent]
    return Path(os.environ.get(key) or Path.home() / folder).expanduser()


def native_path(path: Path) -> Path:
    """Windows file APIs need extended paths independently of Python's manifest."""
    if os.name != 'nt':
        return path
    value = str(path.resolve())
    if value.startswith('\\\\?\\'):
        return Path(value)
    return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)


def agent_transcripts(agent: str, home: Path, archived: bool = False) -> list[Path]:
    if agent == 'claude':
        files = list((home / 'projects').glob('*/*.jsonl'))
    elif agent == 'codex':
        files = list((home / 'sessions').rglob('*.jsonl'))
        if archived:
            files += list((home / 'archived_sessions').rglob('*.jsonl'))
    else:
        raise ValueError(f'unsupported client: {agent}')
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def cmd_agent_sessions(args) -> int:
    if args.partition or args.host or args.cli or args.desktop:
        die('--agent cannot be combined with partition/host/origin filters; use --home')
    if args.limit is not None and args.limit < 1:
        die('--limit must be positive')
    home = native_path(Path(args.home).expanduser() if args.home else agent_home(args.agent))
    try:
        files = agent_transcripts(args.agent, home, args.all)
        for path in files[:args.limit or 20]:
            try:
                session = read_portable_session(path)
            except ValueError as exc:
                print(f'Unavailable: {path}\n  {exc}')
                continue
            print(f'{session.agent} {session.session_id}  {session_title(session)}')
            print(f'  cwd: {session.cwd}\n  transcript: {path}')
        if not files:
            print(f'No {args.agent} transcripts found under {home}.')
        return 0
    except OSError as exc:
        die(str(exc))


def portable_text(content, session: PortableSession) -> str:
    """Text-only projection for previews and unsupported media placeholders."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError('unsupported message content shape')
    out = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError('invalid content block')
        kind = block.get('type', 'unknown')
        if kind in ('text', 'input_text', 'output_text'):
            if not isinstance(block.get('text'), str):
                raise ValueError('text block has no text')
            out.append(block['text'])
        elif kind in ('thinking', 'redacted_thinking', 'reasoning', 'encrypted_content'):
            session.note('private reasoning omitted')
        else:
            session.note(f'{kind} content omitted (placeholder retained)')
            out.append(f'[Imported {kind} content unavailable; consult the original session.]')
    return '\n'.join(out)


def portable_output(value, session: PortableSession):
    """Keep text result arrays structured; translate their block vocabulary once."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError('unsupported tool output shape')
    return [dict(type='input_text', text=portable_text([block], session)) for block in value]


def split_codex_user_text(text: str) -> tuple[str, str]:
    """T7: unwrap only the complete, leading Codex ambient-browser envelope.

    Never search inside a user's request: quoted examples and user-authored
    headings must survive. The exact prefix remains available for return trips.
    """
    match = re.match(
        r'\A\s*<in-app-browser-context source="ambient-ui-state">(?P<body>.*?)'
        r'</in-app-browser-context>[ \t\r\n]*## My request:[ \t]*\r?\n', text, re.DOTALL)
    disclaimer = "This block is automatically supplied ambient UI state, not part of the user's request."
    if not match or disclaimer not in match['body'] or not text[match.end():].strip():
        return text, ''
    return text[match.end():], text[:match.end()]


def append_portable_content(session: PortableSession, role: str, content, phase=None) -> None:
    if isinstance(content, str):
        content = [dict(type='text', text=content)]
    if not isinstance(content, list):
        raise ValueError('unsupported message content shape')
    for block in content:
        if not isinstance(block, dict):
            raise ValueError('invalid content block')
        kind = block.get('type')
        if kind == 'tool_use':
            if role != 'assistant' or not isinstance(block.get('input'), dict):
                raise ValueError('invalid Claude tool call')
            session.messages.append(PortableMessage('assistant', tool=dict(
                type='function_call', call_id=block.get('id'), name=block.get('name'),
                arguments=json.dumps(block['input'], ensure_ascii=False))))
        elif kind == 'tool_result':
            if role != 'user':
                raise ValueError('invalid Claude tool result role')
            tool = dict(type='function_call_output', call_id=block.get('tool_use_id'),
                        output=portable_output(block.get('content', ''), session))
            if 'is_error' in block:
                tool['is_error'] = block['is_error']
            session.messages.append(PortableMessage('tool', tool=tool))
        else:
            text = portable_text([block], session)
            if text:
                context = ''
                if session.agent == 'codex' and role == 'user':
                    text, context = split_codex_user_text(text)
                    if context:
                        session.note('ambient browser context retained in conversion metadata, outside user prompt')
                session.messages.append(PortableMessage(role, text, phase=phase, codex_context=context))


def projection_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def with_teleport_metadata(row: dict, payload: dict, messages: list[PortableMessage]) -> dict:
    # T6: the native projection is usable independently; conversion metadata
    # restores format-only distinctions without nesting prior import envelopes.
    row['teleporter'] = dict(version=1, digest=projection_digest(payload),
                             items=[asdict(message) for message in messages])
    return row


def restore_teleport_metadata(row: dict, payload: dict, session: PortableSession) -> bool:
    data = row.get('teleporter')
    if data is None:
        return False
    if not isinstance(data, dict) or data.get('version') != 1 or data.get('digest') != projection_digest(payload):
        session.note('stale or unknown conversion metadata ignored; native history used')
        return False
    try:
        items = [PortableMessage(**item) for item in data['items']]
    except (KeyError, TypeError):
        raise ValueError('invalid conversion metadata') from None
    try:
        for item in items:
            validate_portable_message(item)
        if row['type'] == 'response_item':
            projected = codex_item(items[0]) if len(items) == 1 else None
        else:
            roles = {'user' if item.role == 'tool' else item.role for item in items}
            projected = dict(role=next(iter(roles)), content=[claude_block(item) for item in items]) if len(roles) == 1 else None
        if projected != payload:
            raise ValueError('conversion metadata contradicts native history')
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'invalid conversion metadata: {exc}') from None
    session.messages.extend(item for item in items if not item.synthetic)
    return True


def validate_portable_message(message: PortableMessage) -> None:
    if message.role not in ('user', 'assistant', 'tool') or not isinstance(message.text, str):
        raise ValueError('invalid portable message')
    if not isinstance(message.codex_context, str) or (message.codex_context and (
        message.role != 'user' or message.tool is not None or message.synthetic or
        split_codex_user_text(message.codex_context + message.text) != (message.text, message.codex_context)
    )):
        raise ValueError('invalid Codex ambient context')
    if message.phase not in (None, 'commentary', 'final_answer'):
        raise ValueError('unsupported assistant phase')
    if not isinstance(message.synthetic, bool) or (message.synthetic and (
        message.role != 'user' or message.text != IMPORT_PREFACE or message.tool is not None
    )):
        raise ValueError('invalid synthetic import preface')
    tool = message.tool
    if tool is None:
        if message.role == 'tool':
            raise ValueError('tool result is missing its payload')
        return
    if not isinstance(tool, dict) or not isinstance(tool.get('call_id'), str) or not tool['call_id']:
        raise ValueError('tool call/result is missing its call ID')
    if message.text or message.phase is not None:
        raise ValueError('tool payload cannot also carry message text or phase')
    if tool.keys() - {'type', 'call_id', 'name', 'namespace', 'arguments', 'input', 'output', 'is_error'}:
        raise ValueError('unsupported portable tool fields')
    if tool.get('namespace') is not None and not isinstance(tool['namespace'], str):
        raise ValueError('invalid tool namespace')
    kind = tool.get('type')
    if kind in ('function_call', 'custom_tool_call'):
        key = 'arguments' if kind == 'function_call' else 'input'
        if message.role != 'assistant' or not isinstance(tool.get('name'), str) or not tool['name']:
            raise ValueError('invalid tool call name or role')
        if not isinstance(tool.get(key), str):
            raise ValueError('invalid tool call input')
    elif kind in ('function_call_output', 'custom_tool_call_output'):
        output = tool.get('output')
        if message.role != 'tool' or not isinstance(output, (str, list)):
            raise ValueError('invalid tool output')
        if isinstance(output, list) and any(not isinstance(block, dict) or
                block.get('type') != 'input_text' or not isinstance(block.get('text'), str) or
                block.keys() != {'type', 'text'} for block in output):
            raise ValueError('invalid tool output block')
        if 'is_error' in tool and not isinstance(tool['is_error'], bool):
            raise ValueError('invalid tool error flag')
    else:
        raise ValueError('unsupported portable tool item')


def validate_portable_tools(session: PortableSession) -> None:
    """T2/T6: only complete, unambiguous historical exchanges may cross clients."""
    calls, completed = {}, set()
    for message in session.messages:
        validate_portable_message(message)
        tool = message.tool
        if tool is None:
            continue
        kind, call_id = tool['type'], tool['call_id']
        if kind in ('function_call', 'custom_tool_call'):
            if call_id in calls:
                raise ValueError('duplicate tool call ID')
            calls[call_id] = kind
        else:
            if calls.get(call_id) != kind.removesuffix('_output'):
                raise ValueError('orphan or mismatched tool result; export a complete exchange')
            if call_id in completed:
                raise ValueError('duplicate tool result ID')
            completed.add(call_id)
    if calls.keys() != completed:
        raise ValueError('pending tool call; finish the source turn before teleporting')


def session_title(session: PortableSession) -> str:
    return next((m.text.splitlines()[0] for m in session.messages if m.text.strip()), 'Imported tool history')[:70]


# Claude transcript reader: branch topology and native blocks
def decode_claude_session(records: list[dict], project_dir: str) -> PortableSession:
    # Follow the last main-chain leaf, including non-message parent nodes. File
    # order alone can replay abandoned branches or subagents as user history.
    for rec in records:
        for key in ('uuid', 'parentUuid', 'sourceToolAssistantUUID'):
            if rec.get(key) is not None and not isinstance(rec[key], str):
                raise ValueError(f'invalid Claude {key}')
        if rec.get('type') not in ('user', 'assistant'):
            continue
        message = rec.get('message')
        if not isinstance(message, dict):
            raise ValueError('invalid Claude message')
        content = message.get('content')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') in ('tool_use', 'tool_result'):
                    key = 'id' if block['type'] == 'tool_use' else 'tool_use_id'
                    if not isinstance(block.get(key), str) or not block[key]:
                        raise ValueError('invalid Claude tool call/result ID')
    nodes = {r['uuid']: r for r in records if r.get('uuid') and not r.get('isSidechain')}
    leaves = [r for r in records if r.get('type') in ('user', 'assistant')
              and r.get('uuid') in nodes and not r.get('isSidechain')]
    if not leaves:
        raise ValueError('no main-chain Claude conversation found')
    chain, seen, node = [], set(), leaves[-1]
    while node:
        key = node['uuid']
        if key in seen:
            raise ValueError('cycle in Claude parent chain')
        seen.add(key)
        chain.append(node)
        parent = node.get('parentUuid')
        if node.get('subtype') == 'compact_boundary' or not parent:
            break
        if parent not in nodes:
            raise ValueError('missing Claude parent; export a complete transcript')
        node = nodes[parent]
    chain.reverse()
    # T2: parallel Claude tool results are siblings, not ancestors of the last
    # leaf. Recover only results explicitly linked to active tool-call nodes;
    # unrelated user/assistant branches must remain excluded.
    active_calls = {}
    active_results = set()
    for rec in chain:
        content = rec.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if rec.get('type') == 'assistant' and block.get('type') == 'tool_use':
                active_calls[block.get('id')] = rec['uuid']
            elif block.get('type') == 'tool_result':
                active_results.add(block.get('tool_use_id'))
    siblings = []
    for rec in records:
        if rec.get('type') != 'user' or rec.get('uuid') in seen or nodes.get(rec.get('uuid')) is not rec:
            continue
        content = rec.get('message', {}).get('content', [])
        if not isinstance(content, list) or not content:
            continue
        # Mixed user text on an abandoned branch cannot be reintroduced as if
        # it were active. Only pure tool-result records qualify for recovery.
        if all(isinstance(block, dict) and block.get('type') == 'tool_result' and
               block.get('tool_use_id') in active_calls and block.get('tool_use_id') not in active_results and
               active_calls[block['tool_use_id']] in (rec.get('sourceToolAssistantUUID'), rec.get('parentUuid'))
               for block in content):
            siblings.append(rec)
    if siblings:
        order = {id(rec): index for index, rec in enumerate(records)}
        chain = sorted([*chain, *siblings], key=lambda rec: order[id(rec)])
    sid = leaves[-1].get('sessionId')
    if not isinstance(sid, str) or not sid:
        raise ValueError('Claude sessionId is missing')
    cwds = [r['cwd'] for r in chain if isinstance(r.get('cwd'), str) and r['cwd']]
    # Match existing Claude locator semantics: a visited subdirectory need not
    # be the project where the transcript actually lives (F2/F3).
    cwd = transcript_cwd(cwds, project_dir)
    stamp = next((r['timestamp'] for r in chain if r.get('timestamp')), '')
    session = PortableSession('claude', sid, cwd, stamp)
    for rec in chain:
        if rec.get('sessionId', sid) != sid:
            raise ValueError('mixed Claude session IDs')
        if rec.get('type') not in ('user', 'assistant'):
            if rec.get('subtype') == 'compact_boundary':
                session.note('only the active context after compaction is imported')
            elif rec.get('type') == 'attachment':
                session.note('Claude attachment metadata omitted')
            continue
        message = rec.get('message')
        if not isinstance(message, dict) or message.get('role') != rec['type']:
            raise ValueError('invalid Claude message role')
        if not restore_teleport_metadata(rec, dict(role=message['role'], content=message.get('content')), session):
            append_portable_content(session, rec['type'], message.get('content'))
    # Claude has one result type for function and custom calls. Derive the
    # result kind from the surviving call, including when a client edited only
    # one side and invalidated that row's conversion metadata.
    call_kinds = {}
    for message in session.messages:
        validate_portable_message(message)
        if message.tool:
            tool = message.tool
            if message.role == 'assistant':
                call_kinds[tool['call_id']] = tool['type']
            elif tool.get('call_id') in call_kinds:
                tool['type'] = call_kinds[tool['call_id']] + '_output'
    return session


# Codex rollout reader: compaction, model items, and native events
def decode_codex_session(records: list[dict]) -> PortableSession:
    meta = records[0].get('payload')
    if not isinstance(meta, dict) or not isinstance(meta.get('id'), str):
        raise ValueError('Codex session metadata is missing its id')
    session = PortableSession('codex', meta['id'], meta.get('cwd', ''), meta.get('timestamp', ''))
    items = []
    for rec in records[1:]:
        payload = rec.get('payload', {})
        if not isinstance(payload, dict):
            raise ValueError('invalid Codex payload')
        kind = rec.get('type')
        if kind == 'response_item':
            items.append((payload, rec))
        elif kind == 'compacted':
            # replacement_history is the actual context Codex resumes, not a
            # duplicate transcript to append to everything before compaction.
            replacement = payload.get('replacement_history')
            if not isinstance(replacement, list) or not all(isinstance(x, dict) for x in replacement):
                raise ValueError('Codex compaction lacks replacement_history; cannot reconstruct context')
            items = [(item, {}) for item in replacement]
            session.note('only the active context after compaction is imported')
        elif kind == 'turn_context' and payload.get('cwd'):
            session.cwd = payload['cwd']
        elif kind == 'event_msg' and payload.get('type') == 'thread_rolled_back':
            raise ValueError('Codex rollback history is not supported; export a fork after the rollback')
        elif kind == 'session_meta':
            # Forks can prepend their own header to inherited parent history.
            # The first header owns this rollout; later headers are provenance.
            session.note('inherited Codex session metadata omitted')
    for item, rec in items:
        if restore_teleport_metadata(rec, item, session):
            continue
        kind = item.get('type')
        if kind == 'message':
            role = item.get('role')
            if role not in ('user', 'assistant'):
                session.note('source system/developer instructions omitted')
                continue
            append_portable_content(session, role, item.get('content'), item.get('phase'))
            continue
        elif kind in ('function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output'):
            keys = ('type', 'call_id', 'name', 'namespace', 'arguments', 'input', 'output')
            tool = {key: item[key] for key in keys if key in item}
            output = kind.endswith('_output')
            if output:
                tool['output'] = portable_output(tool.get('output'), session)
            session.messages.append(PortableMessage('tool' if output else 'assistant', tool=tool))
            continue
        elif kind == 'reasoning':
            session.note('private reasoning omitted')
            continue
        else:
            session.note(f'Codex {kind} item omitted (placeholder retained)')
            role, text = 'assistant', f'[Imported {kind} item unavailable; consult the original session.]'
        if text:
            session.messages.append(PortableMessage(role, text))
    return session


def read_portable_session(path: Path) -> PortableSession:
    """Strict snapshot reader: malformed/truncated input fails before any writes."""
    path = native_path(path)
    before = path.stat()
    records = []
    with path.open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise ValueError(f'invalid JSON on line {number}; stop the source session and retry') from None
            if not isinstance(record, dict):
                raise ValueError(f'expected a JSON object on line {number}')
            records.append(record)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('source changed while reading; stop the source session and retry')
    if not records:
        raise ValueError('empty transcript')
    session = (decode_codex_session(records) if records[0].get('type') == 'session_meta'
               else decode_claude_session(records, path.parent.name))
    if not session.messages:
        raise ValueError('no portable conversation content')
    validate_portable_tools(session)
    if not isinstance(session.cwd, str) or not isinstance(session.timestamp, str):
        raise ValueError('invalid source cwd or timestamp')
    try:
        stamp = datetime.fromisoformat(session.timestamp.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('source timestamp must include a timezone') from None
    session.timestamp = stamp.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return session


def portable_tool_arguments(tool: dict) -> dict:
    """Object projection shared by Claude tool_use and Codex display events.

    Conversion metadata retains the original raw arguments for round trips.
    """
    if tool['type'] == 'custom_tool_call':
        return dict(input=tool['input'])
    try:
        arguments = json.loads(tool['arguments'])
    except ValueError:
        arguments = tool['arguments']
    return arguments if isinstance(arguments, dict) else dict(input=arguments)


# ---------------------------------------------------------------------------
# Client encoders: native content blocks, envelopes, and display events
# ---------------------------------------------------------------------------

def claude_block(message: PortableMessage) -> dict:
    tool = message.tool
    if tool is None:
        return dict(type='text', text=message.text)
    kind = tool['type']
    if kind.endswith('_output'):
        output = tool['output']
        if isinstance(output, list):
            output = [dict(type='text', text=block['text']) for block in output]
        block = dict(type='tool_result', tool_use_id=tool['call_id'], content=output)
        if 'is_error' in tool:
            block['is_error'] = tool['is_error']
        return block
    return dict(type='tool_use', id=tool['call_id'], name=tool['name'], input=portable_tool_arguments(tool))


def codex_item(message: PortableMessage) -> dict:
    if message.tool is not None:
        item = {key: value for key, value in message.tool.items() if key != 'is_error'}
        if message.tool.get('is_error'):
            # Responses has no native is_error bit: expose it inside the tool
            # result and restore the precise original via conversion metadata.
            output = item['output']
            item['output'] = ('[Tool error]\n' + output if isinstance(output, str) else
                              [dict(type='input_text', text='[Tool error]'), *output])
        return item
    item = dict(type='message', role=message.role, content=[dict(
        type='input_text' if message.role == 'user' else 'output_text', text=message.codex_context + message.text)])
    if message.phase is not None:
        item['phase'] = message.phase
    return item


def encode_codex_session(messages: list[PortableMessage], sid: str, cwd: str, stamp: str) -> list[dict]:
    """Codex model history and matching desktop display events."""
    rows = []
    # T5: omit model_provider so discovery uses the destination provider.
    rows.append(dict(type='session_meta', timestamp=stamp, payload=dict(
        id=sid, timestamp=stamp, cwd=cwd, originator='claude-session-teleporter',
        cli_version=__version__, source='cli', history_mode='legacy')))
    calls = {}
    for message in messages:
        item = codex_item(message)
        rows.append(with_teleport_metadata(dict(type='response_item', timestamp=stamp, payload=item), item, [message]))
        if message.tool is None:
            event = dict(type='user_message' if message.role == 'user' else 'agent_message', message=message.text)
            if message.role == 'user':
                event.update(images=[], local_images=[], text_elements=[])
            else:
                event['phase'] = message.phase or 'final_answer'
        else:
            tool = message.tool
            if not tool['type'].endswith('_output'):
                calls[tool['call_id']] = tool
                event = dict(type='mcp_tool_call_begin', call_id=tool['call_id'], turn_id='',
                             invocation=dict(server='imported_history', tool=tool['name'],
                                             arguments=portable_tool_arguments(tool)))
            else:
                call = calls[tool['call_id']]
                output = item['output']
                content = [dict(type='text', text=output)] if isinstance(output, str) else [
                    dict(type='text', text=block['text']) for block in output]
                # The installed Codex reader renders MCP events as completed
                # generic tool cards. This display namespace registers no tool.
                event = dict(type='mcp_tool_call_end', call_id=tool['call_id'], turn_id='',
                             invocation=dict(server='imported_history', tool=call['name'],
                                             arguments=portable_tool_arguments(call)),
                             result={'Ok': dict(content=content, isError=tool.get('is_error', False))},
                             duration=dict(secs=0, nanos=0))
        rows.append(dict(type='event_msg', timestamp=stamp, payload=event))
    return rows


def encode_claude_session(messages: list[PortableMessage], sid: str, cwd: str, stamp: str) -> list[dict]:
    """Claude's linked role groups and adjacent native tool exchanges."""
    rows = []
    # Claude requires parallel calls in one assistant message and their results
    # in the following user message. Group by native role, preserving block order.
    groups = []
    for message in messages:
        role = 'user' if message.role == 'tool' else message.role
        if groups and groups[-1][0] == role:
            groups[-1][1].append(message)
        else:
            groups.append((role, [message]))
    # T6: Claude repairs missing/nonadjacent results by dropping late output
    # and inserting a synthetic interruption. Refuse that lossy projection.
    for index, (role, group) in enumerate(groups):
        if role != 'assistant':
            continue
        calls = {message.tool['call_id'] for message in group if message.tool}
        results = ({message.tool['call_id'] for message in groups[index + 1][1] if message.tool}
                   if index + 1 < len(groups) else set())
        if calls != results:
            raise ValueError('Claude cannot preserve this interleaved tool exchange; '
                             'results must immediately follow their call group')
    parent = None
    for index, (role, group) in enumerate(groups):
        mid = str(_uuid.uuid5(_uuid.UUID(sid), str(index)))
        projection = dict(role=role, content=[claude_block(message) for message in group])
        msg = dict(projection)
        if role == 'assistant':
            msg.update(id='msg_' + mid.replace('-', ''), type='message', model='imported',
                       stop_reason='tool_use' if any(m.tool for m in group) else 'end_turn',
                       stop_sequence=None, usage=dict(input_tokens=0, output_tokens=0))
        row = dict(type=role, uuid=mid, parentUuid=parent, sessionId=sid,
                   timestamp=stamp, cwd=cwd, isSidechain=False, userType='external',
                   entrypoint='cli', version=__version__, message=msg)
        rows.append(with_teleport_metadata(row, projection, group))
        parent = mid
    return rows


def teleport_rows(session: PortableSession, target: str, sid: str, cwd: str) -> list[dict]:
    """Shared validation/preface policy, followed by a client-specific encoder."""
    validate_portable_tools(session)
    messages = session.messages
    if messages[0].role != 'user':
        messages = [PortableMessage('user', IMPORT_PREFACE, synthetic=True), *messages]
    encoders = {'claude': encode_claude_session, 'codex': encode_codex_session}
    if target not in encoders:
        raise ValueError(f'unsupported destination client: {target}')
    return encoders[target](messages, sid, cwd, session.timestamp)


# ---------------------------------------------------------------------------
# Transcript publication: filesystem transactions, independent of either client
# ---------------------------------------------------------------------------


def publish_teleport_file(temporary: Path, destination: Path) -> None:
    # Windows rename refuses an existing destination and works over WSL's 9P
    # share, where CreateHardLink is unsupported. POSIX rename can overwrite,
    # so use exclusive hard-link publication there instead.
    if os.name == 'nt':
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)


def _publish_teleport(files: list[tuple[Path, str]], tombstone: Path | None) -> None:
    """Publish complete files without replacing a winner; roll back our files on failure."""
    files = [(native_path(path), content) for path, content in files]
    tombstone = native_path(tombstone) if tombstone is not None else None
    staged, published = [], []
    try:
        for path, content in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.teleport-', dir=path.parent)
            temporary = Path(name)
            staged.append(temporary)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for (path, _), temporary in zip(files, staged):
            if tombstone is not None and tombstone.exists():
                raise ValueError('destination session has a deletion tombstone')
            # Publish a complete file, never replacing a concurrent writer.
            expected = temporary.stat()
            publish_teleport_file(temporary, path)
            published.append((path, expected))
    except BaseException:
        for path, expected in reversed(published):
            try:
                actual = path.lstat()
                # A consumer can replace or resume the transcript before a
                # later metadata write fails. Preserve that consumer's work.
                if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns) == (
                    expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns
                ):
                    path.unlink()
                else:
                    warn(f'preserved a concurrently changed import: {path}')
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                warn(f'could not clean up incomplete import {path}: {cleanup_error}')
        raise
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Codex desktop integration: native RPC, project selection, and membership
# ---------------------------------------------------------------------------

def codex_project_environment(home: Path, sqlite_home: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # T8: an explicit destination must not inherit another host's database.
    current = native_path(Path(env.get('CODEX_HOME', Path.home()/'.codex')).resolve())
    if current != native_path(home.resolve()):
        env.pop('CODEX_SQLITE_HOME', None)
    env['CODEX_HOME'] = str(home)
    if sqlite_home:
        env['CODEX_SQLITE_HOME'] = str(Path(sqlite_home).expanduser().resolve())
    return env


class CodexProjectClient:
    """Short-lived local JSON-RPC client; never starts a model turn."""

    def __init__(self, home: Path, binary: str, sqlite_home: str | None = None):
        env = codex_project_environment(home, sqlite_home)
        self.process = subprocess.Popen([binary, 'app-server'], env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8')
        self.output = queue.Queue()
        self.sequence = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                value = json.loads(line)
                if 'id' in value and ('result' in value or 'error' in value):
                    self.output.put(value)
        except (ValueError, OSError):
            pass  # rpc reports the closed/invalid stream; never print protocol data.
        finally:
            self.output.put(None)

    def __enter__(self):
        try:
            self.rpc('initialize', {'clientInfo': {'name': 'claude_session_teleporter', 'version': __version__},
                                    'capabilities': {'experimentalApi': True}})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()

    def rpc(self, method: str, params: dict) -> dict:
        self.sequence += 1
        self.process.stdin.write(json.dumps(dict(id=self.sequence, method=method, params=params))+'\n')
        self.process.stdin.flush()
        try:
            result = self.output.get(timeout=30)
        except queue.Empty:
            raise ValueError(f'Codex {method} timed out') from None
        if result is None:
            raise ValueError(f'Codex exited during {method}; check --codex-bin and destination configuration')
        if result.get('id') != self.sequence or 'error' in result:
            raise ValueError(f'Codex {method} failed: {result.get("error", "unexpected response")}. '
                             'Project placement requires a Codex app server with project APIs.')
        return result['result']

    def projects(self) -> list[dict]:
        projects, cursor, seen = [], None, set()
        while True:
            response = self.rpc('project/list', {'limit': 100, 'cursor': cursor})
            projects.extend(response['data'])
            cursor = response.get('nextCursor')
            if not cursor:
                return projects
            if cursor in seen:
                raise ValueError('Codex returned a repeated project-list cursor')
            seen.add(cursor)


def select_codex_project(projects: list[dict], cwd: str, selector: str) -> dict | None:
    """T8: match exact roots, never guess from overlapping ancestor folders."""
    if selector == 'auto':
        root = native_path(Path(cwd).resolve())
        matches = [p for p in projects if any(native_path(Path(r['path']).resolve()) == root for r in p['roots'])]
    else:
        matches = [p for p in projects if p['id'] == selector]
        if not matches:
            matches = [p for p in projects if p['name'] == selector]
        if not matches:
            raise ValueError(f'Codex project not found: {selector}')
    if len(matches) > 1:
        raise ValueError('ambiguous Codex project; choose an ID with --codex-project: ' +
                         ', '.join(f'{p["name"]} ({p["id"]})' for p in matches))
    return matches[0] if matches else None


def assign_codex_project(client: CodexProjectClient, sid: str, cwd: str,
                         project: dict | None, *, register: bool = False) -> dict:
    # Native resume registers the destination provider for DB-only desktop lists.
    # No turn/start, source configuration, or historical tool execution occurs.
    thread = client.rpc('thread/read', {'threadId': sid})['thread']
    if thread.get('cwd') != cwd:
        raise ValueError('Codex thread cwd differs from the import; project assignment refused')
    if register or not thread.get('modelProvider'):
        config = client.rpc('config/read', {'cwd': cwd, 'includeLayers': False})['config']
        thread = client.rpc('thread/resume', {'threadId': sid,
                            'modelProvider': config.get('model_provider') or 'openai'})['thread']
    if thread.get('cwd') != cwd:
        raise ValueError('Codex thread cwd differs from the import; project assignment refused')
    if thread.get('projectId'):
        if project is not None and thread['projectId'] != project['id']:
            raise ValueError('thread already belongs to another Codex project; left unchanged')
        project = client.rpc('project/read', {'projectId': thread['projectId']})['project']
        print(f'Codex project already assigned: {project["name"]} ({project["id"]})')
        return project
    if project is None:
        project = client.rpc('project/create', {
            'idempotencyKey': 'claude-session-teleporter:' + str(_uuid.uuid5(TELEPORT_NS, str(Path(cwd).resolve()))),
            'name': Path(cwd).name or cwd, 'roots': [{'path': cwd}],
        })['project']
    client.rpc('thread/metadata/update', {'threadId': sid, 'projectId': project['id']})
    verified = client.rpc('thread/read', {'threadId': sid})['thread']
    if verified.get('projectId') != project['id']:
        raise ValueError('Codex did not retain the requested project assignment')
    print(f'Codex project: {project["name"]} ({project["id"]})')
    return project


def cmd_codex_project(args) -> int:
    """Repair placement independently of the no-overwrite transcript importer."""
    try:
        home = native_path(Path(args.target_home).expanduser().resolve() if args.target_home else agent_home('codex').resolve())
        path = native_path(Path(args.transcript).expanduser().resolve())
        # T8: never address a same-ID session in a different destination store.
        if home/'sessions' not in path.parents:
            raise ValueError('rollout must be inside the destination CODEX_HOME/sessions')
        with path.open(encoding='utf-8') as stream:
            header = json.loads(stream.readline())
        if header.get('type') != 'session_meta':
            raise ValueError('expected a Codex rollout')
        sid, cwd = header['payload']['id'], header['payload']['cwd']
        print(f'Assign Codex session {sid} ({cwd}) to project: {args.codex_project}')
        if not args.apply:
            print('DRY RUN. Project resolution is deferred; no app server started and nothing written.')
            return 0
        with CodexProjectClient(home, args.codex_bin, args.codex_sqlite_home) as client:
            project = select_codex_project(client.projects(), cwd, args.codex_project)
            assign_codex_project(client, sid, cwd, project)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        die(str(exc))


# ---------------------------------------------------------------------------
# Import planning: read-only source, destination, and desktop metadata decisions
# ---------------------------------------------------------------------------

@dataclass
class TeleportPlan:
    source: Path
    session: PortableSession
    target_agent: str
    home: Path
    cwd: str
    session_id: str
    transcript: Path
    files: list[tuple[Path, str]]
    existing: list[Path]
    tombstone: Path | None
    codex_project: str | None

    def already_exists(self) -> bool:
        # T1/T3: never replace an evolved fork or silently fill a partial import.
        if not (self.existing or any(p.exists() or p.is_symlink() for p, _ in self.files)):
            return False
        if len(self.files) > 1 and not all(p.is_file() for p, _ in self.files):
            raise ValueError('incomplete desktop import: transcript and metadata are not both present; '
                             'existing files were preserved. Inspect the destination before retrying')
        return True


def plan_claude_desktop_metadata(session: PortableSession, sid: str, home: Path,
                                cwd: str, target: Path, host, partition: str) -> tuple[tuple[Path, str], Path]:
    """Use only destination-owned Claude connectors and reset donor permissions."""
    if not (host and host.is_wsl) and home / 'projects' != native_path(PROJECTS_DIR.resolve()):
        raise ValueError('desktop target must use this machine\'s Claude projects store (or --target-host wsl:NAME)')
    parts = load_partitions()
    dst = resolve_partition(parts, partition)
    c = CliSession(host=host or WINDOWS_HOST, transcript=target, cli_id=sid,
                   project_dir=target.parent.name, cwd=cwd, origin_cwd=cwd,
                   title=session_title(session),
                   created_at=_ms(session.timestamp), last_activity=int(time.time() * 1000))
    data, _ = build_adopted(c, adopt_template(dst), dst, connector_names(parts))
    # Never inherit donor approvals, worktrees, or runtime session settings.
    data.update(alwaysAllowedReasons=[], sessionPermissionUpdates=[], spawnSeed={})
    if not (host and host.is_wsl):
        data.pop('wslConfig', None)
        data.pop('sshRemoteTranscriptPath', None)
    metadata = dst.path / f'{c.session_id}.json'
    tombstone = dst.path / f'deleted_{c.uuid}'
    if tombstone.exists():
        raise ValueError('destination session has a deletion tombstone')
    return (metadata, json.dumps(data, ensure_ascii=False, indent=2) + '\n'), tombstone


def plan_teleport(args) -> TeleportPlan:
    """Build a complete import without creating files or launching client processes."""
    if args.to != 'codex' and (args.codex_project or args.codex_sqlite_home or args.codex_bin != 'codex'):
        raise ValueError('Codex project options require --to codex')
    project_selector = None
    if args.to == 'codex' and args.codex_project != 'none':
        project_selector = args.codex_project or 'auto'
    source = Path(args.transcript).expanduser()
    session = read_portable_session(source)
    if session.agent == args.to:
        raise ValueError(f'source is already {args.to}; teleport is for crossing clients')
    if args.target_host and (args.to != 'claude' or args.target_home):
        raise ValueError('--target-host is for Claude and cannot be combined with --target-home')
    if args.desktop_partition and args.to != 'claude':
        raise ValueError('--desktop-partition is only for Claude; Codex uses its sessions store')
    host = resolve_host(args.target_host) if args.target_host else None
    home = native_path((host.projects.parent if host else
                        Path(args.target_home).expanduser() if args.target_home else agent_home(args.to)).resolve())
    cwd = args.cwd or session.cwd
    is_absolute = cwd.startswith('/') if host and host.is_wsl else (Path(cwd).is_absolute() or win_to_wsl_path(cwd))
    if not cwd or not is_absolute:
        raise ValueError('destination cwd must be absolute; supply --cwd')
    reachable = host.root / cwd.lstrip('/') if host and host.is_wsl else Path(cwd)
    if os.name != 'nt' and win_to_wsl_path(cwd):
        reachable = Path(win_to_wsl_path(cwd))
    if not reachable.is_dir():
        raise ValueError('destination working directory is unavailable; supply a reachable --cwd')
    sid = str(_uuid.uuid5(TELEPORT_NS, f'{session.agent}:{session.session_id}:{args.to}:{cwd}'))
    stamp = session.timestamp
    if args.to == 'codex':
        target = home / 'sessions' / stamp[:10].replace('-', '/') / f'rollout-{stamp[:19].replace(":", "-")}-{sid}.jsonl'
        existing = [p for folder in ('sessions', 'archived_sessions')
                    for p in (home / folder).rglob(f'*{sid}.jsonl')]
    else:
        target = home / 'projects' / encode_cwd(cwd) / f'{sid}.jsonl'
        existing = list((home / 'projects').glob(f'*/{sid}.jsonl'))
    files = [(target, ''.join(json.dumps(r, ensure_ascii=False) + '\n'
                             for r in teleport_rows(session, args.to, sid, cwd)))]
    tombstone = None
    if args.desktop_partition:
        metadata, tombstone = plan_claude_desktop_metadata(
            session, sid, home, cwd, target, host, args.desktop_partition)
        files.append(metadata)
    return TeleportPlan(source=source, session=session, target_agent=args.to, home=home,
                        cwd=cwd, session_id=sid, transcript=target, files=files,
                        existing=existing, tombstone=tombstone, codex_project=project_selector)


# ---------------------------------------------------------------------------
# Import execution and CLI presentation
# ---------------------------------------------------------------------------

def apply_teleport(plan: TeleportPlan, binary: str, sqlite_home: str | None) -> None:
    """Publish once; keep native project registration outside transcript conversion."""
    if plan.codex_project is None:
        _publish_teleport(plan.files, plan.tombstone)
        return
    with CodexProjectClient(plan.home, binary, sqlite_home) as client:
        project = select_codex_project(client.projects(), plan.cwd, plan.codex_project)
        _publish_teleport(plan.files, plan.tombstone)
        try:
            assign_codex_project(client, plan.session_id, plan.cwd, project, register=True)
        except (OSError, ValueError) as exc:
            raise ValueError(f'Imported transcript retained at {plan.transcript}, but project placement failed: {exc}. '
                             'Repair with codex-project ROLLOUT using the same destination options and --apply') from exc


def print_teleport_plan(plan: TeleportPlan) -> None:
    session = plan.session
    print(f'{session.agent} -> {plan.target_agent}: {len(session.messages)} messages (new independent fork)')
    print(f'Source: {plan.source}\nDestination: {plan.transcript}\nSession: {plan.session_id}\nWorking directory: {plan.cwd}')
    for note, count in session.notices.items():
        print(f'  {count}x {note}')
    if session.messages[0].role != 'user':
        print('  Added a labeled import preface for assistant-first history.')
    print('Project files and source permissions are not copied. Completed tools retain native call/result structure.')
    if plan.codex_project:
        print(f'Codex project: {plan.codex_project} (resolve on apply; auto reuses an exact root or creates a project)')


def print_teleport_resume(plan: TeleportPlan) -> None:
    print(f'Created {plan.session_id}. Resume in the target environment:')
    command = 'codex resume' if plan.target_agent == 'codex' else 'claude --resume'
    print(f'  {command} {plan.session_id}')
    env_key = AGENT_HOMES[plan.target_agent][0]
    print(f'Run from {plan.cwd}; use the destination {env_key}={plan.home}.')
    if plan.target_agent == 'codex':
        print('Desktop: use the same CODEX_HOME and host. ' +
              ('Backend project assignment verified; legacy desktop builds may still need app-side project selection.' if plan.codex_project else
               'Use codex-project to assign this transcript-only import to a project.'))
    elif len(plan.files) > 1:
        print('Desktop: switch accounts or restart Claude to reload its session list.')
    else:
        print('For Claude desktop visibility, include --desktop-partition on the initial import, or use adopt for WSL.')


def cmd_teleport(args) -> int:
    try:
        plan = plan_teleport(args)
        print_teleport_plan(plan)
        if plan.already_exists():
            print('Destination already exists; left untouched (including archived imports).')
        elif not args.apply:
            print('DRY RUN. Nothing written. Re-run with --apply to create this fork.')
        else:
            apply_teleport(plan, args.codex_bin, args.codex_sqlite_home)
            print_teleport_resume(plan)
        return 0
    except (OSError, ValueError) as exc:
        die(str(exc))


# ---------------------------------------------------------------------------
# Local browser UI. Assets are embedded to keep the single-file install story.
# ---------------------------------------------------------------------------

UI_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session teleporter</title>
<style nonce="__NONCE__">
:root{color-scheme:dark;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;background:#090909;color:#eeeee8}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;justify-content:center;align-items:flex-start;padding:clamp(18px,5vw,60px)}button,input,select{font:inherit}button{cursor:pointer}button:disabled{cursor:default}[hidden]{display:none!important}
.app{width:min(860px,100%);border:1px solid #30302d;background:#090909;box-shadow:0 22px 70px #0008}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:25px 28px 22px}h1,h2{font-family:ui-monospace,Consolas,monospace;font-weight:400;letter-spacing:-1.5px}h1{font-size:29px;line-height:1.1;margin:0}h2{font-size:25px;margin:0 0 15px}.local{color:#a4a49b;font-size:11px;white-space:nowrap}.local:before{content:"";display:inline-block;width:6px;height:6px;background:#bad097;margin-right:8px;vertical-align:1px}
.search{display:flex;align-items:center;gap:10px;margin:0 28px 21px;padding:10px 12px;border:1px solid #383832;color:#aaa99f}.search input{width:100%;background:transparent;border:0;outline:0;color:#eeeee8;font-size:12px}.search:focus-within{border-color:#aaa99f}input::placeholder{color:#929289}
.columnhead{padding:10px 28px;border-bottom:1px solid #34342e;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#aaa99f}.all{display:flex;align-items:center;gap:13px;cursor:pointer}.filter-wrap{position:relative}.filter-button{border:0;background:transparent;color:#aaa99f;padding:3px 0 3px 8px;font-size:11px}.filter-button:hover,.filter-button[aria-expanded=true]{color:#eeeee8}.filter-menu{position:absolute;top:calc(100% + 9px);right:0;z-index:3;min-width:190px;max-width:min(300px,80vw);max-height:260px;overflow:auto;padding:7px;background:#161613;border:1px solid #606057;box-shadow:0 14px 30px #000a}.filter-option{display:flex;align-items:center;gap:10px;padding:8px;color:#deded4;white-space:nowrap;cursor:pointer}.filter-option:hover{background:#292923}.filter-separator{height:1px;background:#3e3e36;margin:4px 0}input[type=checkbox]{width:15px;height:15px;accent-color:#eeeee8;margin:0;flex-shrink:0;cursor:pointer}
.list{max-height:min(56vh,560px);min-height:145px;overflow:auto}.row{display:grid;grid-template-columns:15px minmax(0,1fr) 140px;align-items:center;gap:13px;padding:15px 28px;border-bottom:1px solid #262623;min-height:69px;cursor:pointer}.row:has(input:checked){background:#eeeee8;color:#11110e}.row:has(input:checked) input{accent-color:#161612;color-scheme:light}.row:has(input:disabled){opacity:.45}.row-main{display:block;width:100%;padding:0;border:0;background:transparent;color:inherit;text-align:left;cursor:pointer}.row-main:hover .title{text-decoration:underline;text-underline-offset:3px}.title{display:block;font-size:13px;font-weight:600;overflow-wrap:anywhere}.project{display:block;color:#9e9e94;font-size:11px;margin-top:4px}.agent{font-size:11px;text-align:right;color:#afafa4}.row:has(input:checked) .project,.row:has(input:checked) .agent{color:#59594f}.empty{padding:36px 20px;text-align:center;color:#aaa99f}
.actions{display:flex;gap:14px;align-items:center;padding:22px 28px;background:#0d0d0b;border-top:1px solid #42423b;flex-wrap:wrap}.count{color:#afafa4;font-size:12px;margin-right:auto}.to{display:flex;align-items:center;gap:10px;color:#afafa4;font-size:12px}select{background:#11110e;border:1px solid #57574d;color:#eeeee8;padding:10px 30px 10px 11px;min-width:155px;font-size:12px}.primary{background:#eeeee8;color:#11110e;border:1px solid #eeeee8;padding:11px 16px;font-size:12px;font-weight:600;min-width:150px}.primary:hover:not(:disabled){background:white}.primary:disabled{opacity:.4}.secondary{background:transparent;color:#ccccc0;border:1px solid #55554b;padding:10px 16px;font-size:12px}.secondary:hover{border-color:#ccc}.status{margin:0;padding:14px 28px;color:#e0e0d4;background:#181813;border-top:1px solid #44443b;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}.status.error{color:#ffb6a8}.status details,.dialog details{margin-top:10px}.status pre,.dialog pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#aaa99f;max-height:200px;overflow:auto;font:11px/1.5 inherit}
.backdrop{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;padding:20px;background:#000c;z-index:2}.dialog{width:450px;max-width:100%;max-height:90vh;overflow:auto;padding:26px;background:#11110f;border:1px solid #66665a;box-shadow:0 20px 70px #0008}.dialog p{color:#b4b4a6;margin:0 0 18px;line-height:1.7;font-size:12px}.dialog ul{padding:0;list-style:none;margin:0 0 20px;font-size:12px}.dialog li{padding:6px 0;overflow-wrap:anywhere}.dialog li small{display:block;color:#99998d}.dialog-actions{display:flex;justify-content:end;gap:10px;flex-wrap:wrap}.note{color:#a4a49b;font-size:11px;margin-top:14px}
.peek-dialog{width:650px}.peek-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.peek-meta{font-size:11px;color:#a4a49b;overflow-wrap:anywhere}.peek-scroll{max-height:58vh;overflow:auto;margin:17px 0}.peek-turn{padding:13px 0;border-top:1px solid #35352f}.peek-role{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#a4a49b;margin-bottom:7px}.peek-text{font-size:12px;color:#e3e3d9;white-space:pre-wrap;overflow-wrap:anywhere;margin:0!important;line-height:1.6}.peek-gap{font-size:11px;color:#929289;padding:12px 0}.peek-note{font-size:11px;color:#a4a49b;margin:0 0 15px}
@media(max-width:600px){body{padding:0}.app{border-left:0;border-right:0;min-height:100vh}header{padding:22px 18px}h1{font-size:25px}.search{margin:0 18px 17px}.columnhead{padding:10px 18px}.row{padding:15px 18px;grid-template-columns:15px minmax(0,1fr);gap:10px}.agent{grid-column:2;text-align:left;margin-top:-5px}.actions{padding:18px;gap:12px}.count{width:100%;margin-right:0}.to{flex:1}select{min-width:0;max-width:210px;flex:1}.actions>.primary{width:100%}.dialog{padding:20px}}
</style>
</head>
<body>
<main class="app">
<header><h1>Session teleporter</h1><span class="local">On this computer</span></header>
<label class="search"><span aria-hidden="true">⌕</span><input id="search" type="search" aria-label="Search sessions" placeholder="Search sessions…"></label>
<div class="columnhead"><label class="all"><input id="all" type="checkbox"><span>Select shown (up to 25)</span></label><div class="filter-wrap"><button type="button" id="agent-filter" class="filter-button" aria-expanded="false" aria-controls="agent-menu">All agents ▾</button><div id="agent-menu" class="filter-menu" hidden><label class="filter-option"><input id="agent-all" type="checkbox" checked>All</label><div class="filter-separator"></div><div id="agent-options"></div></div></div></div>
<div id="list" class="list" aria-label="Discovered sessions"><p class="empty">Finding sessions…</p></div>
<footer class="actions"><span id="count" class="count" aria-live="polite">0 selected</span><label class="to">To <select id="target" aria-label="Destination agent"></select></label><button type="button" id="transfer" class="primary" disabled>Transfer sessions →</button></footer>
<div id="status" class="status" role="status" aria-live="polite" hidden></div>
</main>
<div id="modal" class="backdrop" hidden><section class="dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><h2 id="confirm-title"></h2><ul id="confirm-list"></ul><p id="confirm-note">Original sessions stay intact. Existing sessions are never overwritten.</p><details><summary>Transfer details</summary><pre id="details"></pre></details><div class="dialog-actions"><button type="button" id="cancel" class="secondary">Cancel</button><button type="button" id="confirm" class="primary">Transfer</button></div><div class="note">Local transfer · no upload</div></section></div>
<div id="peek-modal" class="backdrop" hidden><section class="dialog peek-dialog" role="dialog" aria-modal="true" aria-labelledby="peek-title"><div class="peek-heading"><h2 id="peek-title">Session preview</h2><button type="button" id="peek-close" class="secondary">Close</button></div><div id="peek-meta" class="peek-meta"></div><div id="peek-messages" class="peek-scroll" aria-live="polite">Loading session…</div><div id="peek-note" class="peek-note">Read-only preview · your selection is preserved</div></section></div>
<script nonce="__NONCE__">
(() => {
  const $=id=>document.getElementById(id);
  const token=location.hash.slice(1);
  let catalog={sessions:[],destinations:[]}, selected=new Set(), sources=null, preview=null, busy=false, peekOpener=null, peekGeneration=0;
  const sourceNames=()=>[...new Set(catalog.sessions.map(r=>r.source))].sort((a,b)=>a.localeCompare(b));
  const rows=()=>catalog.sessions.filter(r=>(sources===null||sources.has(r.source))&&(r.title+' '+r.project+' '+r.source).toLowerCase().includes($('search').value.toLowerCase()));
  const target=()=>$('target').value;
  const eligible=r=>!r.missing && !(target()==='codex' && r.kind==='codex') && !(target().startsWith('claude:') && (r.kind==='claude-cli'||r.partition===target().slice(7)));
  const name=()=>catalog.destinations.find(d=>d.id===target())?.name||target();
  function activity(value){
    const date=new Date(Number(value));
    if(!Number.isFinite(Number(value))||Number(value)<=0||!Number.isFinite(date.getTime()))return {label:'Last active unknown',exact:''};
    const options={month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'};
    if(date.getFullYear()!==new Date().getFullYear())options.year='numeric';
    return {label:'Last active '+new Intl.DateTimeFormat(undefined,options).format(date),exact:date.toLocaleString()};
  }
  function renderSources(){
    const names=sourceNames(),options=$('agent-options');options.replaceChildren();
    $('agent-all').checked=sources===null;
    $('agent-filter').textContent=sources===null?'All agents ▾':sources.size===1?[...sources][0]+' ▾':sources.size+' agents ▾';
    for(const name of names){const label=document.createElement('label'),check=document.createElement('input');label.className='filter-option';check.type='checkbox';check.checked=sources!==null&&sources.has(name);check.addEventListener('change',()=>{if(sources===null)sources=new Set();if(check.checked)sources.add(name);else sources.delete(name);if(!sources.size)sources=null;renderSources();render()});label.append(check,document.createTextNode(name));options.append(label)}
  }
  function closePeek(){peekGeneration++;$('peek-modal').hidden=true;document.querySelector('.app').inert=false;peekOpener?.focus()}
  async function openPeek(row,opener){
    peekOpener=opener;const generation=++peekGeneration;$('peek-title').textContent=row.title||'Untitled session';$('peek-meta').textContent=row.source+' · '+(row.project||'Unknown project')+' · '+activity(row.modified).label;$('peek-messages').textContent='Loading session…';$('peek-note').textContent='Read-only preview · your selection is preserved';$('peek-modal').hidden=false;document.querySelector('.app').inert=true;$('peek-close').focus();
    try{const data=await api('peek',{session:row.id});if(generation!==peekGeneration)return;$('peek-title').textContent=data.title||'Untitled session';$('peek-meta').textContent=data.source+' · '+(data.cwd||data.project)+' · '+activity(data.modified).label;const area=$('peek-messages');area.replaceChildren();if(!data.messages.length){area.textContent='No text messages found in this transcript.'}else{const beforeGap=Math.min(2,data.messages.length);for(let i=0;i<data.messages.length;i++){if(i===beforeGap&&data.omitted){const gap=document.createElement('div');gap.className='peek-gap';gap.textContent='… '+data.omitted+' earlier messages omitted …';area.append(gap)}const message=data.messages[i],turn=document.createElement('article'),role=document.createElement('span'),body=document.createElement('p');turn.className='peek-turn';role.className='peek-role';role.textContent=message.role;body.className='peek-text';body.textContent=message.text;turn.append(role,body);area.append(turn)}}if(data.approximate)$('peek-note').textContent='Read-only text preview of an in-progress session · your selection is preserved'}
    catch(err){if(generation===peekGeneration)$('peek-messages').textContent=err.message}
  }
  async function api(path,body){
    const response=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Teleporter-Token':token},body:JSON.stringify(body)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data;
  }
  function status(message,error=false,details=''){
    const el=$('status');el.replaceChildren();el.classList.toggle('error',error);el.append(document.createTextNode(message));
    if(details){const box=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='Transfer output';pre.textContent=details;box.append(summary,pre);el.append(box)}el.hidden=false;
  }
  function update(){
    const n=selected.size;$('count').textContent=n+' selected';$('transfer').textContent='Transfer '+(n?n+' ':'')+'session'+(n===1?'':'s')+' →';$('transfer').disabled=!n||busy;
    const match=rows().filter(eligible);$('all').checked=!!match.length&&match.every(r=>selected.has(r.id));$('all').indeterminate=match.some(r=>selected.has(r.id))&&!$('all').checked;$('all').disabled=!match.length||busy;
  }
  function render(){
    const list=$('list');list.replaceChildren();const found=rows();
    if(!found.length){const empty=document.createElement('p');empty.className='empty';empty.textContent=catalog.sessions.length?'No matching sessions.':'No sessions found on this computer.';list.append(empty)}
    for(const row of found){const line=document.createElement('div');line.className='row';const check=document.createElement('input');check.type='checkbox';check.checked=selected.has(row.id);check.disabled=!eligible(row)||busy;check.setAttribute('aria-label','Select '+row.title);check.addEventListener('click',e=>e.stopPropagation());check.addEventListener('change',()=>{if(check.checked){if(selected.size>=25){check.checked=false;status('Choose up to 25 sessions.',true)}else selected.add(row.id)}else selected.delete(row.id);update()});
      const body=document.createElement('button'),title=document.createElement('span'),project=document.createElement('span'),agent=document.createElement('span');body.type='button';body.className='row-main';body.setAttribute('aria-label','Preview '+row.title);title.className='title';title.textContent=row.title||'Untitled session';project.className='project';const when=activity(row.modified);project.textContent=(row.project||'Unknown project')+' · '+when.label+(row.missing?' · transcript missing':'');if(when.exact)project.title=when.exact;agent.className='agent';agent.textContent=row.source;body.append(title,project);line.append(check,body,agent);line.addEventListener('click',()=>openPeek(row,body));list.append(line)}update();
  }
  async function load(){
    try{catalog=await api('catalog',{});const old=target();$('target').replaceChildren();for(const dest of catalog.destinations){const option=document.createElement('option');option.value=dest.id;option.textContent=dest.name;$('target').append(option)}if(catalog.destinations.some(d=>d.id===old))$('target').value=old;if(sources!==null){sources=new Set([...sources].filter(name=>sourceNames().includes(name)));if(!sources.size)sources=null}selected=new Set([...selected].filter(id=>catalog.sessions.some(r=>r.id===id&&eligible(r))));renderSources();render()}
    catch(err){$('list').replaceChildren();status(err.message,true)}
  }
  function close(){preview=null;$('modal').hidden=true;document.querySelector('.app').inert=false;$('transfer').focus()}
  $('search').addEventListener('input',render);
  $('peek-close').addEventListener('click',closePeek);
  $('agent-filter').addEventListener('click',()=>{const open=$('agent-menu').hidden;$('agent-menu').hidden=!open;$('agent-filter').setAttribute('aria-expanded',String(open))});
  $('agent-all').addEventListener('change',()=>{sources=null;renderSources();render()});
  document.addEventListener('click',e=>{if(!$('agent-menu').hidden&&!e.target.closest('.filter-wrap')){$('agent-menu').hidden=true;$('agent-filter').setAttribute('aria-expanded','false')}});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('agent-menu').hidden){$('agent-menu').hidden=true;$('agent-filter').setAttribute('aria-expanded','false');$('agent-filter').focus()}});
  $('target').addEventListener('change',()=>{selected=new Set([...selected].filter(id=>catalog.sessions.some(r=>r.id===id&&eligible(r))));$('status').hidden=true;render()});
  $('all').addEventListener('change',e=>{for(const row of rows().filter(eligible)){if(e.target.checked&&selected.size<25)selected.add(row.id);else if(!e.target.checked)selected.delete(row.id)}render()});
  $('transfer').addEventListener('click',async()=>{
    busy=true;update();$('status').hidden=true;
    try{preview=await api('preview',{sessions:[...selected],to:target()});$('confirm-title').textContent='Transfer '+selected.size+' session'+(selected.size===1?'':'s')+' to '+preview.destination+'?';$('confirm-list').replaceChildren();for(const item of preview.sessions){const li=document.createElement('li'),small=document.createElement('small');li.textContent=item.title;small.textContent=item.source;li.append(small);$('confirm-list').append(li)}$('confirm-note').textContent='Original sessions stay intact. Existing sessions are never overwritten.'+(preview.cross_account?' This crosses Claude accounts.':'');$('details').textContent=preview.details;$('modal').hidden=false;document.querySelector('.app').inert=true;$('cancel').focus()}
    catch(err){status(err.message,true)}finally{busy=false;update()}
  });
  $('cancel').addEventListener('click',close);
  $('confirm').addEventListener('click',async()=>{
    if(!preview)return;const key=preview.preview;$('confirm').disabled=true;$('confirm').textContent='Transferring…';
    try{const result=await api('apply',{preview:key});close();const success=result.results.filter(r=>r.ok).length;const total=result.results.length;const output=result.results.map(r=>r.title+'\n'+r.output).join('\n\n');status(success+' of '+total+' session'+(total===1?'':'s')+' transferred to '+result.destination+'.'+(success<total?' Check transfer output.':''),success<total,output);selected.clear();await load()}
    catch(err){close();status(err.message,true)}finally{$('confirm').disabled=false;$('confirm').textContent='Transfer'}
  });
  document.addEventListener('keydown',e=>{if(!$('peek-modal').hidden){if(e.key==='Escape'){e.preventDefault();closePeek()}else if(e.key==='Tab'){e.preventDefault();$('peek-close').focus()}return}if($('modal').hidden)return;if(e.key==='Escape'){e.preventDefault();close()}if(e.key==='Tab'){const first=$('cancel'),last=$('confirm');if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}});
  load();
})();
</script>
</body>
</html>'''


def ui_codex_summary(path: Path) -> tuple[str, str] | None:
    """Read only the rollout head for the picker; preview validates the whole file."""
    try:
        with path.open(encoding='utf-8') as stream:
            header = json.loads(stream.readline())
            if header.get('type') != 'session_meta':
                return None
            cwd = header.get('payload', {}).get('cwd', '')
            if not isinstance(cwd, str):
                return None
            title = ''
            for _, line in zip(range(120), stream):
                if '"user_message"' not in line and '"input_text"' not in line:
                    continue
                record = json.loads(line)
                payload = record.get('payload', {})
                if record.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                    title = payload.get('message', '')
                elif (record.get('type') == 'response_item' and payload.get('type') == 'message'
                      and payload.get('role') == 'user'):
                    title = next((block.get('text', '') for block in payload.get('content', [])
                                  if isinstance(block, dict) and block.get('type') == 'input_text'), '')
                if isinstance(title, str) and title.strip():
                    return title.strip().splitlines()[0][:70], cwd
            return 'Codex session', cwd
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def ui_cli_summary(host: Host, path: Path) -> CliSession | None:
    """Read location from the head and a readable title from the tail."""
    session = read_cli_session(host, path, deep=False)
    if session is None:
        return None
    try:
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 262144))
            tail = stream.read().decode('utf-8', errors='replace')
    except OSError:
        return session
    title, prompt = '', ''
    for line in tail.splitlines():
        if '"ai-title"' in line:
            record = _loads(line)
            if record and record.get('type') == 'ai-title':
                title = record.get('aiTitle') or title
        elif '"last-prompt"' in line:
            record = _loads(line)
            if record and record.get('type') == 'last-prompt':
                prompt = record.get('lastPrompt') or prompt
    session.title = (title or prompt or session.title).strip().splitlines()[0][:70]
    return session


def ui_path_key(path: Path) -> str:
    """Compare ordinary and extended-length Windows paths as one file."""
    value = str(path.resolve())
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def ui_catalog() -> dict:
    """Discover resumable sessions from local clients and Claude partitions."""
    rows: list[dict] = []
    parts = load_partitions() if sessions_root().exists() else []
    used_transcripts: set[str] = set()
    adopted_ids = {(s.wsl_distro, s.cli_session_id) for p in parts for s in p.sessions if s.is_wsl}
    for p in parts:
        used_transcripts.update(ui_path_key(s.transcript) for s in p.sessions if s.transcript)
        for s in p.unarchived:
            rows.append({'id': f'p:{p.key}:{s.uuid}', 'kind': 'partition', 'title': s.title,
                         'project': re.split(r'[\\/]', s.cwd.rstrip('\\/'))[-1] or s.cwd,
                         'source': f'Claude · {p.name}', 'partition': p.key,
                         'distro': s.wsl_distro, 'cwd': s.cwd,
                         'missing': not bool(s.transcript), 'modified': s.last_activity})
    for path in agent_transcripts('claude', native_path(agent_home('claude'))):
        if ui_path_key(path) in used_transcripts:
            continue
        c = ui_cli_summary(WINDOWS_HOST, path)
        if c is None:
            continue
        rows.append({'id': 'c:' + str(path), 'kind': 'claude-cli', 'title': c.title,
                     'project': re.split(r'[\\/]', c.cwd.rstrip('\\/'))[-1] or c.cwd,
                     'source': 'Claude · CLI', 'cwd': c.cwd,
                     'modified': c.last_activity})
    for path in agent_transcripts('codex', native_path(agent_home('codex')))[:100]:
        summary = ui_codex_summary(path)
        if summary is None:
            continue
        title, cwd = summary
        rows.append({'id': 'x:' + str(path), 'kind': 'codex',
                     'title': title,
                     'project': re.split(r'[\\/]', cwd.rstrip('\\/'))[-1] or cwd,
                     'source': 'Codex', 'cwd': cwd,
                     'modified': int(path.stat().st_mtime * 1000)})
    for host in discover_hosts():
        if not host.is_wsl:
            continue
        try:
            paths = sorted(host.projects.glob('*/*.jsonl'),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        except OSError:
            continue
        for path in paths:
            c = ui_cli_summary(host, path)
            if c is None:
                continue
            if not c.born_in_cli or (host.distro, c.cli_id) in adopted_ids:
                continue
            rows.append({'id': f'w:{host.distro}:{c.cli_id}', 'kind': 'wsl',
                         'title': c.title,
                         'project': re.split(r'[\\/]', c.cwd.rstrip('\\/'))[-1] or c.cwd,
                         'source': f'Claude · {host.distro}', 'distro': host.distro,
                         'cwd': c.cwd, 'modified': c.last_activity})
    rows.sort(key=lambda r: -r['modified'])
    destinations = [{'id': 'codex', 'name': 'Codex'}]
    destinations += [{'id': 'claude:' + p.key, 'name': 'Claude · ' + p.name,
                      'signed_in': p.signed_in} for p in parts]
    return {'sessions': rows, 'destinations': destinations}


def ui_job(row: dict, target: str, parts: list[Partition]) -> tuple[list[str], Path]:
    """Map a discovered row to an existing dry-run-first CLI command."""
    if target != 'codex' and not target.startswith('claude:'):
        raise ValueError('Choose a destination')
    dst = next((p for p in parts if target == 'claude:' + p.key), None)
    if target != 'codex' and dst is None:
        raise ValueError('Destination organization is unavailable; refresh sessions')
    kind, row_id = row['kind'], row['id']
    if kind == 'partition':
        src = next((p for p in parts if row['partition'] == p.key), None)
        if src is None:
            raise ValueError('Source organization changed; refresh sessions')
        session = next((s for s in src.sessions if row_id == f'p:{src.key}:{s.uuid}'), None)
        if session is None or session.archived or not session.transcript:
            raise ValueError('Source transcript is unavailable; refresh sessions')
        if dst:
            if src.key == dst.key:
                raise ValueError('A session is already in the selected organization')
            if session.uuid in dst.tombstones or (dst.path / session.path.name).exists():
                raise ValueError('A selected session already exists or was deleted in the destination')
            argv = ['copy', '--from=' + src.key, '--to=' + dst.key,
                    '--session=' + session.uuid]
            if src.account != dst.account:
                argv.append('--allow-cross-account')
            return argv, session.transcript
        argv = ['teleport', '--to=codex', '--codex-project=auto']
        if session.is_wsl:
            argv.append('--cwd=' + wsl_to_win_path(session.cwd, session.wsl_distro or ''))
        return argv + ['--', str(session.transcript)], session.transcript
    if kind == 'wsl':
        _, distro, cli_id = row_id.split(':', 2)
        host = next((h for h in discover_hosts() if h.distro == distro and h.is_wsl), None)
        if host is None:
            raise ValueError('WSL source is unavailable; refresh sessions')
        session = next((c for c in scan_cli_sessions(host) if c.cli_id == cli_id and c.born_in_cli), None)
        if session is None:
            raise ValueError('WSL session changed; refresh sessions')
        if dst:
            if session.uuid in dst.tombstones or (dst.path / session.session_id).with_suffix('.json').exists():
                raise ValueError('A selected session already exists or was deleted in the destination')
            return ['adopt', '--from=' + host.name, '--to=' + dst.key,
                    '--session=' + session.cli_id], session.transcript
        return (['teleport', '--to=codex', '--codex-project=auto',
                 '--cwd=' + wsl_to_win_path(session.cwd, distro), '--', str(session.transcript)],
                session.transcript)
    if kind in ('codex', 'claude-cli'):
        if kind == 'codex' and target == 'codex':
            raise ValueError('A selected session is already in Codex')
        if kind == 'claude-cli' and dst:
            raise ValueError('Local Claude CLI sessions need a different host workflow')
        path = Path(row_id[2:])
        expected = agent_transcripts('codex' if kind == 'codex' else 'claude',
                                     native_path(agent_home('codex' if kind == 'codex' else 'claude')))
        if path not in expected:
            raise ValueError('Source transcript changed; refresh sessions')
        if dst:
            return (['teleport', '--to=claude', '--desktop-partition=' + dst.key,
                     '--', str(path)], path)
        return ['teleport', '--to=codex', '--codex-project=auto', '--', str(path)], path
    raise ValueError('Unknown session; refresh sessions')


def ui_batch(data: dict, catalog: dict | None = None) -> tuple[list[tuple[dict, list[str], Path]], str]:
    if set(data) != {'sessions', 'to'} or not isinstance(data['sessions'], list) or not isinstance(data['to'], str):
        raise ValueError('Choose sessions and a destination')
    ids = data['sessions']
    if not 0 < len(ids) <= 25 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Select 1 to 25 distinct sessions')
    catalog = catalog if catalog is not None else ui_catalog()
    if data['to'] not in {d['id'] for d in catalog['destinations']}:
        raise ValueError('Destination is unavailable; refresh sessions')
    found = {row['id']: row for row in catalog['sessions']}
    parts = load_partitions() if sessions_root().exists() else []
    jobs = []
    for row_id in ids:
        row = found.get(row_id)
        if row is None:
            raise ValueError('A selected session changed; refresh sessions')
        argv, source = ui_job(row, data['to'], parts)
        jobs.append((row, argv, source))
    return jobs, next(d['name'] for d in catalog['destinations'] if d['id'] == data['to'])


def ui_peek(data: dict, catalog: dict | None = None) -> dict:
    """Read a few turns from a discovered transcript without changing selection."""
    if set(data) != {'session'} or not isinstance(data['session'], str):
        raise ValueError('Choose one session to preview')
    catalog = catalog if catalog is not None else ui_catalog()
    row = next((r for r in catalog['sessions'] if r['id'] == data['session']), None)
    if row is None:
        raise ValueError('Session changed; refresh sessions')
    parts = load_partitions() if sessions_root().exists() else []
    if row['kind'] == 'partition':
        part = next(p for p in parts if p.key == row['partition'])
        session = next(s for s in part.sessions if s.uuid == row['id'].rsplit(':', 1)[1])
        path = session.transcript
    elif row['kind'] == 'wsl':
        _, distro, cli_id = row['id'].split(':', 2)
        host = next((h for h in discover_hosts() if h.is_wsl and h.distro == distro), None)
        if host is None:
            raise ValueError('WSL source changed; refresh sessions')
        cli = next((s for s in scan_cli_sessions(host) if s.cli_id == cli_id), None)
        path = cli.transcript if cli else None
    else:
        path = Path(row['id'][2:])
    if path is None or not path.is_file():
        raise ValueError('Transcript is unavailable')
    from collections import deque
    first: list[dict] = []
    last: deque[dict] = deque(maxlen=6)
    count = 0

    def add(role: str, content: str) -> None:
        nonlocal count
        if role not in ('user', 'assistant') or not isinstance(content, str) or not content.strip():
            return
        item = {'role': role, 'text': content.strip()[:1200]}
        count += 1
        if len(first) < 2:
            first.append(item)
        else:
            last.append(item)

    try:
        session = read_portable_session(path)
        for message in session.messages:
            add(message.role, message.text)
        approximate = False
    except (OSError, ValueError):
        # In-progress sessions can have incomplete tool calls. Text is still
        # useful for a read-only peek, even when a transfer cannot be planned.
        approximate = True
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                record = _loads(line)
                if not record:
                    continue
                payload = record.get('payload') or {}
                if record.get('type') == 'response_item' and payload.get('type') == 'message':
                    content = payload.get('content') or []
                    add(payload.get('role', ''), '\n'.join(block.get('text', '') for block in content
                                                       if isinstance(block, dict) and isinstance(block.get('text'), str)))
                elif record.get('type') in ('user', 'assistant'):
                    content = (record.get('message') or {}).get('content')
                    if isinstance(content, str):
                        add(record['type'], content)
                    elif isinstance(content, list):
                        add(record['type'], '\n'.join(block.get('text', '') for block in content
                                                      if isinstance(block, dict) and isinstance(block.get('text'), str)))
    return {'title': row['title'], 'source': row['source'], 'project': row['project'],
            'modified': row['modified'], 'cwd': row.get('cwd', ''),
            'messages': first + list(last), 'omitted': max(0, count - len(first) - len(last)),
            'approximate': approximate}


def ui_run(argv: list[str], apply: bool = False) -> dict:
    from contextlib import redirect_stdout, redirect_stderr
    from io import StringIO
    output = StringIO()
    # HTTPServer is serial, so output capture and destination writes cannot race.
    with redirect_stdout(output), redirect_stderr(output):
        try:
            code = main(['--ascii', *([argv[0], '--apply', *argv[1:]] if apply else argv)])
        except SystemExit as exc:
            code = exc.code or 0
        except (OSError, ValueError) as exc:
            print(str(exc))
            code = 1
    return {'ok': code == 0, 'output': output.getvalue()}


def ui_digest(jobs: list[tuple[dict, list[str], Path]]) -> str:
    digest = hashlib.sha256()
    for row, _, source in jobs:
        digest.update(row['id'].encode())
        paths = [source]
        if row['kind'] == 'partition':
            account, org = row['partition'].split('/', 1)
            uuid = row['id'].rsplit(':', 1)[1]
            paths.append(sessions_root() / account / org / f'local_{uuid}.json')
        for path in paths:
            digest.update(str(path).encode())
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
    return digest.hexdigest()


def ui_preview(data: dict, catalog: dict | None = None) -> tuple[list[tuple[dict, list[str], Path]], str, str, list[str]]:
    jobs, destination = ui_batch(data, catalog)
    before = ui_digest(jobs)
    outputs = []
    for row, argv, _ in jobs:
        result = ui_run(argv)
        if not result['ok'] or 'already exists' in result['output'] or 'skip:' in result['output']:
            raise ValueError(f"{row['title']}: {result['output'].strip() or 'Cannot preview transfer'}")
        outputs.append(result['output'])
    if before != ui_digest(jobs):
        raise ValueError('A source changed during preview; finish the running turn and try again')
    return jobs, destination, before, outputs


def make_ui_server(port: int = 0):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import hmac
    import secrets
    token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    pending: dict[str, tuple] = {}
    catalog_cache: dict = {'at': 0.0, 'value': None}

    def recent_catalog() -> dict | None:
        return catalog_cache['value'] if time.monotonic() - catalog_cache['at'] < 30 else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Session titles, paths and auth material stay out of access logs.

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def reply(self, status, data, html=False):
            body = (data if html else json.dumps(data, ensure_ascii=False)).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; "
                             f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                             "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def trusted(self):
            host = f'127.0.0.1:{self.server.server_port}'
            origin = self.headers.get('Origin')
            return (self.headers.get('Host') == host
                    and (origin is None or origin == 'http://' + host)
                    and self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site'))

        def do_GET(self):
            if not self.trusted():
                self.reply(403, {'error': 'This UI is local to this computer'})
            elif self.path == '/':
                self.reply(200, UI_HTML.replace('__NONCE__', nonce), html=True)
            else:
                self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.trusted() or not hmac.compare_digest(self.headers.get('X-Teleporter-Token', ''), token):
                self.reply(403, {'error': 'Open the complete launch URL from your terminal to connect'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Expected a JSON request of at most 64 KiB')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
                self.dispatch(data)
            except (ValueError, OSError) as exc:
                self.reply(400, {'error': str(exc)})
            except SystemExit:
                self.reply(400, {'error': 'Session selection changed; refresh and try again'})

        def dispatch(self, data):
            if self.path == '/api/catalog':
                if data:
                    raise ValueError('Catalog request takes no options')
                catalog_cache['value'] = ui_catalog()
                catalog_cache['at'] = time.monotonic()
                self.reply(200, catalog_cache['value'])
            elif self.path == '/api/peek':
                self.reply(200, ui_peek(data, recent_catalog()))
            elif self.path == '/api/preview':
                jobs, destination, digest, outputs = ui_preview(data, recent_catalog())
                now = time.monotonic()
                for key, value in list(pending.items()):
                    if now - value[0] > 600:
                        del pending[key]
                if len(pending) >= 32:
                    del pending[next(iter(pending))]
                key = secrets.token_urlsafe(24)
                pending[key] = (now, data, digest, outputs)
                self.reply(200, {'preview': key, 'destination': destination,
                                 'sessions': [{'title': row['title'], 'source': row['source']}
                                              for row, _, _ in jobs],
                                 'cross_account': any('--allow-cross-account' in argv for _, argv, _ in jobs),
                                 'details': '\n\n'.join(outputs)})
            elif self.path == '/api/apply':
                if set(data) != {'preview'} or not isinstance(data['preview'], str):
                    raise ValueError('Apply requires a preview ID only')
                plan = pending.pop(data['preview'], None)
                if plan is None or time.monotonic() - plan[0] > 600:
                    raise ValueError('Preview expired or already used; select sessions again')
                _, request, digest, outputs = plan
                jobs, destination, current_digest, current_outputs = ui_preview(request)
                if digest != current_digest or outputs != current_outputs:
                    raise ValueError('Transfer changed since confirmation; select sessions again')
                results = []
                for row, argv, _ in jobs:
                    result = ui_run(argv, apply=True)
                    succeeded = result['ok'] and not any(marker in result['output'] for marker in
                                                           ('already exists', 'Nothing to do.', 'appeared in destination',
                                                            'Copied 0 session', 'Adopted 0 session'))
                    results.append({'title': row['title'], 'ok': succeeded, 'output': result['output']})
                self.reply(200, {'destination': destination, 'results': results,
                                 'ok': all(r['ok'] for r in results)})
            else:
                self.reply(404, {'error': 'Not found'})

    server = HTTPServer(('127.0.0.1', port), Handler)
    server.launch_url = f'http://127.0.0.1:{server.server_port}/#{token}'
    return server


def cmd_ui(args) -> int:
    import webbrowser
    try:
        with make_ui_server(args.port) as server:
            print(f'Claude Session Teleporter\nOpen this local URL: {server.launch_url}', flush=True)
            print('Keep this terminal open. Press Ctrl+C to stop. No session data is uploaded.', flush=True)
            if not args.no_browser:
                try:
                    webbrowser.open(server.launch_url)
                except webbrowser.Error:
                    pass
            server.serve_forever()
    except KeyboardInterrupt:
        print('\nUI stopped.')
    except OSError as exc:
        die(f'Cannot start browser UI: {exc}')
    return 0


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

    ui = sub.add_parser('ui', help='open a local browser UI (no extra dependencies)')
    ui.add_argument('--port', type=int, choices=range(0, 65536), metavar='PORT', default=0,
                    help='loopback port (default: choose a free port)')
    ui.add_argument('--no-browser', action='store_true', help='print the URL without opening a browser')
    ui.set_defaults(fn=cmd_ui)

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
    sp.add_argument("--agent", choices=("claude", "codex"), help="list transcript paths for cross-client teleport (20 most recent by default)")
    sp.add_argument("--home", metavar="DIR", help="with --agent: read this .claude/.codex directory")
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

    tp = sub.add_parser(
        "teleport", help="fork conversation history between Claude and Codex",
        description="Convert a Claude transcript or Codex rollout into a new resumable session. "
        "Dry run by default. Tools retain native structure; permissions and credentials never transfer.",
    )
    tp.add_argument("transcript", help="source Claude JSONL transcript or Codex rollout file")
    tp.add_argument("--to", required=True, choices=("claude", "codex"))
    tp.add_argument("--target-home", metavar="DIR", help="target .claude/.codex directory (defaults to the client's environment variable or home)")
    tp.add_argument("--cwd", help="destination working directory; required if the source path is unavailable")
    tp.add_argument("--target-host", metavar="HOST", help="Claude destination host, e.g. wsl:Ubuntu (uses that host's .claude)")
    tp.add_argument("--desktop-partition", metavar="SEL", help="also create Claude desktop metadata, e.g. active")
    tp.add_argument("--apply", action="store_true", help="actually create the fork (default is dry run)")
    tp.set_defaults(fn=cmd_teleport)

    cp = sub.add_parser('codex-project', help='assign an existing Codex rollout to a desktop project')
    cp.add_argument('transcript', help='existing rollout inside the destination CODEX_HOME/sessions')
    cp.add_argument('--target-home', metavar='DIR', help='destination CODEX_HOME')
    cp.add_argument('--apply', action='store_true', help='assign the project (default is dry run)')
    cp.set_defaults(fn=cmd_codex_project)
    for parser in (tp, cp):
        parser.add_argument('--codex-project', metavar='auto|ID|NAME' if parser is cp else 'auto|none|ID|NAME',
                            default='auto' if parser is cp else None,
                            help='Codex imports default to auto: reuse an exact folder match or create a project; '
                                 'use none with teleport for transcript-only import')
        parser.add_argument('--codex-bin', default='codex', metavar='PATH',
                            help='Codex executable with project APIs (use the desktop-bundled version if needed)')
        parser.add_argument('--codex-sqlite-home', metavar='DIR',
                            help='destination CODEX_SQLITE_HOME when the desktop uses a separate index')

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
