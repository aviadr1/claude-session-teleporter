"""
Safety invariants - see INVARIANTS.md, section "Safety".

These assert the ABSENCE of an effect. A test here failing means someone can
lose work, so they are deliberately blunt: snapshot the bytes, do the thing,
compare the bytes.
"""

import json

import pytest
from conftest import connector, cs, record


# --------------------------------------------------------------------------
# S1 - nothing is written without --apply
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["copy", "adopt", "eject"])
def test_dry_run_writes_nothing(two_orgs, cmd):
    w = two_orgs
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="in the distro")

    before = w.snapshot(w.store, w.projects, w.wsl_root)
    ledger_before = (w.tmp / "session-copy-ledger.json").exists()

    if cmd == "copy":
        w.run("copy", "--from", w.ORG_A[:4])
    elif cmd == "adopt":
        w.run("adopt", "--from", "wsl:Testbuntu")
    else:
        w.run("eject", "1111aaaa", "-p", w.ORG_A[:4], "--to", "wsl:Testbuntu")

    assert w.snapshot(w.store, w.projects, w.wsl_root) == before
    assert (w.tmp / "session-copy-ledger.json").exists() == ledger_before


# --------------------------------------------------------------------------
# S2 - never overwrite
# --------------------------------------------------------------------------


def test_never_overwrites_existing_session(two_orgs, capsys):
    w = two_orgs
    uuid = "1111aaaa-0000-4000-8000-000000000001"
    # the same session id already exists in the destination, with other content
    target = w.b / f"local_{uuid}.json"
    target.write_text('{"sessionId":"local_' + uuid + '","title":"DO NOT CLOBBER"}', encoding="utf-8")
    before = target.read_bytes()

    w.run("copy", "--from", w.ORG_A[:4], "--apply")

    assert target.read_bytes() == before
    assert "already there" in capsys.readouterr().out


def test_exclusive_create_is_used(two_orgs, monkeypatch):
    """The guarantee is structural: every metadata write opens with mode 'x'."""
    w = two_orgs
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="t")
    modes = []
    real_open = open

    def spy(path, mode="r", *a, **k):
        if str(path).endswith(".json") and any(c in mode for c in "wxa"):
            modes.append(mode)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    w.run("copy", "--from", w.ORG_A[:4], "--apply")
    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")

    assert modes, "expected at least one metadata write"
    assert all("x" in m for m in modes), f"non-exclusive metadata write: {modes}"


# --------------------------------------------------------------------------
# S3 - never resurrect a deleted session
# --------------------------------------------------------------------------


def test_tombstone_blocks_copy(two_orgs, capsys):
    w = two_orgs
    uuid = "1111aaaa-0000-4000-8000-000000000001"
    w.add_tombstone(w.b, uuid)

    w.run("copy", "--from", w.ORG_A[:4], "--apply")

    assert not (w.b / f"local_{uuid}.json").exists()
    assert "deleted there" in capsys.readouterr().out


def test_tombstone_blocks_adopt(two_orgs, capsys):
    w = two_orgs
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="t")
    cli = cs.CliSession(host=w.wsl, transcript=w.wsl.projects, cli_id="wsl-1")
    w.add_tombstone(w.b, cli.uuid)

    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")

    assert not (w.b / f"local_{cli.uuid}.json").exists()
    assert "deleted there" in capsys.readouterr().out


# --------------------------------------------------------------------------
# S4 / S5 / S8 - the source is never modified
# --------------------------------------------------------------------------


def test_source_partition_untouched(two_orgs):
    w = two_orgs
    before = w.snapshot(w.a)
    w.run("copy", "--from", w.ORG_A[:4], "--apply")
    assert w.snapshot(w.a) == before


def test_copy_does_not_touch_transcripts(two_orgs):
    w = two_orgs
    before = w.snapshot(w.projects)
    w.run("copy", "--from", w.ORG_A[:4], "--apply")
    assert w.snapshot(w.projects) == before, "copy must never duplicate a conversation"


def test_adopt_never_writes_into_wsl(two_orgs):
    w = two_orgs
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="t")
    before = w.snapshot(w.wsl_root)
    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")
    assert w.snapshot(w.wsl_root) == before, "adopt is metadata-only on the windows side"


# --------------------------------------------------------------------------
# S6 - a session with no transcript cannot be copied
# --------------------------------------------------------------------------


def test_copy_skips_session_without_transcript(two_orgs, capsys):
    w = two_orgs
    uuid = "3333cccc-0000-4000-8000-000000000003"
    w.add_session(w.a, uuid, "cli-missing", title="orphan")  # no transcript written

    w.run("copy", "--from", w.ORG_A[:4], "--apply")

    assert not (w.b / f"local_{uuid}.json").exists()
    assert "no transcript" in capsys.readouterr().out


# --------------------------------------------------------------------------
# S7 - cross-account needs an explicit flag
# --------------------------------------------------------------------------


def test_cross_account_refused_by_default(world, capsys):
    w = world
    other = w.partition("acct-two", "org-x")
    mine = w.partition("acct-one", "org-y")
    w.sign_in("acct-one", "org-y")
    w.add_session(other, "4444dddd-0000-4000-8000-000000000004", "cli-x", title="theirs")
    w.add_transcript(r"C:\repo", "cli-x")

    with pytest.raises(SystemExit) as e:
        w.run("copy", "--from", "org-x", "--apply")

    assert e.value.code == 2
    assert "different accounts" in capsys.readouterr().err
    assert not list(mine.glob("local_*.json"))


def test_cross_account_allowed_with_flag(world):
    w = world
    other = w.partition("acct-two", "org-x")
    mine = w.partition("acct-one", "org-y")
    w.sign_in("acct-one", "org-y")
    w.add_session(other, "4444dddd-0000-4000-8000-000000000004", "cli-x", title="theirs")
    w.add_transcript(r"C:\repo", "cli-x")

    w.run("copy", "--from", "org-x", "--allow-cross-account", "--apply")

    assert (mine / "local_4444dddd-0000-4000-8000-000000000004.json").exists()


# --------------------------------------------------------------------------
# S9 - adopt is idempotent
# --------------------------------------------------------------------------


def test_adopt_uuid_is_deterministic(world):
    w = world
    a = cs.CliSession(host=w.wsl, transcript=w.wsl_root, cli_id="same-id")
    b = cs.CliSession(host=w.wsl, transcript=w.wsl_root, cli_id="same-id")
    other = cs.CliSession(host=w.wsl, transcript=w.wsl_root, cli_id="different-id")

    assert a.uuid == b.uuid
    assert a.uuid != other.uuid


def test_adopt_twice_is_a_noop(two_orgs, capsys):
    w = two_orgs
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="only once")

    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")
    after_first = w.snapshot(w.b)
    capsys.readouterr()

    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")

    assert w.snapshot(w.b) == after_first
    assert "already adopted" in capsys.readouterr().out
    assert len(list(w.b.glob("local_*.json"))) == 2  # the native one, plus one adopted


# --------------------------------------------------------------------------
# S10 - an adopted session never inherits a permission bypass
# --------------------------------------------------------------------------


def test_adopt_resets_permission_mode(two_orgs):
    w = two_orgs
    # the donor this will clone is set to bypass
    donor = w.b / "local_2222bbbb-0000-4000-8000-000000000002.json"
    data = json.loads(donor.read_text(encoding="utf-8"))
    data["permissionMode"] = "bypassPermissions"
    donor.write_text(json.dumps(data), encoding="utf-8")
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="t")

    w.run("adopt", "--from", "wsl:Testbuntu", "--apply")

    adopted = [
        json.loads(f.read_text(encoding="utf-8"))
        for f in w.b.glob("local_*.json")
        if json.loads(f.read_text(encoding="utf-8")).get("wslConfig")
    ]
    assert adopted, "expected an adopted session"
    assert all(s["permissionMode"] == "auto" for s in adopted)


def test_adopt_reports_the_permission_reset(two_orgs, capsys):
    w = two_orgs
    donor = w.b / "local_2222bbbb-0000-4000-8000-000000000002.json"
    data = json.loads(donor.read_text(encoding="utf-8"))
    data["permissionMode"] = "bypassPermissions"
    donor.write_text(json.dumps(data), encoding="utf-8")
    w.add_wsl_transcript("/home/me/repo", "wsl-1", title="t")

    w.run("adopt", "--from", "wsl:Testbuntu")

    assert "reset permissionMode" in capsys.readouterr().out, "a silent reset is a surprise"


# --------------------------------------------------------------------------
# S11 / S12 - eject is the one that forks
# --------------------------------------------------------------------------


def test_eject_writes_second_transcript(two_orgs, capsys):
    w = two_orgs
    src = w.projects / cs.encode_cwd(r"C:\repo") / "cli-a-1.jsonl"
    before = src.read_bytes()

    w.run("eject", "1111aaaa", "-p", w.ORG_A[:4], "--to", "wsl:Testbuntu", "--apply")

    target = w.wsl.projects / cs.encode_cwd("/mnt/c/repo") / "cli-a-1.jsonl"
    assert target.exists(), "eject must place a transcript where the WSL CLI looks"
    assert src.read_bytes() == before, "the windows session keeps its own transcript"
    out = capsys.readouterr().out
    assert "claude --resume cli-a-1" in out
    assert "do not run both at once" in out, "the fork must be stated"


def test_eject_rewrites_cwd_only(two_orgs):
    w = two_orgs
    w.run("eject", "1111aaaa", "-p", w.ORG_A[:4], "--to", "wsl:Testbuntu", "--apply")

    target = w.wsl.projects / cs.encode_cwd("/mnt/c/repo") / "cli-a-1.jsonl"
    recs = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert all(r["cwd"] == "/mnt/c/repo" for r in recs if "cwd" in r)
    # everything else survives untouched
    assert all(r.get("version") == "2.1.233" for r in recs if "version" in r)


def test_eject_refuses_existing_target(two_orgs, capsys):
    w = two_orgs
    d = w.wsl.projects / cs.encode_cwd("/mnt/c/repo")
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-a-1.jsonl").write_text("PRE-EXISTING", encoding="utf-8")

    w.run("eject", "1111aaaa", "-p", w.ORG_A[:4], "--to", "wsl:Testbuntu", "--apply")

    assert (d / "cli-a-1.jsonl").read_text(encoding="utf-8") == "PRE-EXISTING"
    assert "already" in capsys.readouterr().out


def test_eject_refuses_unmappable_cwd(two_orgs, capsys):
    w = two_orgs
    uuid = "5555eeee-0000-4000-8000-000000000005"
    unc = r"\\server\share\repo"
    w.add_session(w.a, uuid, "cli-unc", cwd=unc, title="on a share")
    w.add_transcript(unc, "cli-unc")

    with pytest.raises(SystemExit) as e:
        w.run("eject", "5555eeee", "-p", w.ORG_A[:4], "--to", "wsl:Testbuntu", "--apply")

    assert e.value.code == 2
    assert "cannot express" in capsys.readouterr().err


def test_eject_of_a_wsl_session_is_a_noop(two_orgs, capsys):
    """It is already in the distro; there is nothing to fork."""
    w = two_orgs
    uuid = "6666ffff-0000-4000-8000-000000000006"
    w.add_session(
        w.a, uuid, "wsl-native", cwd="/home/me/repo", title="already there",
        wslConfig={"distro": "Testbuntu"},
        sshRemoteTranscriptPath="/home/me/.claude/projects/-home-me-repo/wsl-native.jsonl",
    )
    before = w.snapshot(w.wsl_root)

    w.run("eject", "6666ffff", "-p", w.ORG_A[:4], "--to", "wsl:Testbuntu", "--apply")

    assert w.snapshot(w.wsl_root) == before
    assert "never left the distro" in capsys.readouterr().out


# --------------------------------------------------------------------------
# S13 - selectors refuse to guess
# --------------------------------------------------------------------------


def test_ambiguous_selectors_die(two_orgs, capsys):
    w = two_orgs
    w.add_session(w.a, "7777aaaa-0000-4000-8000-000000000007", "cli-dup1", title="duplicate title")
    w.add_session(w.a, "8888aaaa-0000-4000-8000-000000000008", "cli-dup2", title="duplicate title")

    with pytest.raises(SystemExit) as e:
        w.run("copy", "--from", w.ORG_A[:4], "-s", "duplicate title")

    assert e.value.code == 2
    assert "ambiguous" in capsys.readouterr().err


def test_unknown_selectors_die(two_orgs, capsys):
    w = two_orgs
    for argv, needle in (
        (("sessions", "-p", "nosuchorg"), "no partition matches"),
        (("sessions", "-H", "wsl:Nope"), "no WSL distro matches"),
        (("copy", "--from", w.ORG_A[:4], "-s", "nosuchsession"), "no session in"),
    ):
        with pytest.raises(SystemExit) as e:
            w.run(*argv)
        assert e.value.code == 2
        assert needle in capsys.readouterr().err


def test_adopt_refuses_a_non_wsl_source(two_orgs, capsys):
    with pytest.raises(SystemExit) as e:
        two_orgs.run("adopt", "--from", "windows")
    assert e.value.code == 2
    assert "adopt imports from a WSL distro" in capsys.readouterr().err
