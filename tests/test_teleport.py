"""Cross-client journeys exercise the CLI against disposable stores."""
import json
import uuid
from pathlib import Path

import pytest
from conftest import cs

SID = 'a4c61cf0-1705-4017-8402-38c70b30ed75'
STAMP = '2026-09-28T10:00:00.000Z'
PROMPT = 'Remember the synthetic code: TELEPORT-47.'
ANSWER = 'The synthetic code is TELEPORT-47.'


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    return path


def claude_rows(cwd):
    rows = []
    parent = None
    for role, text in [('user', PROMPT), ('assistant', ANSWER)]:
        mid = str(uuid.uuid4())
        rows.append(dict(type=role, uuid=mid, parentUuid=parent, sessionId=SID,
                         timestamp=STAMP, cwd=str(cwd), isSidechain=False,
                         message=dict(role=role, content=[dict(type='text', text=text)])))
        parent = mid
    return rows


def codex_rows(cwd):
    return [dict(type='session_meta', timestamp=STAMP, payload=dict(id=SID, cwd=str(cwd), timestamp=STAMP)),
            *[dict(type='response_item', timestamp=STAMP, payload=dict(type='message', role=role,
              content=[dict(type=kind, text=text)])) for role, kind, text in
              [('user', 'input_text', PROMPT), ('assistant', 'output_text', ANSWER)]]]


@pytest.fixture(params=['claude', 'codex'])
def journey(request, tmp_path):
    source = request.param
    cwd = tmp_path / 'project with spaces'
    cwd.mkdir()
    rows = claude_rows(cwd) if source == 'claude' else codex_rows(cwd)
    path = write_rows(tmp_path / 'source.jsonl', rows)
    dest = tmp_path / 'destination'
    target = 'codex' if source == 'claude' else 'claude'
    argv = ['teleport', str(path), '--to', target, '--target-home', str(dest)]
    return source, path, dest, argv


def test_teleport_dry_run_apply_and_repeat(journey, capsys):
    source, path, dest, argv = journey
    original = path.read_bytes()
    assert cs.main(argv) == 0
    assert not dest.exists()
    assert 'DRY RUN' in capsys.readouterr().out
    assert cs.main([*argv, '--apply']) == 0
    files = list(dest.rglob('*.jsonl'))
    assert len(files) == 1
    imported = cs.read_portable_session(files[0])
    assert [(m.role, m.text) for m in imported.messages][-2:] == [('user', PROMPT), ('assistant', ANSWER)]
    assert imported.agent != source
    before = files[0].read_bytes()
    assert cs.main([*argv, '--apply']) == 0
    assert files[0].read_bytes() == before
    assert path.read_bytes() == original
    assert 'already exists' in capsys.readouterr().out


def test_codex_has_both_model_history_and_desktop_history(journey):
    source, path, dest, argv = journey
    if source != 'claude':
        pytest.skip('Codex destination only')
    cs.main([*argv, '--apply'])
    rows = [json.loads(x) for x in next(dest.rglob('*.jsonl')).read_text().splitlines()]
    assert [r['payload']['message'] for r in rows if r['type'] == 'event_msg'][-2:] == [PROMPT, ANSWER]
    assert {r['payload']['type'] for r in rows if r['type'] == 'response_item'} == {'message'}
    assert rows[0]['payload']['history_mode'] == 'legacy'


@pytest.mark.parametrize('agent', ['claude', 'codex'])
def test_tools_are_text_and_private_reasoning_is_omitted(tmp_path, agent):
    rows = claude_rows(tmp_path) if agent == 'claude' else codex_rows(tmp_path)
    if agent == 'claude':
        rows[-1]['message']['content'] += [dict(type='thinking', thinking='PRIVATE'),
            dict(type='tool_use', id='tool1', name='Bash', input={'command': 'echo EVIDENCE'})]
        rows.append({**rows[0], 'uuid':str(uuid.uuid4()), 'parentUuid':rows[-1]['uuid'],
                     'message':{'role':'user','content':[dict(type='tool_result', tool_use_id='tool1', content='EVIDENCE')]}})
    else:
        rows += [dict(type='response_item', payload=p) for p in [
            dict(type='reasoning', encrypted_content='PRIVATE'),
            dict(type='function_call', call_id='tool1', name='exec_command', arguments='{"cmd":"echo EVIDENCE"}'),
            dict(type='function_call_output', call_id='tool1', output='EVIDENCE')]]
    session = cs.read_portable_session(write_rows(tmp_path/'source.jsonl', rows))
    texts = '\n'.join(m.text for m in session.messages)
    assert 'PRIVATE' not in texts
    assert 'EVIDENCE' in texts and 'tool1' in texts
    assert session.notices


def test_claude_follows_latest_branch_not_siblings(tmp_path):
    rows = claude_rows(tmp_path)
    rows.append({**rows[-1], 'uuid':str(uuid.uuid4()), 'message':{'role':'assistant','content':'Chosen branch'}})
    session = cs.read_portable_session(write_rows(tmp_path/'source.jsonl', rows))
    assert [m.text for m in session.messages] == [PROMPT, 'Chosen branch']


def test_codex_compaction_uses_replacement_context(tmp_path):
    rows = codex_rows(tmp_path)
    rows.append(dict(type='compacted', payload=dict(message='', replacement_history=[dict(type='message', role='user', content=[dict(type='input_text', text='Current summary')])])))
    rows.append(codex_rows(tmp_path)[-1])
    session = cs.read_portable_session(write_rows(tmp_path/'source.jsonl', rows))
    assert [m.text for m in session.messages] == ['Current summary', ANSWER]
    assert session.notices


@pytest.mark.parametrize('bad', ['{broken', '[]', '{"type":"unknown"}'])
def test_bad_source_fails_without_writes(tmp_path, bad):
    path = tmp_path/'bad.jsonl'; path.write_text(bad)
    dest = tmp_path/'destination'
    with pytest.raises(SystemExit):
        cs.main(['teleport',str(path),'--to','codex','--target-home',str(dest),'--apply'])
    assert not dest.exists()


def test_missing_branch_parent_fails(tmp_path):
    rows = claude_rows(tmp_path); rows[0]['parentUuid'] = 'missing'
    with pytest.raises(ValueError, match='parent'):
        cs.read_portable_session(write_rows(tmp_path/'source.jsonl',rows))


def test_desktop_metadata_uses_destination_and_resets_permissions(world, tmp_path):
    part = world.partition('account', 'org')
    world.sign_in('account', 'org')
    world.add_session(part, 'donor', 'cli-donor', permissionMode='bypassPermissions',
                      alwaysAllowedReasons=['danger'],sessionPermissionUpdates=[{'danger':True}])
    source = write_rows(tmp_path/'source.jsonl',codex_rows(tmp_path))
    args=['teleport',str(source),'--to','claude','--target-home',str(world.projects.parent),'--desktop-partition','active']
    assert cs.main(args) == 0
    assert len(list(part.glob('local_*.json'))) == 1
    assert cs.main([*args,'--apply']) == 0
    metadata=json.loads(next(p for p in part.glob('local_*.json') if p.name!='local_donor.json').read_text())
    assert metadata['permissionMode']=='auto'
    assert metadata['alwaysAllowedReasons']==[]
    assert metadata['sessionPermissionUpdates']==[]
    assert 'wslConfig' not in metadata
    assert (world.projects / cs.encode_cwd(str(tmp_path)) / (metadata['cliSessionId']+'.jsonl')).exists()


def test_codex_fork_with_inherited_parent_header(tmp_path):
    rows = codex_rows(tmp_path)
    rows.insert(1, {**rows[0], 'payload': {**rows[0]['payload'], 'id': str(uuid.uuid4())}})
    session = cs.read_portable_session(write_rows(tmp_path/'fork.jsonl', rows))
    assert session.session_id == SID
    assert [m.text for m in session.messages] == [PROMPT, ANSWER]


def test_archived_codex_import_is_not_resurrected(tmp_path, capsys):
    source = write_rows(tmp_path/'source.jsonl', claude_rows(tmp_path))
    dest = tmp_path/'codex'
    args = ['teleport', str(source), '--to', 'codex', '--target-home', str(dest), '--apply']
    cs.main(args)
    original = next(dest.rglob('*.jsonl'))
    archived = dest/'archived_sessions'/original.name
    archived.parent.mkdir(); original.rename(archived)
    content = archived.read_bytes()
    cs.main(args)
    assert archived.read_bytes() == content and not original.exists()
    assert 'already exists' in capsys.readouterr().out


def test_desktop_tombstone_blocks_import(world, tmp_path):
    part = world.partition('account', 'org'); world.sign_in('account', 'org')
    source = write_rows(tmp_path/'source.jsonl', codex_rows(tmp_path))
    args=['teleport',str(source),'--to','claude','--target-home',str(world.projects.parent),
          '--desktop-partition','active','--apply']
    cs.main(args)
    metadata = next(part.glob('local_*.json'))
    world.add_tombstone(part, metadata.stem.removeprefix('local_'))
    metadata.unlink()
    imported = next(world.projects.rglob('*.jsonl')); imported.unlink()
    snapshot = world.snapshot(part, world.projects)
    with pytest.raises(SystemExit):
        cs.main(args)
    assert world.snapshot(part, world.projects) == snapshot


def test_wsl_desktop_points_at_imported_transcript(world, tmp_path):
    part = world.partition('account','org'); world.sign_in('account','org')
    cwd='/home/me/project'; (world.wsl_root/cwd.lstrip('/')).mkdir(parents=True)
    source=write_rows(tmp_path/'source.jsonl',codex_rows(tmp_path))
    assert cs.main(['teleport',str(source),'--to','claude','--target-host','wsl:Testbuntu',
                    '--cwd',cwd,'--desktop-partition','active','--apply']) == 0
    data=json.loads(next(part.glob('local_*.json')).read_text())
    assert data['wslConfig']=={'distro':'Testbuntu'}
    assert (world.wsl_root/data['sshRemoteTranscriptPath'].lstrip('/')).exists()
    assert not list(world.projects.rglob('*.jsonl'))


def test_publish_failure_rolls_back_only_our_files(tmp_path, monkeypatch):
    transcript, metadata = tmp_path/'transcript.jsonl', tmp_path/'metadata.json'
    publish = cs.publish_teleport_file

    def competing_writer(src, dst):
        if Path(dst).name == metadata.name:
            dst.write_text('concurrent writer')
        return publish(src, dst)

    monkeypatch.setattr(cs, 'publish_teleport_file', competing_writer)
    with pytest.raises(FileExistsError):
        cs._publish_teleport([(transcript, 'ours'), (metadata, 'ours')], None)
    assert not transcript.exists()
    assert metadata.read_text() == 'concurrent writer'
    assert not list(tmp_path.glob('.teleport-*'))


@pytest.mark.parametrize('fault', ['truncated', 'rollback', 'compaction', 'empty', 'same-client', 'cwd'])
def test_refused_import_does_not_create_destination(tmp_path, fault):
    rows=codex_rows(tmp_path)
    target='claude'
    if fault=='rollback':rows.append(dict(type='event_msg',payload=dict(type='thread_rolled_back',num_turns=1)))
    if fault=='compaction':rows.append(dict(type='compacted',payload=dict(message='summary only')))
    if fault=='empty':rows=rows[:1]
    if fault=='same-client':target='codex'
    if fault=='cwd':rows[0]['payload']['cwd']=str(tmp_path/'missing')
    source=write_rows(tmp_path/'source.jsonl',rows)
    if fault=='truncated':
        with source.open('a') as stream:stream.write('{"type":')
    dest=tmp_path/'destination'
    with pytest.raises(SystemExit):
        cs.main(['teleport',str(source),'--to',target,'--target-home',str(dest),'--apply'])
    assert not dest.exists()


def test_images_get_visible_placeholder_and_notice(tmp_path):
    rows=claude_rows(tmp_path)
    rows[0]['message']['content'].append(dict(type='image',source={'data':'SECRET-BINARY'}))
    result=cs.read_portable_session(write_rows(tmp_path/'source.jsonl',rows))
    assert 'SECRET-BINARY' not in str(result)
    assert 'unavailable' in result.messages[0].text
    assert any('image' in n for n in result.notices)


def test_source_permissions_and_instructions_never_transfer(tmp_path):
    rows=codex_rows(tmp_path)
    rows[0]['payload']['base_instructions']={'text':'SECRET-SYSTEM'}
    rows.insert(1,dict(type='response_item',payload=dict(type='message',role='developer',content=[dict(type='input_text',text='SECRET-DEVELOPER')])))
    rows.insert(2,dict(type='turn_context',payload=dict(approval_policy='never',sandbox_policy={'type':'danger-full-access'})))
    source=write_rows(tmp_path/'source.jsonl',rows);dest=tmp_path/'dest'
    cs.main(['teleport',str(source),'--to','claude','--target-home',str(dest),'--apply'])
    output=next(dest.rglob('*.jsonl')).read_text()
    for secret in ['SECRET-SYSTEM','SECRET-DEVELOPER','danger-full-access','approval_policy']:
        assert secret not in output


@pytest.mark.parametrize('agent', ['claude','codex'])
def test_discovery_lists_usable_paths_without_writes(tmp_path, capsys, agent):
    home=tmp_path/'store'
    path=home/'projects'/'encoded'/'session.jsonl' if agent=='claude' else home/'sessions'/'2026'/'09'/'28'/'rollout.jsonl'
    rows=claude_rows(tmp_path) if agent=='claude' else codex_rows(tmp_path)
    write_rows(path, rows)
    original=path.read_bytes()
    assert cs.main(['sessions','--agent',agent,'--home',str(home),'-n','1'])==0
    out=capsys.readouterr().out
    assert str(path) in out and SID in out
    assert path.read_bytes()==original


def test_destination_honors_client_environment(tmp_path, monkeypatch):
    for source_agent, target, key in [('claude','codex','CODEX_HOME'),('codex','claude','CLAUDE_CONFIG_DIR')]:
        dest=tmp_path/target
        monkeypatch.setenv(key,str(dest))
        rows=claude_rows(tmp_path) if source_agent=='claude' else codex_rows(tmp_path)
        source=write_rows(tmp_path/(source_agent+'.jsonl'),rows)
        cs.main(['teleport',str(source),'--to',target,'--apply'])
        assert len(list(dest.rglob('*.jsonl')))==1


def test_publish_supports_long_destination_paths(tmp_path):
    # Windows CreateHardLink needs extended paths even when open() succeeds.
    folder=tmp_path/('a'*100)/('b'*100)
    target=folder/('c'*70+'.jsonl')
    cs._publish_teleport([(target,'complete transcript')],None)
    assert cs.native_path(target).read_text()=='complete transcript'
    assert not list(cs.native_path(folder).glob('.teleport-*'))


def test_claude_destination_uses_transcript_location_after_directory_visits(tmp_path):
    cwd=tmp_path/'project';cwd.mkdir();subdir=cwd/'subdir';subdir.mkdir()
    rows=claude_rows(cwd);rows[-1]['cwd']=str(subdir)
    source=write_rows(tmp_path/'projects'/cs.encode_cwd(str(cwd))/'session.jsonl',rows)
    assert cs.read_portable_session(source).cwd==str(cwd)
