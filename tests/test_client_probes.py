"""Opt-in real-client compatibility tests; isolated homes and loopback APIs only.

RUN_CLIENT_PROBES=1 uv run pytest tests/test_client_probes.py -v
No user auth, remote inference, or desktop-store mutation is required.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import tempfile
from pathlib import Path
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import cs
from test_teleport import ANSWER, PROMPT, claude_rows, codex_rows, write_rows

pytestmark = pytest.mark.skipif(os.environ.get('RUN_CLIENT_PROBES') != '1', reason='opt-in real client probes')


@contextmanager
def capture_api():
    requests = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                status, body = 200, {'input_tokens': 10}
            else:
                requests.put(data)
                status, body = 400, {'type': 'error', 'error': {'type': 'invalid_request_error',
                                                            'message': 'Synthetic capture complete'}}
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def clean_env(home):
    # These clients must never inherit the operator's credentials or provider routes.
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ('ANTHROPIC_', 'OPENAI_', 'CODEX_', 'CLAUDE_', 'CLAUDECODE'))}
    env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home/'config'))
    return env


@contextmanager
def app_server(home, cwd, base_url):
    binary = shutil.which('codex')
    if not binary:
        pytest.skip('codex is not installed')
    env = clean_env(home)
    env['CODEX_HOME'] = str(home)
    args = [binary, '-c', 'model_provider="probe"', '-c', 'model="probe-model"',
            '-c', 'model_providers.probe.name="Local probe"',
            '-c', f'model_providers.probe.base_url="{base_url}"',
            '-c', 'model_providers.probe.wire_api="responses"',
            '-c', 'model_providers.probe.supports_websockets=false', 'app-server']
    with (home/'probe-stderr.log').open('w') as errors:
        process = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8')
        output = queue.Queue()

        def read():
            for line in process.stdout:
                output.put(json.loads(line))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        sequence = 0

        def rpc(method, params):
            nonlocal sequence
            sequence += 1
            process.stdin.write(json.dumps(dict(id=sequence, method=method, params=params))+'\n')
            process.stdin.flush()
            while True:
                value = output.get(timeout=20)
                if value.get('id') == sequence:
                    assert 'error' not in value, value
                    return value['result']

        try:
            rpc('initialize', {'clientInfo': {'name': 'teleporter_probe', 'version': '1.0'},
                               'capabilities': {'experimentalApi': True}})
            yield rpc
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            reader.join(timeout=5)


def test_real_codex_discovery_read_resume_and_model_context(tmp_path):
    cwd = tmp_path/'project'; cwd.mkdir()
    source = write_rows(tmp_path/'claude.jsonl', claude_rows(cwd))
    home = tmp_path/'codex'
    assert cs.main(['teleport', str(source), '--to', 'codex', '--target-home', str(home), '--apply']) == 0
    imported = cs.read_portable_session(next(home.rglob('*.jsonl')))
    with capture_api() as (url, requests):
        for restart in range(2):
            with app_server(home, cwd, url) as rpc:
                listing = rpc('thread/list', {'limit': 10, 'modelProviders': []})
                assert imported.session_id in [x['id'] for x in listing['data']]
                thread = rpc('thread/read', {'threadId': imported.session_id, 'includeTurns': True})['thread']
                display = json.dumps(thread['turns'])
                assert PROMPT in display and ANSWER in display
                rpc('thread/resume', {'threadId': imported.session_id, 'modelProvider': 'probe',
                                     'model': 'probe-model', 'approvalPolicy': 'untrusted', 'sandbox': 'read-only'})
                if restart:
                    rpc('turn/start', {'threadId': imported.session_id,
                                      'input': [{'type': 'text', 'text': 'Recall the synthetic code.'}]})
                    request = requests.get(timeout=20)
                    context = json.dumps(request['input'])
                    assert PROMPT in context and ANSWER in context
                    assert not any(x.get('type') in ('function_call', 'custom_tool_call') for x in request['input'])


def test_real_claude_resume_model_context(tmp_path):
    binary = shutil.which('claude')
    if not binary:
        pytest.skip('claude is not installed')
    cwd = tmp_path/'project'; cwd.mkdir()
    source = write_rows(tmp_path/'codex.jsonl', codex_rows(cwd))
    home = tmp_path/'claude'
    assert cs.main(['teleport', str(source), '--to', 'claude', '--target-home', str(home), '--apply']) == 0
    imported = cs.read_portable_session(next(home.rglob('*.jsonl')))
    with capture_api() as (url, requests):
        env = clean_env(tmp_path)
        env.update(CLAUDE_CONFIG_DIR=str(home), ANTHROPIC_BASE_URL=url,
                   ANTHROPIC_API_KEY='synthetic-loopback-only', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        result = subprocess.run([binary, '--bare', '-p', '--resume', imported.session_id,
                                 '--model', 'claude-sonnet-4-6', '--tools', '', '--setting-sources', '',
                                 '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                                 '--', 'Recall the synthetic code.'], env=env, cwd=cwd,
                                capture_output=True, text=True, timeout=30)
        assert 'Synthetic capture complete' in result.stdout, result.stdout + result.stderr
        request = requests.get(timeout=2)
        messages = request['messages']
        assert PROMPT in json.dumps(messages[0]) and messages[0]['role'] == 'user'
        assert ANSWER in json.dumps(messages[1]) and messages[1]['role'] == 'assistant'


def test_real_windows_to_wsl_publication():
    root = os.environ.get('TELEPORT_WSL_PROBE_ROOT')
    if os.name != 'nt' or not root:
        pytest.skip('requires Windows and TELEPORT_WSL_PROBE_ROOT pointing at a WSL temp directory')
    with tempfile.TemporaryDirectory(prefix='teleport-wsl-probe-', dir=root) as folder:
        target = Path(folder)/'synthetic.jsonl'
        cs._publish_teleport([(target, 'synthetic transcript')], None)
        assert target.read_text() == 'synthetic transcript'
        with pytest.raises(FileExistsError):
            cs._publish_teleport([(target, 'replacement')], None)
        assert target.read_text() == 'synthetic transcript'
        assert not list(Path(folder).glob('.teleport-*'))
