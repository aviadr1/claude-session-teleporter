"""Project placement is separate from transcript cwd and never rewrites history."""
import pytest
from conftest import cs
from teleport_support import claude_rows, codex_rows, write_rows


def project(pid, *roots):
    return dict(id=pid, name=pid, roots=[dict(path=str(p)) for p in roots])


def test_auto_project_prefers_exact_roots_and_rejects_ambiguity(tmp_path):
    root = tmp_path/'repo'
    parent = project('parent', tmp_path)
    exact = project('exact', root)
    assert cs.select_codex_project([parent, exact], str(root), 'auto') == exact
    assert cs.select_codex_project([parent], str(root), 'auto') is None
    with pytest.raises(ValueError, match='ambiguous'):
        cs.select_codex_project([exact, project('other', root)], str(root), 'auto')
    assert cs.select_codex_project([exact, project('other', root)], str(root), 'other')['id'] == 'other'
    with pytest.raises(ValueError, match='not found'):
        cs.select_codex_project([exact], str(root), 'absent')


def test_project_dry_run_never_starts_codex_or_creates_home(tmp_path, capsys):
    src = write_rows(tmp_path/'source.jsonl', claude_rows(tmp_path))
    home = tmp_path/'destination'
    assert cs.main(['teleport', str(src), '--to', 'codex', '--target-home', str(home),
                    '--codex-bin', '/missing/codex']) == 0
    assert not home.exists()
    assert 'Codex project: auto' in capsys.readouterr().out


def test_project_options_are_rejected_for_claude(tmp_path):
    src = write_rows(tmp_path/'source.jsonl', codex_rows(tmp_path))
    with pytest.raises(SystemExit):
        cs.main(['teleport', str(src), '--to', 'claude', '--codex-project', 'auto', '--apply'])


@pytest.mark.parametrize('roots,selector', [([], 'missing'), (['duplicate', 'duplicate'], 'auto')])
def test_invalid_selection_fails_without_guessing(tmp_path, roots, selector):
    projects = [project(str(i), tmp_path) for i, _ in enumerate(roots)]
    with pytest.raises(ValueError):
        cs.select_codex_project(projects, str(tmp_path), selector)


def test_unavailable_codex_fails_before_publishing(tmp_path):
    src = write_rows(tmp_path/'source.jsonl', claude_rows(tmp_path))
    home = tmp_path/'destination'
    with pytest.raises(SystemExit):
        cs.main(['teleport', str(src), '--to', 'codex', '--target-home', str(home),
                 '--codex-bin', str(tmp_path/'missing'), '--apply'])
    assert not home.exists()


def test_repair_refuses_other_store_and_archived_sessions(tmp_path):
    home = tmp_path/'home'
    for path in [tmp_path/'other.jsonl', home/'archived_sessions'/'rollout.jsonl']:
        write_rows(path, codex_rows(tmp_path))
        with pytest.raises(SystemExit):
            cs.main(['codex-project', str(path), '--target-home', str(home), '--apply'])


def test_shared_secondary_root_matches_once(tmp_path):
    item = project('multi', tmp_path/'first', tmp_path, tmp_path)
    assert cs.select_codex_project([item], str(tmp_path), 'auto') == item


def test_project_database_environment_tracks_destination(tmp_path, monkeypatch):
    home = tmp_path/'codex'
    index = str(tmp_path/'desktop-index')
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('CODEX_SQLITE_HOME', index)
    # Windows extended/non-extended spellings still address the same store.
    assert cs.codex_project_environment(cs.native_path(home))['CODEX_SQLITE_HOME'] == index
    other = tmp_path/'other'
    assert 'CODEX_SQLITE_HOME' not in cs.codex_project_environment(other)
    assert cs.codex_project_environment(other, index)['CODEX_SQLITE_HOME'] == index
    assert cs.select_codex_project([project('same', home)], str(cs.native_path(home)), 'auto')['id'] == 'same'


def test_explicit_transcript_only_import_needs_no_codex(tmp_path):
    src = write_rows(tmp_path/'source.jsonl', claude_rows(tmp_path))
    home = tmp_path/'destination'
    assert cs.main(['teleport', str(src), '--to', 'codex', '--target-home', str(home),
                    '--codex-project', 'none', '--codex-bin', str(tmp_path/'missing'), '--apply']) == 0
    assert len(list(home.rglob('*.jsonl'))) == 1
