"""
copy --refresh: bringing an earlier copy up to date, and only as a fast-forward.

A copy made earlier goes stale when work continues in the source org. The
transcript is shared, so the conversation is already current; what is stale is
the desktop metadata (turn count, last message, wrap-up, PR links). Refreshing
overwrites that metadata, which is the one place this tool ever replaces an
existing session file - so every precondition is tested by asserting the file
is left byte-for-byte alone when it does not hold.
"""

from __future__ import annotations

import json

import pytest

from conftest import cs, connector, record

UUID = "3333cccc-0000-4000-8000-000000000003"
CLI = "cli-shared-3"


def chain(*links: tuple[str, str | None], logical: dict[str, str] | None = None) -> list[dict]:
    """Transcript records with uuid/parentUuid, the way Claude Code links turns."""
    out = []
    for uid, parent in links:
        r = record(r"C:\repo")
        r["uuid"], r["parentUuid"] = uid, parent
        if logical and uid in logical:
            r["logicalParentUuid"] = logical[uid]
        out.append(r)
    return out


@pytest.fixture
def stale(two_orgs):
    """
    A session copied from B into A, then continued in A for two more turns.
    B still holds the copy as it was at m1.
    """
    w = two_orgs
    w.add_transcript(r"C:\repo", CLI, records=chain(("m1", None), ("m2", "m1"), ("m3", "m2")))
    w.src = w.add_session(
        w.a, UUID, CLI, title="continued in A",
        connectors=[connector("Linear", w.LINEAR_A, ["issue"])],
        enabled_tools={f"{w.LINEAR_A}:issue": True},
        lastAssistantUuid="m3", completedTurns=3, lastTurnReport="new report",
        lastFocusedAt=1_700_000_100_000,
    )
    w.dst = w.add_session(
        w.b, UUID, CLI, title="continued in A",
        connectors=[connector("Linear", w.LINEAR_B, ["issue"])],
        enabled_tools={f"{w.LINEAR_B}:issue": True},
        lastAssistantUuid="m1", completedTurns=1, lastTurnReport="old report",
        lastFocusedAt=1_700_000_900_000,
    )
    w.ledger = w.tmp / "session-copy-ledger.json"
    w.ledger.write_text(json.dumps({"copies": [
        {"uuid": UUID, "src": w.ORG_B, "dest": w.ORG_A, "at": 1}
    ]}), encoding="utf-8")
    return w


def refresh(w, *extra: str) -> int:
    return w.run("copy", "--from", w.ORG_A[:4], "-s", UUID[:8], "--refresh", *extra)


def backups(w) -> list:
    d = w.tmp / "session-copy-backups"
    return sorted(d.rglob("*.json")) if d.exists() else []


def test_dry_run_refresh_writes_nothing(stale, capsys):
    before = stale.snapshot(stale.store)
    ledger = stale.ledger.read_bytes()
    refresh(stale)
    assert stale.snapshot(stale.store) == before
    assert stale.ledger.read_bytes() == ledger
    assert backups(stale) == []
    assert "REFRESH (turns 1->3)" in capsys.readouterr().out


def test_without_flag_existing_copy_is_untouched_and_hinted(stale, capsys):
    before = stale.dst.read_bytes()
    stale.run("copy", "--from", stale.ORG_A[:4], "-s", UUID[:8], "--apply")
    assert stale.dst.read_bytes() == before
    assert "--refresh" in capsys.readouterr().out


def test_refresh_fast_forwards_and_ports(stale):
    refresh(stale, "--apply")
    data = json.loads(stale.dst.read_text(encoding="utf-8"))
    assert data["lastAssistantUuid"] == "m3"
    assert data["completedTurns"] == 3
    assert data["lastTurnReport"] == "new report"
    # connectors point at the destination org, exactly as a fresh copy would
    assert [s["uuid"] for s in data["remoteMcpServersConfig"]] == [stale.LINEAR_B]
    assert data["enabledMcpTools"] == {f"{stale.LINEAR_B}:issue": True}
    # the copy's own, later focus time survives
    assert data["lastFocusedAt"] == 1_700_000_900_000


def test_refresh_keeps_the_previous_version(stale):
    old = stale.dst.read_bytes()
    refresh(stale, "--apply")
    saved = backups(stale)
    assert len(saved) == 1 and saved[0].read_bytes() == old


def test_refresh_keeps_archive_state_of_the_copy(stale):
    data = json.loads(stale.dst.read_text(encoding="utf-8"))
    data["isArchived"] = True
    stale.dst.write_text(json.dumps(data), encoding="utf-8")
    refresh(stale, "--apply")
    assert json.loads(stale.dst.read_text(encoding="utf-8"))["isArchived"] is True


def test_refresh_is_recorded_in_the_ledger(stale):
    refresh(stale, "--apply")
    entries = json.loads(stale.ledger.read_text(encoding="utf-8"))["copies"]
    assert entries[-1]["uuid"] == UUID and entries[-1]["refresh"] is True
    assert entries[-1]["dest"] == stale.ORG_B


def test_second_refresh_is_a_noop(stale, capsys):
    refresh(stale, "--apply")
    after = stale.dst.read_bytes()
    capsys.readouterr()
    refresh(stale, "--apply")
    assert stale.dst.read_bytes() == after
    assert "up to date" in capsys.readouterr().out


def _set_dst(w, **fields):
    data = json.loads(w.dst.read_text(encoding="utf-8"))
    data.update(fields)
    w.dst.write_text(json.dumps(data), encoding="utf-8")
    return w.dst.read_bytes()


def test_diverged_copy_is_left_alone(stale, capsys):
    # the copy continued on its own from m1: m1 -> m4, not on A's line
    stale.add_transcript(r"C:\repo", CLI, records=chain(
        ("m1", None), ("m2", "m1"), ("m3", "m2"), ("m4", "m1")))
    before = _set_dst(stale, lastAssistantUuid="m4")
    refresh(stale, "--apply")
    assert stale.dst.read_bytes() == before
    assert "diverged" in capsys.readouterr().out
    assert backups(stale) == []


def test_copy_ahead_of_source_is_left_alone(stale, capsys):
    stale.add_transcript(r"C:\repo", CLI, records=chain(
        ("m1", None), ("m2", "m1"), ("m3", "m2"), ("m5", "m3")))
    before = _set_dst(stale, lastAssistantUuid="m5")
    refresh(stale, "--apply")
    assert stale.dst.read_bytes() == before
    assert "newer here" in capsys.readouterr().out


def test_session_the_tool_never_copied_is_left_alone(stale, capsys):
    stale.ledger.write_text(json.dumps({"copies": []}), encoding="utf-8")
    before = stale.dst.read_bytes()
    refresh(stale, "--apply")
    assert stale.dst.read_bytes() == before
    assert "not a tool copy" in capsys.readouterr().out


def test_ledger_link_in_either_direction_counts(stale):
    stale.ledger.write_text(json.dumps({"copies": [
        {"uuid": UUID, "src": stale.ORG_A, "dest": stale.ORG_B, "at": 1}
    ]}), encoding="utf-8")
    refresh(stale, "--apply")
    assert json.loads(stale.dst.read_text(encoding="utf-8"))["lastAssistantUuid"] == "m3"


def test_unmarked_copy_cannot_be_compared(stale, capsys):
    data = json.loads(stale.dst.read_text(encoding="utf-8"))
    del data["lastAssistantUuid"]
    stale.dst.write_text(json.dumps(data), encoding="utf-8")
    before = stale.dst.read_bytes()
    refresh(stale, "--apply")
    assert stale.dst.read_bytes() == before
    assert "can't compare" in capsys.readouterr().out


def test_compaction_boundary_is_followed(stale):
    # after compaction the new chain starts with parentUuid null and points back
    # through logicalParentUuid; the copy at m1 is still an ancestor of m3
    stale.add_transcript(r"C:\repo", CLI, records=chain(
        ("m1", None), ("c1", None), ("m3", "c1"), logical={"c1": "m1"}))
    refresh(stale, "--apply")
    assert json.loads(stale.dst.read_text(encoding="utf-8"))["lastAssistantUuid"] == "m3"


def test_copy_since_leaves_out_older_sessions(two_orgs, monkeypatch, capsys):
    w = two_orgs
    now = 1_700_000_000_000 + 10 * 3_600_000
    monkeypatch.setattr(cs.time, "time", lambda: now / 1000)
    w.add_session(w.a, "4444dddd-0000-4000-8000-000000000004", "cli-a-4",
                  title="recent", lastActivityAt=now - 3_600_000)
    w.add_transcript(r"C:\repo", "cli-a-4")
    w.run("copy", "--from", w.ORG_A[:4], "--since", "4h", "--apply")
    assert (w.b / "local_4444dddd-0000-4000-8000-000000000004.json").exists()
    # "work in A" was last active ten hours ago
    assert not (w.b / "local_1111aaaa-0000-4000-8000-000000000001.json").exists()
    assert "1 older left out" in capsys.readouterr().out


def test_replace_refuses_a_file_that_changed(tmp_path):
    target = tmp_path / "local_x.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    with pytest.raises(FileExistsError):
        cs.replace_session_file(target, b'{"a": 0}', {"a": 2}, tmp_path / "bk")
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert not (tmp_path / "bk").exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_refresh_writes_are_exclusive_create(stale, monkeypatch):
    """Every file refresh opens for writing is new; the swap is an os.replace."""
    modes = []
    real_open = open

    def spy(path, mode="r", *a, **k):
        if any(c in mode for c in "wxa") and "session-copy-ledger" not in str(path):
            modes.append(mode)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    refresh(stale, "--apply")
    assert modes and all("x" in m for m in modes), modes
