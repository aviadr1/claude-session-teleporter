"""
pack / unpack - see INVARIANTS.md, S16-S19.

A pack leaves this machine, so it is treated as untrusted on the way back in:
these tests build hostile packs as well as honest ones, and assert both what
lands and what never does.
"""

import hashlib
import json
import time
import zipfile

import pytest
from conftest import cs, record, transcript

UUID_A = "1111aaaa-0000-4000-8000-000000000001"


@pytest.fixture
def packed(two_orgs):
    """Org A's session, with the sidecar directory Claude Code keeps beside a transcript."""
    w = two_orgs
    side = w.projects / cs.encode_cwd(r"C:\repo") / "cli-a-1"
    (side / "subagents").mkdir(parents=True)
    (side / "subagents" / "agent-1.jsonl").write_text(
        transcript([record(r"C:\repo", human=False)]), encoding="utf-8"
    )
    (side / "tool-results").mkdir()
    (side / "tool-results" / "out.txt").write_text(r"listing of C:\repo", encoding="utf-8")
    w.pack = w.tmp / "p.zip"
    return w


def pack_it(w, *extra: str) -> zipfile.ZipFile:
    assert w.run("pack", "--from", w.ORG_A[:4], "-o", str(w.pack), "--apply", *extra) == 0
    return zipfile.ZipFile(w.pack)


def records(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def hostile_pack(path, mutate) -> None:
    """A structurally valid pack whose manifest `mutate` then corrupts."""
    data = transcript([record(r"C:\repo")]).encode()
    entry = {
        "uuid": "9999dddd-0000-4000-8000-000000000009",
        "cliSessionId": "cli-evil",
        "title": "evil",
        "projectDir": "C--repo",
        "cwd": r"C:\repo",
        "metadata": "sessions/local_9999dddd-0000-4000-8000-000000000009.json",
        "files": [{"path": "cli-evil.jsonl", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}],
    }
    members = {"projects/C--repo/cli-evil.jsonl": data, entry["metadata"]: b"{}"}
    mutate(entry, members)
    manifest = {"format": cs.PACK_FORMAT, "version": cs.PACK_VERSION, "sessions": [entry]}
    with zipfile.ZipFile(path, "w") as z:
        for name, blob in members.items():
            z.writestr(name, blob)
        z.writestr(cs.PACK_MANIFEST, json.dumps(manifest))


# --------------------------------------------------------------------------
# S1 / S4 - dry run by default, and the source is only ever read
# --------------------------------------------------------------------------


def test_pack_and_unpack_dry_runs_write_nothing(packed):
    w = packed
    before = w.snapshot(w.store, w.projects, w.wsl_root)

    w.run("pack", "--from", w.ORG_A[:4], "-o", str(w.pack))
    assert not w.pack.exists()

    pack_it(w)
    before = w.snapshot(w.store, w.projects, w.wsl_root)
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu")
    w.run("unpack", str(w.pack), "--to", w.ORG_B[:4])
    assert w.snapshot(w.store, w.projects, w.wsl_root) == before
    assert not (w.tmp / "session-copy-ledger.json").exists()


def test_pack_leaves_the_source_untouched(packed):
    w = packed
    before = w.snapshot(w.store, w.projects)
    pack_it(w)
    assert w.snapshot(w.store, w.projects) == before


def test_pack_never_overwrites_its_output(packed, capsys):
    w = packed
    w.pack.write_bytes(b"MINE")
    with pytest.raises(SystemExit):
        w.run("pack", "--from", w.ORG_A[:4], "-o", str(w.pack), "--apply")
    assert w.pack.read_bytes() == b"MINE"
    assert "already exists" in capsys.readouterr().err


# --------------------------------------------------------------------------
# what a pack holds
# --------------------------------------------------------------------------


def test_pack_holds_transcript_sidecars_metadata_and_checksums(packed):
    z = pack_it(packed)
    manifest = json.loads(z.read(cs.PACK_MANIFEST))
    (entry,) = manifest["sessions"]
    assert entry["uuid"] == UUID_A and entry["projectDir"] == cs.encode_cwd(r"C:\repo")
    assert {f["path"] for f in entry["files"]} == {
        "cli-a-1.jsonl", "cli-a-1/subagents/agent-1.jsonl", "cli-a-1/tool-results/out.txt",
    }
    for f in entry["files"]:
        blob = z.read(f"projects/{entry['projectDir']}/{f['path']}")
        assert hashlib.sha256(blob).hexdigest() == f["sha256"]
    assert json.loads(z.read(entry["metadata"]))["sessionId"] == f"local_{UUID_A}"
    # enough to remap connectors on a machine that has never seen org A
    assert manifest["connectors"][packed.LINEAR_A] == "Linear"


def test_since_and_archived_filters(packed):
    w = packed
    now = int(time.time() * 1000)
    w.add_session(w.a, "3333cccc-0000-4000-8000-000000000003", "cli-new", title="recent", lastActivityAt=now)
    w.add_transcript(r"C:\repo", "cli-new")
    w.add_session(w.a, "4444dddd-0000-4000-8000-000000000004", "cli-arch", title="archived",
                  lastActivityAt=now, isArchived=True)
    w.add_transcript(r"C:\repo", "cli-arch")

    z = pack_it(w, "--since", "4h")
    assert [e["cliSessionId"] for e in json.loads(z.read(cs.PACK_MANIFEST))["sessions"]] == ["cli-new"]

    w.pack = w.tmp / "q.zip"
    z = pack_it(w, "--since", "4h", "--include-archived")
    assert {e["cliSessionId"] for e in json.loads(z.read(cs.PACK_MANIFEST))["sessions"]} == {"cli-new", "cli-arch"}


def test_parse_since_rejects_garbage():
    assert cs.parse_since("4h") == 4 * 3600 * 1000
    with pytest.raises(SystemExit):
        cs.parse_since("4 hours")


# --------------------------------------------------------------------------
# unpack into the CLI inside WSL - the cross-host case
# --------------------------------------------------------------------------


def test_unpack_to_wsl_rewrites_cwd_and_places_where_the_cli_looks(packed, capsys):
    w = packed
    pack_it(w)
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu", "--apply")

    there = w.wsl.projects / cs.encode_cwd("/mnt/c/repo")
    recs = records(there / "cli-a-1.jsonl")
    assert recs and all(r["cwd"] == "/mnt/c/repo" for r in recs if "cwd" in r)
    assert all(r["version"] == "2.1.233" for r in recs if "version" in r), "only cwd is rewritten"
    assert records(there / "cli-a-1" / "subagents" / "agent-1.jsonl")[0]["cwd"] == "/mnt/c/repo"
    # plain files are history, never rewritten
    assert (there / "cli-a-1" / "tool-results" / "out.txt").read_text(encoding="utf-8") == r"listing of C:\repo"
    assert not (w.b / f"local_{UUID_A}.json").exists(), "a WSL destination gets no desktop metadata"
    assert "wsl -d Testbuntu --cd /mnt/c/repo -e claude --resume cli-a-1" in capsys.readouterr().out


def test_moved_session_keeps_each_records_own_cwd(packed):
    w = packed
    uuid = "5555eeee-0000-4000-8000-000000000005"
    w.add_session(w.a, uuid, "cli-moved", cwd=r"C:\repo\wt", title="moved")
    w.add_transcript(r"C:\repo\wt", "cli-moved", records=[record(r"C:\repo"), record(r"C:\repo\wt")])
    pack_it(w, "-s", "5555")
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu", "--apply")

    recs = records(w.wsl.projects / cs.encode_cwd("/mnt/c/repo/wt") / "cli-moved.jsonl")
    assert [r["cwd"] for r in recs if "cwd" in r] == ["/mnt/c/repo", "/mnt/c/repo/wt"]


def test_unpack_to_wsl_refuses_an_unreachable_cwd(packed, capsys):
    w = packed
    unc = r"\\server\share\repo"
    w.add_session(w.a, "6666ffff-0000-4000-8000-000000000006", "cli-unc", cwd=unc, title="share")
    w.add_transcript(unc, "cli-unc")
    pack_it(w, "-s", "6666")
    before = w.snapshot(w.wsl_root)
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu", "--apply")
    assert w.snapshot(w.wsl_root) == before
    assert "cwd unreachable" in capsys.readouterr().out


# --------------------------------------------------------------------------
# unpack on another machine - transcripts only, or into the desktop app
# --------------------------------------------------------------------------


def test_cli_unpack_elsewhere_is_byte_identical(packed, monkeypatch):
    w = packed
    pack_it(w)
    elsewhere = w.tmp / "other-machine" / ".claude" / "projects"
    monkeypatch.setattr(cs, "PROJECTS_DIR", elsewhere)
    monkeypatch.setattr(cs, "LOCAL_IS_WINDOWS", True)
    w.run("unpack", str(w.pack), "--cli", "--apply")

    src = w.projects / cs.encode_cwd(r"C:\repo")
    dst = elsewhere / cs.encode_cwd(r"C:\repo")
    for rel in ("cli-a-1.jsonl", "cli-a-1/subagents/agent-1.jsonl", "cli-a-1/tool-results/out.txt"):
        assert (dst / rel).read_bytes() == (src / rel).read_bytes()


def test_map_cwd_moves_the_session_to_another_layout(packed, monkeypatch):
    w = packed
    pack_it(w)
    elsewhere = w.tmp / "mac" / ".claude" / "projects"
    monkeypatch.setattr(cs, "PROJECTS_DIR", elsewhere)
    monkeypatch.setattr(cs, "LOCAL_IS_WINDOWS", False)
    w.run("unpack", str(w.pack), "--cli", "--map-cwd", r"c:\REPO=/Users/me/repo", "--apply")
    recs = records(elsewhere / cs.encode_cwd("/Users/me/repo") / "cli-a-1.jsonl")
    assert all(r["cwd"] == "/Users/me/repo" for r in recs if "cwd" in r)


def test_map_cwd_rules():
    wsl = cs.Host(kind="wsl", distro="D", posix_home="/home/me")
    maps = cs.parse_cwd_maps([r"C:\Users\me=D:\home", r"C:\Users\me\GitHub=/src"])
    assert cs.map_cwd(r"C:\Users\me\GitHub\repo", maps, wsl) == "/src/repo", "most specific wins"
    assert cs.map_cwd(r"C:\Users\me\notes", maps, wsl) == "/mnt/d/home/notes"
    assert cs.map_cwd(r"C:\Users\meg", maps, wsl) == "/mnt/c/Users/meg", "prefix ends at a separator"
    assert cs.map_cwd(r"\\server\share", [], wsl) is None


def test_unpack_into_desktop_ports_connectors_and_strips_grants(packed, monkeypatch):
    w = packed
    monkeypatch.setattr(cs, "LOCAL_IS_WINDOWS", True)  # the fixture's paths are Windows ones
    meta = w.a / f"local_{UUID_A}.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data.update(permissionMode="bypassPermissions", alwaysAllowedReasons=["rm -rf"],
                sessionPermissionUpdates=[{"allow": "Bash(*)"}])
    meta.write_text(json.dumps(data), encoding="utf-8")
    pack_it(w)
    transcripts = w.snapshot(w.projects)

    w.run("unpack", str(w.pack), "--to", w.ORG_B[:4], "--apply")

    got = json.loads((w.b / f"local_{UUID_A}.json").read_text(encoding="utf-8"))
    assert {s["uuid"] for s in got["remoteMcpServersConfig"]} == {w.LINEAR_B, w.SENTRY_B}
    assert all(k.split(":")[0] in (w.LINEAR_B, w.SENTRY_B) for k in got["enabledMcpTools"])
    assert got["permissionMode"] == "auto"
    assert got["alwaysAllowedReasons"] == [] and got["sessionPermissionUpdates"] == []
    assert w.snapshot(w.projects) == transcripts, "identical transcripts already here are reused"
    ledger = json.loads((w.tmp / "session-copy-ledger.json").read_text(encoding="utf-8"))
    assert ledger["copies"][0]["uuid"] == UUID_A and ledger["copies"][0]["dest"] == w.ORG_B


# --------------------------------------------------------------------------
# S2 / S3 - never overwrite, never resurrect, re-running is a no-op
# --------------------------------------------------------------------------


def test_unpack_twice_is_a_noop(packed, capsys):
    w = packed
    pack_it(w)
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu", "--apply")
    before = w.snapshot(w.wsl_root)
    capsys.readouterr()
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu", "--apply")
    assert w.snapshot(w.wsl_root) == before
    assert "already there" in capsys.readouterr().out


def test_unpack_never_overwrites_a_different_transcript(packed, capsys):
    w = packed
    pack_it(w)
    d = w.wsl.projects / cs.encode_cwd("/mnt/c/repo")
    d.mkdir(parents=True)
    (d / "cli-a-1.jsonl").write_text("ANOTHER VERSION", encoding="utf-8")
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu", "--apply")
    assert (d / "cli-a-1.jsonl").read_text(encoding="utf-8") == "ANOTHER VERSION"
    assert not (d / "cli-a-1").exists(), "nothing of a refused session lands"
    assert "different transcript there" in capsys.readouterr().out


def test_tombstone_blocks_unpack(packed, capsys):
    w = packed
    pack_it(w)
    w.add_tombstone(w.b, UUID_A)
    w.run("unpack", str(w.pack), "--to", w.ORG_B[:4], "--apply")
    assert not (w.b / f"local_{UUID_A}.json").exists()
    assert "deleted there" in capsys.readouterr().out


def test_unpack_uses_exclusive_create(packed, monkeypatch):
    w = packed
    pack_it(w)
    modes = []
    real_open = open

    def spy(file, mode="r", *a, **kw):
        if any(c in mode for c in "wa"):
            modes.append((str(file), mode))
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    w.run("unpack", str(w.pack), "--host", "wsl:Testbuntu", "--apply")
    assert not modes, f"unpack opened files for overwrite: {modes}"


# --------------------------------------------------------------------------
# S18 - a pack is untrusted input
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e, m: e["files"][0].update(path="../../escape.jsonl"),
        lambda e, m: e["files"].append({"path": "cli-evil/../../x", "sha256": "0" * 64}),
        lambda e, m: e.update(projectDir=".."),
        lambda e, m: e.update(cliSessionId="C:\\x"),
        lambda e, m: e.update(metadata="../local_x.json"),
        lambda e, m: e["files"][0].update(sha256="nope"),
        lambda e, m: m.pop("projects/C--repo/cli-evil.jsonl"),
    ],
    ids=["dotdot", "nested-dotdot", "project-dir", "cli-id", "metadata", "no-digest", "missing-member"],
)
def test_hostile_pack_is_refused_before_any_write(world, mutate):
    hostile_pack(world.tmp / "evil.zip", mutate)
    before = world.snapshot(world.tmp)
    with pytest.raises(SystemExit):
        world.run("unpack", str(world.tmp / "evil.zip"), "--host", "wsl:Testbuntu", "--apply")
    assert world.snapshot(world.tmp) == before


def test_tampered_file_is_not_unpacked(world, capsys):
    def tamper(e, m):
        m["projects/C--repo/cli-evil.jsonl"] = b'{"cwd":"C:\\\\repo","evil":true}\n'

    hostile_pack(world.tmp / "evil.zip", tamper)
    before = world.snapshot(world.wsl_root)
    world.run("unpack", str(world.tmp / "evil.zip"), "--host", "wsl:Testbuntu", "--apply")
    assert world.snapshot(world.wsl_root) == before
    assert "damaged in pack" in capsys.readouterr().out


def test_a_zip_without_a_manifest_is_not_a_pack(world, capsys):
    with zipfile.ZipFile(world.tmp / "plain.zip", "w") as z:
        z.writestr("projects/C--repo/x.jsonl", "{}")
    with pytest.raises(SystemExit):
        world.run("unpack", str(world.tmp / "plain.zip"), "--host", "wsl:Testbuntu")
    assert "no manifest.json" in capsys.readouterr().err


def test_hostile_pack_control_unpacks_when_untouched(world):
    """The refusals above are about the mutation, not about the pack builder."""
    hostile_pack(world.tmp / "fine.zip", lambda e, m: None)
    assert world.run("unpack", str(world.tmp / "fine.zip"), "--host", "wsl:Testbuntu", "--apply") == 0
    assert (world.wsl.projects / cs.encode_cwd("/mnt/c/repo") / "cli-evil.jsonl").exists()


def test_unpack_restores_files_of_a_session_the_app_still_lists(packed, monkeypatch):
    """The backup case: metadata survived, the transcript did not."""
    w = packed
    monkeypatch.setattr(cs, "LOCAL_IS_WINDOWS", True)
    pack_it(w)
    lost = w.projects / cs.encode_cwd(r"C:\repo") / "cli-a-1.jsonl"
    original = lost.read_bytes()
    lost.unlink()
    meta = w.a / f"local_{UUID_A}.json"
    meta_before = meta.read_bytes()

    w.run("unpack", str(w.pack), "--to", w.ORG_A[:4], "--apply")

    assert lost.read_bytes() == original
    assert meta.read_bytes() == meta_before, "metadata already there is left alone"


def test_to_is_refused_where_no_metadata_is_written(packed, capsys):
    w = packed
    pack_it(w)
    for extra in (["--host", "wsl:Testbuntu"], ["--cli"]):
        with pytest.raises(SystemExit):
            w.run("unpack", str(w.pack), "--to", w.ORG_B[:4], *extra)
    assert "transcripts only" in capsys.readouterr().err


def test_malformed_connectors_in_a_pack_do_not_crash(packed, monkeypatch):
    w = packed
    monkeypatch.setattr(cs, "LOCAL_IS_WINDOWS", True)
    meta = w.a / f"local_{UUID_A}.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data.update(remoteMcpServersConfig=["not-a-connector", 7], enabledMcpTools=["nope"])
    meta.write_text(json.dumps(data), encoding="utf-8")
    pack_it(w)
    assert w.run("unpack", str(w.pack), "--to", w.ORG_B[:4], "--apply") == 0
    got = json.loads((w.b / f"local_{UUID_A}.json").read_text(encoding="utf-8"))
    assert got.get("remoteMcpServersConfig", []) == [] and got.get("enabledMcpTools", {}) == {}
