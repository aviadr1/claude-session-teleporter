"""Build a fake Claude Code store under a scratch home. Never touches the real one."""
import json
import os
import sys
import time
from pathlib import Path

home = Path(sys.argv[1])
assert "cst-scratch" in str(home), home
appdata = home / "AppData" / "Roaming"
store = appdata / "Claude" / "claude-code-sessions"
projects = home / ".claude" / "projects"

ACCOUNT = "1eb44d48-7a51-4c1e-9d2a-5b0c7e3f9a11"
WORK = "3c426532-1eaa-4e6f-93c1-4d30abca7b89"
PERSONAL = "762f7f2a-1cab-4c8a-98d1-d53bf5e8872c"

now = int(time.time() * 1000)
HOUR = 3_600_000


def enc(cwd: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in cwd)


def conn(name, uuid, tools):
    return {"uuid": uuid, "name": name, "tools": [{"name": t} for t in tools]}


WORK_CONN = [
    conn("Linear", "01812872-0000-4000-8000-000000000001", ["list_issues"]),
    conn("Sentry", "e5c4f439-0000-4000-8000-000000000002", ["search_issues"]),
    conn("Datadog", "9d1a0b7e-0000-4000-8000-000000000003", ["search_logs"]),
]
PERSONAL_CONN = [
    conn("Linear", "4b57c823-0000-4000-8000-000000000011", ["list_issues"]),
    conn("Sentry", "be036bca-0000-4000-8000-000000000012", ["search_issues"]),
]


def session(part, uuid, cli, title, cwd, connectors, age_h, archived=False, **extra):
    d = store / ACCOUNT / part
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "sessionId": f"local_{uuid}", "cliSessionId": cli, "cwd": cwd, "originCwd": cwd,
        "createdAt": now - (age_h + 2) * HOUR, "lastActivityAt": now - age_h * HOUR,
        "lastFocusedAt": now - age_h * HOUR, "model": "claude-opus-5", "effort": "high",
        "isArchived": archived, "title": title, "titleSource": "auto", "permissionMode": "auto",
        "enabledMcpTools": {f"{c['uuid']}:{c['tools'][0]['name']}": True for c in connectors},
        "remoteMcpServersConfig": connectors, "alwaysAllowedReasons": [],
        "sessionPermissionUpdates": [], "spawnSeed": {},
    }
    data.update(extra)
    (d / f"local_{uuid}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    t = projects / enc(cwd)
    t.mkdir(parents=True, exist_ok=True)
    rec = {"type": "user", "cwd": cwd, "timestamp": "2026-09-27T10:00:00.000Z",
           "version": "2.1.233", "gitBranch": "main", "entrypoint": "claude-desktop",
           "userType": "external", "origin": {"kind": "human"}}
    (t / f"{cli}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


repo = r"C:\Users\you\projects\api"
session(WORK, "5651c527-1111-4111-8111-000000000001", "a1b2c3d4-0000-4000-8000-000000000001",
        "Fix flaky auth test", repo, WORK_CONN, 1,
        error="process exited", errorAt=now - HOUR, errorCategory="crash")
session(WORK, "7c0ffee0-2222-4222-8222-000000000002", "a1b2c3d4-0000-4000-8000-000000000002",
        "Migrate billing webhooks", repo, WORK_CONN, 5)
session(WORK, "0ddba11a-3333-4333-8333-000000000003", "a1b2c3d4-0000-4000-8000-000000000003",
        "Old spike", repo, WORK_CONN, 90, archived=True)
session(PERSONAL, "b16b00b5-4444-4444-8444-000000000004", "a1b2c3d4-0000-4000-8000-000000000004",
        "Blog post draft", r"C:\Users\you\projects\blog", PERSONAL_CONN, 3)

(home / ".claude.json").write_text(json.dumps({"oauthAccount": {
    "accountUuid": ACCOUNT, "organizationUuid": PERSONAL, "organizationName": "personal",
    "emailAddress": "you@example.com"}}), encoding="utf-8")
(appdata / "Claude" / "plan-usage-history.json").write_text(json.dumps({"samples": [
    {"org": WORK, "t": now - HOUR, "u": {"fh": 98, "sd": 71}},
    {"org": PERSONAL, "t": now - 2 * HOUR, "u": {"fh": 4, "sd": 12}},
]}), encoding="utf-8")
print("fixture at", home)
