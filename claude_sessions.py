#!/usr/bin/env python3
"""
claude_sessions.py - inspect and migrate Claude Code desktop sessions across
account/org partitions.

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

Commands:
  partitions   list every account/org partition on this machine
  sessions     list unarchived sessions (--all to include archived)
  active       explain which partition is "active", three ways
  copy         copy sessions between partitions (dry-run by default)
  label        give a partition a human-readable name

Safety invariants for `copy`:
  * never overwrites an existing session in the destination (no --force exists)
  * never writes over a `deleted_<id>` tombstone (a session you deleted there)
  * never modifies or removes anything in the source partition
  * never touches transcripts - they are shared, not duplicated
  * refuses a session whose transcript is missing (nothing to continue)
  * dry-run is the default; --apply is required to write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
    def is_remote(self) -> bool:
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

        Built only from natively-created sessions: sessions this tool imported
        carry the source org's uuids and would poison the map. Ties go to the
        most recently active session.
        """
        out: dict[str, str] = {}
        for s in sorted(self.sessions, key=lambda x: x.last_activity):
            if s.uuid in self.imported:
                continue
            out.update(s.servers)
        return out


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
                sess.transcript = tindex.get(sess.cli_session_id)
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
    print(f"{G.warn} {msg}", file=sys.stderr)


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


def port_session(sess: Session, dst: Partition, keep_error: bool) -> tuple[dict, list[str]]:
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

    tools = data.get("enabledMcpTools")
    if isinstance(tools, dict):
        new_tools = {}
        for key, val in tools.items():
            uuid, sep, tool = key.partition(":")
            if not sep:
                new_tools[key] = val
            elif uuid in dropped:
                continue
            else:
                new_tools[f"{remap.get(uuid, uuid)}:{tool}"] = val
        data["enabledMcpTools"] = new_tools

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


def cmd_sessions(args) -> int:
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
            flags = ("A" if s.archived else " ") + (" " if s.transcript else "!") + ("R" if s.is_remote else " ")
            rows.append(
                [
                    flags,
                    s.uuid[:8],
                    ts(s.last_activity),
                    trunc(s.title, 54),
                    trunc(s.branch or Path(s.cwd).name, 34),
                ]
            )
        print(table(["FLG", "ID", "LAST ACTIVITY", "TITLE", "BRANCH/DIR"], rows))
        print(
            f"({len(sel)} {'all' if args.all else 'unarchived'} of {len(p.sessions)})"
            f"   flags: A=archived  !=transcript missing  R=ssh/remote\n"
        )
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
            _, notes = port_session(s, dst, args.keep_error)
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
        payload, _ = port_session(s, dst, args.keep_error)
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
    print("The app caches its session list in memory - restart Claude to see them.")
    return 0


def cmd_label(args) -> int:
    p = resolve_partition(load_partitions(), args.partition)
    labels = _read_json(LABELS_PATH, {})
    labels[p.org] = args.name
    _write_json(LABELS_PATH, labels)
    print(f"{G.check} {p.org} labelled {args.name!r}  ({LABELS_PATH})")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="claude_sessions",
        description="Inspect and migrate Claude Code sessions across account/org partitions.",
    )
    ap.add_argument("--ascii", action="store_true", help="force plain ASCII output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("partitions", help="list account/org partitions").set_defaults(fn=cmd_partitions)

    sp = sub.add_parser("sessions", help="list sessions in each partition")
    sp.add_argument("-p", "--partition", help="selector: label, org uuid prefix, or 'active'")
    sp.add_argument("-a", "--all", action="store_true", help="include archived sessions")
    sp.set_defaults(fn=cmd_sessions)

    sub.add_parser("active", help="explain which partition is active").set_defaults(fn=cmd_active)

    cp = sub.add_parser("copy", help="copy sessions between partitions (dry-run by default)")
    cp.add_argument("--from", dest="source", help="source partition (default: the only other one with work)")
    cp.add_argument("--to", help="destination partition (default: the signed-in one)")
    cp.add_argument("-s", "--session", action="append", default=[], help="id/prefix/title substring (repeatable)")
    cp.add_argument("--include-archived", action="store_true", help="also copy archived sessions")
    cp.add_argument("--keep-error", action="store_true", help="preserve previous crash state")
    cp.add_argument("--allow-cross-account", action="store_true", help="permit copying between accounts")
    cp.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")
    cp.set_defaults(fn=cmd_copy)

    lp = sub.add_parser("label", help="give a partition a readable name")
    lp.add_argument("partition")
    lp.add_argument("name")
    lp.set_defaults(fn=cmd_label)

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
