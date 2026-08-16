"""
A fake machine to run the tool against.

Every test builds a `World`: a session store with real-shaped partitions, a
transcript pool, and a WSL host whose "distro" is just a temp directory. The
tool is pointed at it by patching the module-level locations, so tests exercise
the real code paths - including argument parsing, via `world.run(...)`.

Nothing here touches the developer's own Claude Code state.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    """Import claude_sessions.py by path - it is a script, not a package."""
    spec = importlib.util.spec_from_file_location("claude_sessions", ROOT / "claude_sessions.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_sessions"] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load_module()


# --------------------------------------------------------------------------
# builders for the two file formats the tool reads
# --------------------------------------------------------------------------


def connector(name: str, uuid: str, tools: list[str]) -> dict:
    return {"uuid": uuid, "name": name, "tools": [{"name": t} for t in tools]}


def session_json(
    uuid: str,
    cli_id: str,
    cwd: str = r"C:\repo",
    title: str = "a session",
    connectors: list[dict] | None = None,
    enabled_tools: dict | None = None,
    **extra,
) -> dict:
    """Desktop metadata, shaped like the real thing."""
    data = {
        "sessionId": f"local_{uuid}",
        "cliSessionId": cli_id,
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": 1_700_000_000_000,
        "lastActivityAt": 1_700_000_100_000,
        "lastFocusedAt": 1_700_000_100_000,
        "model": "claude-opus-5",
        "effort": "high",
        "isArchived": False,
        "title": title,
        "titleSource": "auto",
        "permissionMode": "auto",
        "enabledMcpTools": enabled_tools if enabled_tools is not None else {},
        "remoteMcpServersConfig": connectors if connectors is not None else [],
        "alwaysAllowedReasons": [],
        "sessionPermissionUpdates": [],
        "spawnSeed": {},
    }
    data.update(extra)
    return data


def record(
    cwd: str,
    *,
    entrypoint: str = "cli",
    ts: str = "2026-08-15T10:00:00.000Z",
    branch: str = "main",
    human: bool = True,
    type_: str = "user",
) -> dict:
    r = {
        "type": type_,
        "cwd": cwd,
        "timestamp": ts,
        "version": "2.1.233",
        "gitBranch": branch,
        "entrypoint": entrypoint,
        "userType": "external",
    }
    if human:
        r["origin"] = {"kind": "human"}
    return r


def transcript(records: list[dict], title: str | None = None, last_prompt: str | None = None) -> str:
    """Serialise records the way Claude Code does - compact, one json per line."""
    lines = [json.dumps(r, separators=(",", ":")) for r in records]
    if title:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": title}, separators=(",", ":")))
    if last_prompt:
        lines.append(
            json.dumps({"type": "last-prompt", "lastPrompt": last_prompt}, separators=(",", ":"))
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# the world
# --------------------------------------------------------------------------


@dataclass
class World:
    tmp: Path
    monkeypatch: object
    store: Path = field(init=False)
    projects: Path = field(init=False)
    wsl_root: Path = field(init=False)
    wsl: object = field(init=False)

    def __post_init__(self):
        self.store = self.tmp / "store"
        self.projects = self.tmp / "windows-home" / ".claude" / "projects"
        self.wsl_root = self.tmp / "distro"
        for p in (self.store, self.projects, self.wsl_root):
            p.mkdir(parents=True, exist_ok=True)

        self.monkeypatch.setenv("CLAUDE_SESSIONS_ROOT", str(self.store))
        self.monkeypatch.setattr(cs, "PROJECTS_DIR", self.projects)
        self.monkeypatch.setattr(cs, "CLAUDE_JSON", self.tmp / ".claude.json")
        self.monkeypatch.setattr(cs, "LABELS_PATH", self.tmp / "partition-labels.json")
        self.monkeypatch.setattr(cs, "LEDGER_PATH", self.tmp / "session-copy-ledger.json")

        self.wsl = cs.Host(
            kind="wsl", distro="Testbuntu", user="me", posix_home="/home/me", mount=self.wsl_root
        )
        # discover_hosts() shells out to wsl.exe; pin the result instead
        self.monkeypatch.setattr(cs, "_HOST_CACHE", [cs.WINDOWS_HOST, self.wsl])
        (self.wsl.projects).mkdir(parents=True, exist_ok=True)

    # -- store -------------------------------------------------------------

    def partition(self, account: str, org: str) -> Path:
        p = self.store / account / org
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_session(self, part: Path, uuid: str, cli_id: str, **kw) -> Path:
        f = part / f"local_{uuid}.json"
        f.write_text(json.dumps(session_json(uuid, cli_id, **kw), indent=2), encoding="utf-8")
        return f

    def add_tombstone(self, part: Path, uuid: str) -> Path:
        f = part / f"deleted_{uuid}"
        f.write_text("", encoding="utf-8")
        return f

    def sign_in(self, account: str, org: str, name: str = "TestOrg") -> None:
        (self.tmp / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "accountUuid": account,
                        "organizationUuid": org,
                        "organizationName": name,
                        "emailAddress": "me@example.com",
                    }
                }
            ),
            encoding="utf-8",
        )

    # -- transcripts -------------------------------------------------------

    def add_transcript(self, cwd: str, cli_id: str, records=None, **kw) -> Path:
        """A transcript on the Windows side, in the directory `cwd` encodes to."""
        d = self.projects / cs.encode_cwd(cwd)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{cli_id}.jsonl"
        f.write_text(transcript(records or [record(cwd)], **kw), encoding="utf-8")
        return f

    def add_wsl_transcript(
        self, cwd: str, cli_id: str, records=None, project_dir: str | None = None, **kw
    ) -> Path:
        """
        A transcript inside the distro.

        `project_dir` overrides the directory, which is how a moved session is
        modelled: the directory names where it ended up, while early records
        still carry the cwd it started in.
        """
        d = self.wsl.projects / (project_dir or cs.encode_cwd(cwd))
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{cli_id}.jsonl"
        f.write_text(transcript(records or [record(cwd)], **kw), encoding="utf-8")
        return f

    # -- running the tool --------------------------------------------------

    def run(self, *argv: str) -> int:
        """Invoke the CLI exactly as a user would, argument parsing included."""
        return cs.main(["--ascii", *argv])

    def snapshot(self, *paths: Path) -> dict[str, bytes]:
        """Every file under `paths`, by content - for asserting nothing changed."""
        out: dict[str, bytes] = {}
        for base in paths:
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    out[str(f.relative_to(base))] = f.read_bytes()
        return out


@pytest.fixture
def world(tmp_path, monkeypatch):
    cs.G = cs.make_glyphs(True)  # ascii, so assertions never fight box drawing
    return World(tmp_path, monkeypatch)


@pytest.fixture
def two_orgs(world):
    """
    The common setup: one account, two orgs, the same connectors with different
    uuids in each - which is the whole reason porting is not a file copy.
    """
    world.LINEAR_A, world.LINEAR_B = "aaaa1111-0000-0000-0000-000000000001", "bbbb2222-0000-0000-0000-000000000002"
    world.SENTRY_A, world.SENTRY_B = "aaaa1111-0000-0000-0000-000000000003", "bbbb2222-0000-0000-0000-000000000004"
    world.ACCOUNT = "acct-0000-0000-0000-000000000001"
    world.ORG_A, world.ORG_B = "orga-0000-0000-0000-00000000000a", "orgb-0000-0000-0000-00000000000b"
    world.a = world.partition(world.ACCOUNT, world.ORG_A)
    world.b = world.partition(world.ACCOUNT, world.ORG_B)
    world.sign_in(world.ACCOUNT, world.ORG_B)

    # a native session in each org, establishing that org's connector uuids
    world.add_session(
        world.a, "1111aaaa-0000-4000-8000-000000000001", "cli-a-1",
        title="work in A",
        connectors=[connector("Linear", world.LINEAR_A, ["issue"]),
                    connector("Sentry", world.SENTRY_A, ["errors"])],
        enabled_tools={f"{world.LINEAR_A}:issue": True, f"{world.SENTRY_A}:errors": True},
    )
    world.add_transcript(r"C:\repo", "cli-a-1")
    world.add_session(
        world.b, "2222bbbb-0000-4000-8000-000000000002", "cli-b-1",
        title="work in B",
        connectors=[connector("Linear", world.LINEAR_B, ["issue"]),
                    connector("Sentry", world.SENTRY_B, ["errors"])],
        enabled_tools={f"{world.LINEAR_B}:issue": True, f"{world.SENTRY_B}:errors": True},
    )
    world.add_transcript(r"C:\repo", "cli-b-1")
    return world
