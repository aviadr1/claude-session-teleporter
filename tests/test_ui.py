"""Local browser UI: bulk transfer, preview integrity, and loopback boundary."""

import http.client
import json
import threading
from pathlib import Path

from conftest import cs


def test_sessions_root_finds_microsoft_store_claude(tmp_path, monkeypatch):
    monkeypatch.delenv('CLAUDE_SESSIONS_ROOT', raising=False)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    store = tmp_path / 'Local' / 'Packages' / 'Claude_test' / 'LocalCache' / 'Roaming' / 'Claude' / 'claude-code-sessions'
    store.mkdir(parents=True)
    assert cs.sessions_root() == store
    override = tmp_path / 'chosen-store'
    monkeypatch.setenv('CLAUDE_SESSIONS_ROOT', str(override))
    assert cs.sessions_root() == override


def test_ui_search_catalog_includes_older_sessions(world):
    world.monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(world.projects.parent))
    world.monkeypatch.setenv('CODEX_HOME', str(world.tmp / 'empty-codex'))
    part = world.partition('account', 'organization')
    for index in range(205):
        world.add_session(part, f'{index:08x}-0000-4000-8000-000000000001',
                          f'cli-{index}', title=f'Session {index}')
        world.add_transcript(r'C:\repo', f'cli-{index}')
    catalog = cs.ui_catalog()
    assert len(catalog['sessions']) == 205
    assert any(row['title'] == 'Session 0' for row in catalog['sessions'])


class Browser:
    def __init__(self):
        self.server = cs.make_ui_server()
        self.token = self.server.launch_url.rsplit('#', 1)[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, data=None, **headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        base = {'Host': f'127.0.0.1:{self.server.server_port}'}
        if data is not None:
            base.update({'Content-Type': 'application/json', 'X-Teleporter-Token': self.token})
        base.update(headers)
        conn.request('POST' if data is not None else 'GET', path,
                     body=json.dumps(data).encode() if data is not None else None, headers=base)
        response = conn.getresponse()
        raw = response.read()
        result = raw.decode() if path == '/' and response.status == 200 else json.loads(raw)
        status = response.status
        conn.close()
        return status, result


def test_ui_bulk_copy_previews_then_preserves_sources(two_orgs):
    world = two_orgs
    world.monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(world.projects.parent))
    world.monkeypatch.setenv('CODEX_HOME', str(world.tmp / 'empty-codex'))
    second = '3333aaaa-0000-4000-8000-000000000003'
    world.add_session(world.a, second, 'cli-a-2', title='second A session')
    world.add_transcript(r'C:\repo', 'cli-a-2')
    world.add_session(world.a, '4444aaaa-0000-4000-8000-000000000004',
                      'cli-a-archived', title='archived session', isArchived=True)
    world.add_transcript(r'C:\repo', 'cli-a-archived')
    source_before = world.snapshot(world.a, world.projects)
    browser = Browser()
    try:
        status, html = browser.request('/')
        assert status == 200
        assert 'Session teleporter' in html
        assert 'Last active' in html
        assert 'https://' not in html
        status, catalog = browser.request('/api/catalog', {})
        assert status == 200
        assert any(d['id'] == 'claude:' + world.ACCOUNT + '/' + world.ORG_B for d in catalog['destinations'])
        assert not any('cli-a-archived' in r['id'] for r in catalog['sessions'])
        selected = [r['id'] for r in catalog['sessions'] if r.get('partition') == world.ACCOUNT + '/' + world.ORG_A]
        assert len(selected) == 2
        assert all(isinstance(r['modified'], int) and r['modified'] > 0
                   for r in catalog['sessions'] if r['id'] in selected)
        status, preview = browser.request('/api/preview', {'sessions': selected, 'to': 'claude:' + world.ACCOUNT + '/' + world.ORG_B})
        assert status == 200, preview
        assert len(preview['sessions']) == 2
        assert world.snapshot(world.a, world.projects) == source_before
        assert not (world.b / f'local_{second}.json').exists()
        status, result = browser.request('/api/apply', {'preview': preview['preview']})
        assert status == 200, result
        assert result['ok'] and len(result['results']) == 2
        assert (world.b / f'local_{second}.json').exists()
        assert world.snapshot(world.a, world.projects) == source_before
        status, error = browser.request('/api/apply', {'preview': preview['preview']})
        assert status == 400 and 'already used' in error['error']
    finally:
        browser.close()


def test_ui_rejects_changed_source_and_foreign_browser(two_orgs):
    world = two_orgs
    world.monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(world.projects.parent))
    world.monkeypatch.setenv('CODEX_HOME', str(world.tmp / 'empty-codex'))
    browser = Browser()
    try:
        status, _ = browser.request('/api/catalog', {}, **{'X-Teleporter-Token': 'wrong'})
        assert status == 403
        status, _ = browser.request('/api/catalog', {}, Origin='https://example.com')
        assert status == 403
        status, catalog = browser.request('/api/catalog', {})
        source = next(r for r in catalog['sessions'] if r.get('partition') == world.ACCOUNT + '/' + world.ORG_A)
        destination = 'claude:' + world.ACCOUNT + '/' + world.ORG_B
        status, preview = browser.request('/api/preview', {'sessions': [source['id']], 'to': destination})
        assert status == 200, preview
        path = world.a / f"local_{source['id'].rsplit(':', 1)[1]}.json"
        path.write_bytes(path.read_bytes() + b'\n')
        status, error = browser.request('/api/apply', {'preview': preview['preview']})
        assert status == 400 and 'changed' in error['error'].lower()
        assert not (world.b / path.name).exists()
    finally:
        browser.close()


def test_ui_mixed_sources_into_one_claude_org(two_orgs):
    from teleport_support import write_rows, codex_rows

    world = two_orgs
    world.monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(world.projects.parent))
    codex_home = world.tmp / 'empty-codex'
    world.monkeypatch.setenv('CODEX_HOME', str(codex_home))
    source = write_rows(codex_home / 'sessions' / '2026' / '09' / '30' / 'rollout.jsonl', codex_rows(world.tmp))
    original = source.read_bytes()
    browser = Browser()
    try:
        status, catalog = browser.request('/api/catalog', {})
        assert status == 200
        chosen = [r['id'] for r in catalog['sessions'] if r['kind'] == 'codex' or r.get('partition') == world.ACCOUNT + '/' + world.ORG_A]
        assert len(chosen) == 2
        destination = 'claude:' + world.ACCOUNT + '/' + world.ORG_B
        status, preview = browser.request('/api/preview', {'sessions': chosen, 'to': destination})
        assert status == 200, preview
        status, result = browser.request('/api/apply', {'preview': preview['preview']})
        assert status == 200, result
        assert result['ok'], result
        assert source.read_bytes() == original
        assert len(list(world.b.glob('local_*.json'))) == 3
    finally:
        browser.close()


def test_ui_adopts_wsl_session_without_touching_transcript(two_orgs):
    world = two_orgs
    world.monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(world.projects.parent))
    world.monkeypatch.setenv('CODEX_HOME', str(world.tmp / 'empty-codex'))
    transcript = world.add_wsl_transcript('/home/me/project', 'wsl-ui-1', title='WSL research')
    original = transcript.read_bytes()
    browser = Browser()
    try:
        status, catalog = browser.request('/api/catalog', {})
        assert status == 200
        source = next(r for r in catalog['sessions'] if r['kind'] == 'wsl')
        destination = 'claude:' + world.ACCOUNT + '/' + world.ORG_B
        status, preview = browser.request('/api/preview', {'sessions': [source['id']], 'to': destination})
        assert status == 200, preview
        status, result = browser.request('/api/apply', {'preview': preview['preview']})
        assert status == 200 and result['ok'], result
        assert transcript.read_bytes() == original
        assert len(list(world.b.glob('local_*.json'))) == 2
    finally:
        browser.close()


def test_ui_claude_to_codex_preview_is_read_only(two_orgs):
    from teleport_support import write_rows, claude_rows

    world = two_orgs
    world.monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(world.projects.parent))
    codex_home = world.tmp / 'empty-codex'
    world.monkeypatch.setenv('CODEX_HOME', str(codex_home))
    cwd = world.tmp / 'project'
    cwd.mkdir()
    metadata = world.a / 'local_1111aaaa-0000-4000-8000-000000000001.json'
    data = json.loads(metadata.read_text())
    data['cwd'] = data['originCwd'] = str(cwd)
    metadata.write_text(json.dumps(data))
    write_rows(world.projects / cs.encode_cwd(str(cwd)) / 'ui-unique.jsonl',
               claude_rows(cwd))
    before = world.snapshot(world.a, world.projects)
    browser = Browser()
    try:
        status, catalog = browser.request('/api/catalog', {})
        assert status == 200
        source = next(r for r in catalog['sessions'] if r['kind'] == 'claude-cli'
                      and r['id'].endswith('ui-unique.jsonl'))
        status, preview = browser.request('/api/preview', {'sessions': [source['id']], 'to': 'codex'})
        assert status == 200, preview
        assert 'Codex' in preview['destination']
        assert world.snapshot(world.a, world.projects) == before
        assert not codex_home.exists()
    finally:
        browser.close()


def test_ui_peek_reads_turns_without_writing(two_orgs):
    from teleport_support import write_rows, claude_rows, PROMPT

    world = two_orgs
    world.monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(world.projects.parent))
    world.monkeypatch.setenv('CODEX_HOME', str(world.tmp / 'empty-codex'))
    write_rows(world.projects / cs.encode_cwd(r'C:\repo') / 'cli-a-1.jsonl',
               claude_rows(r'C:\repo'))
    before = world.snapshot(world.a, world.projects)
    browser = Browser()
    try:
        status, html = browser.request('/')
        assert status == 200
        assert 'id="agent-filter"' in html and 'id="peek-modal"' in html
        status, catalog = browser.request('/api/catalog', {})
        assert status == 200
        source = next(r for r in catalog['sessions'] if r.get('partition') == world.ACCOUNT + '/' + world.ORG_A)
        status, preview = browser.request('/api/peek', {'session': source['id']})
        assert status == 200, preview
        assert any(PROMPT in message['text'] for message in preview['messages'])
        assert preview['source'] == source['source']
        assert world.snapshot(world.a, world.projects) == before
        status, error = browser.request('/api/peek', {'session': 'x:/unknown'})
        assert status == 400
    finally:
        browser.close()
