"""
Early warning for format drift.

Every format claim in INVARIANTS.md was reverse-engineered from real files and
can be invalidated by any Claude Code release. `test_formats.py` pins those
claims against fixtures - which keeps them honest about our intent, but cannot
notice when reality moves.

This file re-checks them against whatever real store is on this machine, and
skips cleanly when there is none, so CI stays green on a bare runner while a
developer's own machine acts as the canary.

If Anthropic changes something, this fails before anyone points the tool at
their sessions.
"""

from __future__ import annotations

import json
import os

import pytest
from conftest import cs

pytestmark = pytest.mark.drift


def _store():
    try:
        root = cs.sessions_root()
    except SystemExit:
        return None
    return root if root.exists() else None


def _real_sessions():
    root = _store()
    if root is None:
        return []
    out = []
    for f in root.glob("*/*/local_*.json"):
        try:
            out.append((f, json.loads(f.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def _real_transcripts():
    out = []
    if cs.PROJECTS_DIR.exists():
        out += [(cs.WINDOWS_HOST, p) for p in cs.PROJECTS_DIR.glob("*/*.jsonl")]
    if os.name == "nt":
        for h in cs.discover_hosts():
            if h.is_wsl:
                out += [(h, p) for p in h.projects.glob("*/*.jsonl")]
    return out


needs_store = pytest.mark.skipif(_store() is None, reason="no real Claude Code store on this machine")
needs_transcripts = pytest.mark.skipif(not _real_transcripts(), reason="no real transcripts on this machine")


# --------------------------------------------------------------------------


@needs_transcripts
def test_encode_cwd_matches_real_store():
    """
    F1 + F2 together, against reality.

    Every transcript either sits in the directory its cwd encodes to, or in a
    directory that matches no cwd at all - the app's `ssh-<id>` mirrors. A third
    case would mean the encoding rule has changed.
    """
    unexplained = []
    for host, path in _real_transcripts():
        c = cs.read_cli_session(host, path, deep=False)
        if c is None:
            continue
        if cs.encode_cwd(c.cwd) == c.project_dir:
            continue
        if c.project_dir.startswith("ssh-"):
            continue  # a mirror of a remote session, named by session id
        unexplained.append((str(path), c.cwd, c.project_dir))

    assert not unexplained, (
        "transcripts whose directory is explained by neither the encoding rule "
        f"nor the ssh-mirror convention:\n" + "\n".join(map(str, unexplained))
    )


@needs_transcripts
def test_every_real_transcript_is_locatable():
    """F2: the path the tool would hand the app must exist, for every session."""
    dead = []
    for host, path in _real_transcripts():
        c = cs.read_cli_session(host, path, deep=False)
        if c is None or not host.is_wsl:
            continue
        posix = host.posix_transcript_path(c.project_dir, c.cli_id)
        if not cs.Path(cs.wsl_to_win_path(posix, host.distro)).exists():
            dead.append(posix)
    assert not dead, "locator produced paths that do not exist:\n" + "\n".join(dead)


@needs_transcripts
def test_entrypoint_values_are_known():
    """F7: an unrecognised entrypoint means the cli/app split needs revisiting."""
    seen = set()
    for host, path in _real_transcripts():
        c = cs.read_cli_session(host, path, deep=False)
        if c:
            seen |= c.entrypoints
    unknown = seen - {"cli", "claude-desktop"}
    assert not unknown, f"unknown entrypoint values, cli/app classification may be wrong: {unknown}"


@needs_store
def test_wsl_sessions_carry_both_fields():
    """
    F8 + F9: a WSL session is identified by wslConfig, and always carries the
    transcript path inside the distro. One without the other would break
    resolution and make `copy` refuse it under S6.
    """
    broken = []
    for path, data in _real_sessions():
        has_cfg = isinstance(data.get("wslConfig"), dict) and data["wslConfig"].get("distro")
        has_path = bool(data.get("sshRemoteTranscriptPath"))
        if has_cfg and not has_path:
            broken.append(f"{path.name}: wslConfig without sshRemoteTranscriptPath")
    assert not broken, "\n".join(broken)


@needs_store
def test_session_metadata_has_the_fields_we_depend_on():
    """The keys every command reads. A rename upstream shows up here first."""
    required = {"sessionId", "cliSessionId", "cwd", "createdAt", "lastActivityAt", "title"}
    missing = []
    for path, data in _real_sessions():
        gaps = required - set(data)
        if gaps:
            missing.append(f"{path.name}: missing {sorted(gaps)}")
    assert not missing, "\n".join(missing)


@needs_store
def test_connector_uuids_really_are_org_scoped():
    """F5's premise: the same connector has a different uuid in each org."""
    parts = cs.load_partitions()
    if len(parts) < 2:
        pytest.skip("need two partitions to compare")
    shared = []
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            for name, uuid in a.connectors.items():
                if b.connectors.get(name) == uuid and a.org != b.org:
                    shared.append(f"{name} has the same uuid in {a.name} and {b.name}")
    # not an error - a connector CAN be installed once and shared - but if it
    # were true of all of them, the remapping would be pointless
    assert len(shared) < sum(len(p.connectors) for p in parts), (
        "every connector shares a uuid across orgs; remapping may no longer be needed:\n"
        + "\n".join(shared)
    )


@needs_store
def test_contested_connectors_still_resolve_by_clear_majority():
    """
    The connector map is decided by majority so that a stray import cannot
    redefine an org. That only holds while the majority is unambiguous - a tie
    would make the winner arbitrary, and every copy into that org suspect.
    """
    ties = []
    for p in cs.load_partitions():
        for name, tally in p.contested_connectors.items():
            counts = sorted(tally.values(), reverse=True)
            if len(counts) > 1 and counts[0] == counts[1]:
                ties.append(f"{p.name}/{name}: {tally}")
    assert not ties, (
        "connector uuid decided by a coin flip; inspect these partitions for "
        "sessions copied in without a ledger entry:\n" + "\n".join(ties)
    )
