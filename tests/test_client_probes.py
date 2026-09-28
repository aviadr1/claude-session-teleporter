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
from teleport_support import (ANSWER, PROMPT, claude_rows, codex_rows, write_rows,
                              tool_rows, TOOL_OUTPUT, CALL_ID, teleport, native_history, parallel_branch_rows)

pytestmark = pytest.mark.skipif(os.environ.get('RUN_CLIENT_PROBES') != '1', reason='opt-in real client probes')


def reply_events(path, text):
    """Minimal successful external API streams; all client persistence is real."""
    if 'messages' in path:
        return [
            dict(type='message_start', message=dict(id='msg_probe',type='message',role='assistant',
                 model='claude-sonnet-4-6',content=[],stop_reason=None,stop_sequence=None,
                 usage=dict(input_tokens=20,output_tokens=0))),
            dict(type='content_block_start',index=0,content_block=dict(type='text',text='')),
            dict(type='content_block_delta',index=0,delta=dict(type='text_delta',text=text)),
            dict(type='content_block_stop',index=0),
            dict(type='message_delta',delta=dict(stop_reason='end_turn',stop_sequence=None),usage=dict(output_tokens=5)),
            dict(type='message_stop'),
        ]
    part=dict(type='output_text',text=text,annotations=[])
    item=dict(id='msg_probe',type='message',role='assistant',status='completed',content=[part])
    response=dict(id='resp_probe',object='response',created_at=1,status='completed',model='probe-model',
                  output=[item],usage=dict(input_tokens=20,output_tokens=5,total_tokens=25))
    return [
        dict(type='response.created',response={**response,'status':'in_progress','output':[]}),
        dict(type='response.output_item.added',output_index=0,item={**item,'status':'in_progress','content':[]}),
        dict(type='response.content_part.added',output_index=0,item_id='msg_probe',content_index=0,part={**part,'text':''}),
        dict(type='response.output_text.delta',output_index=0,item_id='msg_probe',content_index=0,delta=text),
        dict(type='response.output_text.done',output_index=0,item_id='msg_probe',content_index=0,text=text),
        dict(type='response.content_part.done',output_index=0,item_id='msg_probe',content_index=0,part=part),
        dict(type='response.output_item.done',output_index=0,item=item),
        dict(type='response.completed',response=response),
    ]


@contextmanager
def capture_api(reply=None):
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
                if reply is not None:
                    events = reply_events(self.path, reply)
                    wire = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(wire)))
                    self.end_headers()
                    self.wfile.write(wire)
                    return
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
        notifications = queue.Queue()

        def read():
            for line in process.stdout:
                message = json.loads(line)
                (output if 'id' in message else notifications).put(message)

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
            yield rpc, notifications
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


def claude_resume(home, cwd, url, session_id, prompt):
    """Own the complete CLI invocation, with one credential-free environment."""
    binary = shutil.which('claude')
    if not binary:
        pytest.skip('claude is not installed')
    env = clean_env(home.parent)
    env.update(CLAUDE_CONFIG_DIR=str(home), ANTHROPIC_BASE_URL=url,
               ANTHROPIC_API_KEY='synthetic-loopback-only', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    return subprocess.run([binary, '--bare', '-p', '--resume', session_id,
                           '--model', 'claude-sonnet-4-6', '--tools', '', '--setting-sources', '',
                           '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--', prompt],
                          env=env, cwd=cwd, capture_output=True, text=True, timeout=30)


def wait_completed(notifications):
    while True:
        notification = notifications.get(timeout=20)
        if notification.get('method') == 'turn/completed':
            assert notification['params']['turn']['status'] == 'completed', notification
            return


@pytest.mark.parametrize('assistant_first', [False, True])
def test_real_codex_discovery_read_resume_and_model_context(tmp_path, assistant_first):
    cwd = tmp_path/'project'; cwd.mkdir()
    rows = claude_rows(cwd)
    if assistant_first:
        rows = rows[1:]
        rows[0]['parentUuid'] = None
    source = write_rows(tmp_path/'claude.jsonl', rows)
    home = tmp_path/'codex'
    assert cs.main(['teleport', str(source), '--to', 'codex', '--target-home', str(home), '--apply']) == 0
    imported = cs.read_portable_session(next(home.rglob('*.jsonl')))
    with capture_api() as (url, requests):
        for restart in range(2):
            with app_server(home, cwd, url) as (rpc, notifications):
                listing = rpc('thread/list', {'limit': 10})
                assert imported.session_id in [x['id'] for x in listing['data']]
                thread = rpc('thread/read', {'threadId': imported.session_id, 'includeTurns': True})['thread']
                display = json.dumps(thread['turns'])
                assert ANSWER in display
                assert ('[Import context]' if assistant_first else PROMPT) in display
                rpc('thread/resume', {'threadId': imported.session_id, 'modelProvider': 'probe',
                                     'model': 'probe-model', 'approvalPolicy': 'untrusted', 'sandbox': 'read-only'})
                if restart:
                    rpc('turn/start', {'threadId': imported.session_id,
                                      'input': [{'type': 'text', 'text': 'Recall the synthetic code.'}]})
                    request = requests.get(timeout=20)
                    context = json.dumps(request['input'])
                    assert ANSWER in context
                    assert ('[Import context]' if assistant_first else PROMPT) in context
                    assert not any(x.get('type') in ('function_call', 'custom_tool_call') for x in request['input'])


def test_real_claude_resume_model_context(tmp_path):
    cwd = tmp_path/'project'; cwd.mkdir()
    source = write_rows(tmp_path/'codex.jsonl', codex_rows(cwd))
    home = tmp_path/'claude'
    assert cs.main(['teleport', str(source), '--to', 'claude', '--target-home', str(home), '--apply']) == 0
    imported = cs.read_portable_session(next(home.rglob('*.jsonl')))
    with capture_api() as (url, requests):
        result = claude_resume(home, cwd, url, imported.session_id, 'Recall the synthetic code.')
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


def test_real_codex_tool_evidence_and_completed_turn_survive_restart(tmp_path):
    cwd=tmp_path/'project';cwd.mkdir();home=tmp_path/'codex'
    source=write_rows(tmp_path/'claude.jsonl',parallel_branch_rows(cwd))
    expected=native_history(source,'claude')
    path=teleport(source,'codex',home)
    marker=TOOL_OUTPUT
    imported=cs.read_portable_session(next(home.rglob('*.jsonl')))
    reply='Synthetic persisted reply'
    with capture_api(reply=reply) as (url, requests):
        with app_server(home,cwd,url) as (rpc,notifications):
            rpc('thread/resume',dict(threadId=imported.session_id,modelProvider='probe',model='probe-model',sandbox='read-only',approvalPolicy='untrusted'))
            rpc('turn/start',dict(threadId=imported.session_id,input=[dict(type='text',text='Continue the synthetic test')]))
            request=requests.get(timeout=20)
            evidence=[item for item in request['input'] if marker in json.dumps(item)]
            assert evidence and all(item.get('type')=='function_call_output' for item in evidence)
            assert any(item.get('type')=='function_call' and item.get('call_id')==CALL_ID for item in request['input'])
            wait_completed(notifications)
        with app_server(home,cwd,url) as (rpc,notifications):
            thread=rpc('thread/read',dict(threadId=imported.session_id,includeTurns=True))['thread']
            visible=json.dumps(thread['turns'])
            for text in (PROMPT,ANSWER,marker,reply):assert text in visible
            tools=[item for turn in thread['turns'] for item in turn['items'] if item['type']=='mcpToolCall']
            assert {item['id'] for item in tools}=={CALL_ID,'second'}
            assert {item['id']:item['status'] for item in tools}=={CALL_ID:'completed','second':'failed'}
            rpc('thread/resume',dict(threadId=imported.session_id,modelProvider='probe',model='probe-model',sandbox='read-only',approvalPolicy='untrusted'))
            rpc('turn/start',dict(threadId=imported.session_id,input=[dict(type='text',text='Check durable context')]))
            request=requests.get(timeout=20)
            for text in (PROMPT,ANSWER,marker,reply):assert text in json.dumps(request['input'])
            wait_completed(notifications)

    returned=teleport(path,'claude',tmp_path/'returned')
    assert native_history(returned,'claude')[:len(expected)]==expected


def test_real_claude_tool_evidence_and_completed_turn_survive_restart(tmp_path):
    cwd=tmp_path/'project';cwd.mkdir();home=tmp_path/'claude'
    source=write_rows(tmp_path/'codex.jsonl',tool_rows('codex',cwd))
    expected=native_history(source,'codex')
    path=teleport(source,'claude',home)
    marker=TOOL_OUTPUT
    imported=cs.read_portable_session(next(home.rglob('*.jsonl')))
    reply='Synthetic persisted reply'
    with capture_api(reply=reply) as (url,requests):
        for iteration in range(2):
            result=claude_resume(home,cwd,url,imported.session_id,f'Continue the synthetic test {iteration}')
            assert result.returncode==0 and reply in result.stdout,result.stdout+result.stderr
            request=requests.get(timeout=2)
            evidence=[m for m in request['messages'] if marker in json.dumps(m)]
            assert evidence and all(m['role']=='user' for m in evidence)
            blocks=[b for m in evidence for b in m['content'] if marker in json.dumps(b)]
            assert blocks and all(b['type']=='tool_result' for b in blocks)
            assert any(b.get('type')=='tool_use' and b.get('id')==CALL_ID for m in request['messages'] for b in m['content'])
            for text in (PROMPT,ANSWER,marker):assert text in json.dumps(request['messages'])
            if iteration:assert reply in json.dumps(request['messages'])

    returned=teleport(path,'codex',tmp_path/'returned')
    assert native_history(returned,'codex')[:len(expected)]==expected
