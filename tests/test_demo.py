"""docs/demo.gif and the README's example output come from docs/demo_fixture.py.

These tests replay the recorded commands against a fresh demo store, so the
demo cannot claim output the tool no longer prints.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def demo(tmp_path: Path):
    home = tmp_path / "home"
    subprocess.run(
        [sys.executable, str(ROOT / "docs" / "demo_fixture.py"), str(home)],
        check=True,
        capture_output=True,
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "PYTHONIOENCODING": "utf-8",
    }
    env.pop("CLAUDE_SESSIONS_ROOT", None)

    def run(*args: str) -> str:
        result = subprocess.run(
            [sys.executable, str(ROOT / "claude_sessions.py"), *args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout

    return run


def test_demo_shows_two_orgs_one_out_of_quota(demo) -> None:
    out = demo("partitions")
    assert "  work      3c426532" in out and "2%" in out
    assert "● personal  762f7f2a" in out and "88%" in out


def test_demo_dry_run_plans_two_copies_and_the_connector_fixes(demo) -> None:
    out = demo("copy", "--from", "work")
    assert "2 COPY   1 skip: archived" in out
    assert "remapped Linear: 01812872 ▶ 4b57c823" in out
    assert "dropped Datadog (9d1a0b7e) - not present in personal" in out
    assert "DRY RUN. Nothing written." in out


def test_demo_apply_brings_both_sessions_into_the_signed_in_org(demo) -> None:
    assert "Copied 2 session(s) into personal" in demo("copy", "--from", "work", "--apply")
    out = demo("sessions", "-p", "personal")
    for title in ("Fix flaky auth test", "Blog post draft", "Migrate billing webhooks"):
        assert title in out
    assert "(3 unarchived of 3)" in out


def test_demo_fixture_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    """Control: the fixture can never write into an existing Claude home."""
    (tmp_path / "existing.txt").write_text("keep me")
    result = subprocess.run(
        [sys.executable, str(ROOT / "docs" / "demo_fixture.py"), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "refusing" in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.txt"]
