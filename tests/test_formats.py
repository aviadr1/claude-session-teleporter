"""
Format invariants - see INVARIANTS.md, section "Format".

These encode what was learned by reading real session files. They are the
claims most likely to be falsified by an Anthropic release, which is exactly
why each one is pinned here rather than living in someone's head.
"""

import json

import pytest
from conftest import connector, cs, record


# --------------------------------------------------------------------------
# F1 - project directory encoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cwd,encoded",
    [
        (r"C:\Users\a\GitHub\repo", "C--Users-a-GitHub-repo"),
        ("/home/a/projects/repo", "-home-a-projects-repo"),
        (r"C:\r\.claude\worktrees\wt", "C--r--claude-worktrees-wt"),
        ("/mnt/c/Users/a/x", "-mnt-c-Users-a-x"),
        ("/a_b/c.d-e", "-a-b-c-d-e"),
        ("/träsh/ünicode", "-tr-sh--nicode"),
    ],
)
def test_encode_cwd(cwd, encoded):
    assert cs.encode_cwd(cwd) == encoded


def test_encode_cwd_is_every_non_alphanumeric():
    """The rule, stated once: alphanumerics survive, everything else is a dash."""
    src = "aZ09 /\\:.-_@#"
    assert cs.encode_cwd(src) == "aZ09" + "-" * (len(src) - 4)


# --------------------------------------------------------------------------
# F2 - locate by directory, never by re-encoding a cwd
# --------------------------------------------------------------------------


def test_moved_session_locates_by_directory(world):
    """
    A session that started in a repo and ended up in a worktree. Claude Code
    re-homes the transcript, so the directory names the destination while early
    records still say the origin.
    """
    w = world
    started, ended = "/home/me/repo", "/home/me/repo/.claude/worktrees/wt"
    f = w.add_wsl_transcript(
        started, "moved-1",
        records=[record(started, ts="2026-08-15T10:00:00.000Z"),
                 record(ended, ts="2026-08-15T11:00:00.000Z")],
        project_dir=cs.encode_cwd(ended),
        title="a session that moved",
    )

    c = cs.read_cli_session(w.wsl, f)

    assert c.project_dir == cs.encode_cwd(ended)
    assert c.cwd == ended, "cwd must be where it ended up"
    assert c.origin_cwd == started
    assert c.moved
    # the whole point: the path handed to the app must exist
    assert (w.wsl.projects / c.project_dir / "moved-1.jsonl").exists()
    assert cs.encode_cwd(c.origin_cwd) != c.project_dir, "fixture must actually disagree"


def test_locator_ignores_cwd_when_directory_disagrees(world):
    """
    The strongest form of F2, and the case a moved-session fixture cannot reach.

    When a cwd in the transcript happens to encode to the directory, locating by
    either route gives the same answer, so a locator that re-encodes a cwd looks
    correct. Here the directory matches NO recorded cwd - which is exactly what
    the app's own `ssh-<cliSessionId>` mirror directories look like. Only the
    filesystem knows where the file is.
    """
    w = world
    f = w.add_wsl_transcript("/home/me/repo", "orphan-1", project_dir="ssh-orphan-1")

    c = cs.read_cli_session(w.wsl, f)

    assert c.project_dir == "ssh-orphan-1"
    assert cs.encode_cwd(c.cwd) != c.project_dir, "fixture must disagree with every cwd"
    located = w.wsl.posix_transcript_path(c.project_dir, c.cli_id)
    assert located == "/home/me/.claude/projects/ssh-orphan-1/orphan-1.jsonl"
    assert (w.wsl_root / located.lstrip("/")).exists()


def test_adopt_path_exists_when_directory_matches_no_cwd(two_orgs):
    """The same trap, reached through adopt: the path written must resolve."""
    w = two_orgs
    w.add_wsl_transcript("/home/me/repo", "orphan-1", project_dir="ssh-orphan-1", title="orphan")

    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")

    adopted = next(
        json.loads(f.read_text(encoding="utf-8"))
        for f in w.b.glob("local_*.json")
        if json.loads(f.read_text(encoding="utf-8")).get("wslConfig")
    )
    posix = adopted["sshRemoteTranscriptPath"]
    assert "ssh-orphan-1" in posix, "must use the real directory, not one derived from cwd"
    assert (w.wsl_root / posix.lstrip("/")).exists(), f"adopt wrote a dead path: {posix}"


def test_adopt_path_exists_for_moved_session(two_orgs):
    w = two_orgs
    started, ended = "/home/me/repo", "/home/me/repo/.claude/worktrees/wt"
    w.add_wsl_transcript(
        started, "moved-1",
        records=[record(started), record(ended, ts="2026-08-15T11:00:00.000Z")],
        project_dir=cs.encode_cwd(ended),
        title="moved",
    )

    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")

    adopted = next(
        json.loads(f.read_text(encoding="utf-8"))
        for f in w.b.glob("local_*.json")
        if json.loads(f.read_text(encoding="utf-8")).get("wslConfig")
    )
    # the path the app will open, translated back to something we can check
    posix = adopted["sshRemoteTranscriptPath"]
    assert posix.startswith("/home/me/.claude/projects/")
    local = w.wsl_root / posix.lstrip("/")
    assert local.exists(), f"adopt wrote a path that does not exist: {posix}"
    assert adopted["cwd"] == ended
    assert adopted["originCwd"] == started


# --------------------------------------------------------------------------
# F3 - cwd vs originCwd
# --------------------------------------------------------------------------


def test_cwd_and_origin_cwd(world):
    w = world
    f = w.add_wsl_transcript(
        "/home/me/a", "s1",
        records=[record("/home/me/a"), record("/home/me/b", ts="2026-08-15T11:00:00.000Z")],
        project_dir=cs.encode_cwd("/home/me/b"),
    )
    c = cs.read_cli_session(w.wsl, f)
    assert (c.origin_cwd, c.cwd) == ("/home/me/a", "/home/me/b")


def test_round_trip_is_not_a_move(world):
    """Visiting a subdirectory and coming back is not a move worth reporting."""
    w = world
    f = w.add_wsl_transcript(
        "/home/me/a", "s2",
        records=[record("/home/me/a"),
                 record("/home/me/a/sub", ts="2026-08-15T11:00:00.000Z"),
                 record("/home/me/a", ts="2026-08-15T12:00:00.000Z")],
    )
    c = cs.read_cli_session(w.wsl, f)
    assert c.cwd == "/home/me/a"
    assert not c.moved


def test_shallow_scan_agrees_with_deep_scan(world):
    """`hosts` uses a cheap scan; it must not report a different cwd."""
    w = world
    started, ended = "/home/me/repo", "/home/me/repo/.claude/worktrees/wt"
    f = w.add_wsl_transcript(
        started, "s3",
        records=[record(started), record(ended, ts="2026-08-15T11:00:00.000Z")],
        project_dir=cs.encode_cwd(ended),
    )
    shallow = cs.read_cli_session(w.wsl, f, deep=False)
    deep = cs.read_cli_session(w.wsl, f, deep=True)
    assert shallow.cwd == deep.cwd == ended


# --------------------------------------------------------------------------
# F4 - path mappings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "win,posix",
    [
        (r"C:\Users\me\x", "/mnt/c/Users/me/x"),
        (r"D:\x", "/mnt/d/x"),
        ("C:/Users/me/x", "/mnt/c/Users/me/x"),
    ],
)
def test_path_mapping_round_trips(win, posix):
    assert cs.win_to_wsl_path(win) == posix
    assert cs.wsl_to_win_path(posix, "Ubuntu").lower() == win.replace("/", "\\").lower()


@pytest.mark.parametrize("bad", [r"\\server\share\x", "/already/posix", "relative\\path", ""])
def test_win_to_wsl_rejects_non_drive_paths(bad):
    assert cs.win_to_wsl_path(bad) is None


def test_wsl_native_path_maps_to_unc():
    assert cs.wsl_to_win_path("/home/me/x", "Ubuntu") == r"\\wsl.localhost\Ubuntu\home\me\x"


# --------------------------------------------------------------------------
# F5 / F6 - connector uuids are org-scoped
# --------------------------------------------------------------------------


def _ported(world, **session_kw):
    parts = cs.load_partitions()
    src = next(p for p in parts if p.org == world.ORG_A)
    dst = next(p for p in parts if p.org == world.ORG_B)
    sess = next(s for s in src.sessions if s.uuid.startswith(session_kw.pop("uuid_prefix")))
    data, notes = cs.port_session(sess, dst, False, cs.connector_names(parts))
    return data, notes, dst


def test_connector_uuids_remapped_by_name(two_orgs):
    w = two_orgs
    data, notes, dst = _ported(w, uuid_prefix="1111aaaa")

    got = {s["name"]: s["uuid"] for s in data["remoteMcpServersConfig"]}
    assert got == {"Linear": w.LINEAR_B, "Sentry": w.SENTRY_B}
    assert any("remapped" in n for n in notes)


def test_no_dangling_tool_keys_after_port(two_orgs):
    w = two_orgs
    data, _, dst = _ported(w, uuid_prefix="1111aaaa")

    live = set(dst.connectors.values())
    for key in data["enabledMcpTools"]:
        uuid = key.partition(":")[0]
        assert uuid in live, f"tool key points at a connector absent from the destination: {key}"


def test_dropped_connector_drops_its_tool_keys(world):
    """A connector the destination org does not have takes its tool keys with it."""
    w = world
    a, b = w.partition("acct", "org-a"), w.partition("acct", "org-b")
    w.sign_in("acct", "org-b")
    GONE = "cccc3333-0000-0000-0000-000000000005"
    KEPT_A, KEPT_B = "aaaa1111-0000-0000-0000-000000000001", "bbbb2222-0000-0000-0000-000000000002"
    w.add_session(
        a, "1111aaaa-0000-4000-8000-000000000001", "cli-a",
        connectors=[connector("Linear", KEPT_A, ["issue"]), connector("Obscure", GONE, ["thing"])],
        enabled_tools={f"{KEPT_A}:issue": True, f"{GONE}:thing": True},
    )
    w.add_transcript(r"C:\repo", "cli-a")
    w.add_session(
        b, "2222bbbb-0000-4000-8000-000000000002", "cli-b",
        connectors=[connector("Linear", KEPT_B, ["issue"])],
        enabled_tools={f"{KEPT_B}:issue": True},
    )
    w.add_transcript(r"C:\repo", "cli-b")

    parts = cs.load_partitions()
    src = next(p for p in parts if p.org == "org-a")
    dst = next(p for p in parts if p.org == "org-b")
    data, notes = cs.port_session(src.sessions[0], dst, False, cs.connector_names(parts))

    assert [s["name"] for s in data["remoteMcpServersConfig"]] == ["Linear"]
    assert set(data["enabledMcpTools"]) == {f"{KEPT_B}:issue"}
    assert any("Obscure" in n for n in notes), "a dropped connector must be reported"


def test_stale_tool_key_repaired_from_global_map(two_orgs):
    """
    F6: the app does not prune enabledMcpTools, so a session in org B can carry
    keys naming org A's uuids. The session's own connector list cannot name
    them - repair must use every uuid known on the machine.
    """
    w = two_orgs
    donor = w.b / "local_2222bbbb-0000-4000-8000-000000000002.json"
    data = json.loads(donor.read_text(encoding="utf-8"))
    data["enabledMcpTools"][f"{w.LINEAR_A}:issue"] = True  # org A's uuid, stranded in org B
    donor.write_text(json.dumps(data), encoding="utf-8")

    parts = cs.load_partitions()
    dst = next(p for p in parts if p.org == w.ORG_B)
    sess = next(s for s in dst.sessions if s.uuid.startswith("2222bbbb"))
    out = json.loads(json.dumps(sess.data))
    notes = []
    cs.normalize_enabled_tools(out, dst.connectors, cs.connector_names(parts), notes)

    assert set(out["enabledMcpTools"]) == {f"{w.LINEAR_B}:issue", f"{w.SENTRY_B}:errors"}
    assert any("remapped Linear" in n for n in notes)


def test_one_stray_import_cannot_redefine_an_org(two_orgs):
    """
    F5, the failure mode that motivated deciding the map by majority.

    A session copied in from another org - without the ledger recording it,
    which happens with a hand copy or a copy predating the ledger - carries the
    source org's uuids. If the map went by recency, being the newest session
    would be enough to redefine the destination org, and every later copy would
    be remapped at connectors that do not exist there.
    """
    w = two_orgs
    # org B as it really looks: several native sessions agreeing with each other
    for n in range(2):
        w.add_session(
            w.b, f"bbbb000{n}-0000-4000-8000-00000000000{n}", f"cli-b-{n + 2}",
            title=f"native {n}",
            connectors=[connector("Linear", w.LINEAR_B, ["issue"]),
                        connector("Sentry", w.SENTRY_B, ["errors"])],
        )
        w.add_transcript(r"C:\repo", f"cli-b-{n + 2}")
    # one foreign session carrying org A's uuids, and the most recent of all
    w.add_session(
        w.b, "9999cccc-0000-4000-8000-000000000009", "cli-stray",
        title="copied in by hand",
        lastActivityAt=2_000_000_000_000,  # newer than every native session
        connectors=[connector("Linear", w.LINEAR_A, ["issue"]),
                    connector("Sentry", w.SENTRY_A, ["errors"])],
        enabled_tools={f"{w.LINEAR_A}:issue": True},
    )
    w.add_transcript(r"C:\repo", "cli-stray")

    dst = next(p for p in cs.load_partitions() if p.org == w.ORG_B)

    assert dst.connectors["Linear"] == w.LINEAR_B, "3 native sessions must outvote 1 import"
    assert dst.connectors["Sentry"] == w.SENTRY_B
    assert "Linear" in dst.contested_connectors, "the disagreement must stay visible"


def test_a_tie_is_broken_by_recency_and_reported(two_orgs):
    """
    Majority cannot resolve 1-against-1. Recency breaks it, which is arbitrary -
    so the tie must at least be reported rather than silently decided.
    """
    w = two_orgs  # org B has exactly one native session
    w.add_session(
        w.b, "9999cccc-0000-4000-8000-000000000009", "cli-stray",
        title="copied in by hand", lastActivityAt=2_000_000_000_000,
        connectors=[connector("Linear", w.LINEAR_A, ["issue"])],
    )
    w.add_transcript(r"C:\repo", "cli-stray")

    dst = next(p for p in cs.load_partitions() if p.org == w.ORG_B)

    assert dst.contested_connectors["Linear"] == {w.LINEAR_B: 1, w.LINEAR_A: 1}
    assert dst.connectors["Linear"] == w.LINEAR_A, "recency breaks the tie"


def test_contested_connectors_are_reported_in_the_plan(two_orgs, capsys):
    w = two_orgs
    w.add_session(
        w.b, "9999cccc-0000-4000-8000-000000000009", "cli-stray",
        title="copied in by hand", lastActivityAt=2_000_000_000_000,
        connectors=[connector("Linear", w.LINEAR_A, ["issue"])],
    )
    w.add_transcript(r"C:\repo", "cli-stray")

    w.run("copy", "--from", w.ORG_A[:4])

    out = capsys.readouterr().out
    assert "disagrees with itself about Linear" in out, "a silent majority vote is a surprise"


def test_unknown_uuid_is_dropped_not_carried(two_orgs):
    w = two_orgs
    parts = cs.load_partitions()
    dst = next(p for p in parts if p.org == w.ORG_B)
    data = {"enabledMcpTools": {"deadbeef-0000-0000-0000-000000000000:tool": True}}
    notes = []
    cs.normalize_enabled_tools(data, dst.connectors, cs.connector_names(parts), notes)

    assert data["enabledMcpTools"] == {}
    assert any("dropped" in n for n in notes)


def test_port_strips_crash_and_process_state(two_orgs):
    w = two_orgs
    src_file = w.a / "local_1111aaaa-0000-4000-8000-000000000001.json"
    data = json.loads(src_file.read_text(encoding="utf-8"))
    data.update({"error": "boom", "errorAt": 1, "sshRemoteProcessId": "pid-123"})
    src_file.write_text(json.dumps(data), encoding="utf-8")

    ported, notes, _ = _ported(w, uuid_prefix="1111aaaa")

    assert "error" not in ported and "errorAt" not in ported
    assert "sshRemoteProcessId" not in ported, "a dead process id must not travel"


# --------------------------------------------------------------------------
# F7 - entrypoint distinguishes CLI from app
# --------------------------------------------------------------------------


def test_origin_from_entrypoint(world):
    w = world
    cli = cs.read_cli_session(
        w.wsl, w.add_wsl_transcript("/home/me/a", "c1", records=[record("/home/me/a", entrypoint="cli")])
    )
    app = cs.read_cli_session(
        w.wsl,
        w.add_wsl_transcript("/home/me/b", "c2", records=[record("/home/me/b", entrypoint="claude-desktop")]),
    )
    both = cs.read_cli_session(
        w.wsl,
        w.add_wsl_transcript(
            "/home/me/c", "c3",
            records=[record("/home/me/c", entrypoint="cli"),
                     record("/home/me/c", entrypoint="claude-desktop", ts="2026-08-15T11:00:00.000Z")],
        ),
    )

    assert (cli.origin, cli.born_in_cli) == ("cli", True)
    assert (app.origin, app.born_in_cli) == ("desktop", False)
    assert both.origin == "cli>desktop", "a CLI session later opened in the app"
    assert both.born_in_cli


def test_only_cli_sessions_are_adoptable(two_orgs, capsys):
    w = two_orgs
    w.add_wsl_transcript("/home/me/a", "cli-born", records=[record("/home/me/a", entrypoint="cli")], title="mine")
    w.add_wsl_transcript(
        "/home/me/b", "app-born",
        records=[record("/home/me/b", entrypoint="claude-desktop")], title="the app's",
    )

    w.run("sessions", "-H", "wsl:Testbuntu", "--cli")
    out = capsys.readouterr().out
    assert "cli-born" in out and "app-born" not in out

    w.run("sessions", "-H", "wsl:Testbuntu", "--desktop")
    out = capsys.readouterr().out
    assert "app-born" in out and "cli-born" not in out


# --------------------------------------------------------------------------
# F8 / F9 - WSL sessions are not ssh sessions
# --------------------------------------------------------------------------


def test_wsl_session_is_not_remote(world):
    w = world
    wsl = cs.Session(uuid="u", path=w.tmp / "x", data={
        "wslConfig": {"distro": "Ubuntu"},
        "sshRemoteTranscriptPath": "/home/me/.claude/projects/-home-me-a/x.jsonl",
    })
    ssh = cs.Session(uuid="u", path=w.tmp / "x", data={
        "sshRemoteTranscriptPath": "/home/other/.claude/projects/-x/y.jsonl",
    })

    assert wsl.is_wsl and not wsl.is_remote, "WSL reuses the ssh fields but is not remote"
    assert ssh.is_remote and not ssh.is_wsl


def test_wsl_transcript_resolves_without_mirror(two_orgs, monkeypatch):
    """
    F9: before the app mirrors it to projects/ssh-<id>/, an adopted session's
    transcript must still resolve - otherwise S6 would refuse to copy it.
    """
    w = two_orgs
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="t")
    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")

    # the tool resolves through the UNC spelling; point that at the fake distro
    monkeypatch.setattr(
        cs, "wsl_to_win_path", lambda posix, distro: str(w.wsl_root / posix.lstrip("/"))
    )
    parts = cs.load_partitions()
    dst = next(p for p in parts if p.org == w.ORG_B)
    adopted = next(s for s in dst.sessions if s.is_wsl)

    assert not list(w.projects.glob("ssh-*")), "no mirror exists yet"
    assert adopted.transcript is not None, "must resolve via sshRemoteTranscriptPath"
    assert adopted.transcript.exists()
