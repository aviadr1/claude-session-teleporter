"""Adversarial invariants found during the post-implementation red-team pass."""
import json
from pathlib import Path

import pytest
from conftest import cs
from teleport_support import claude_rows, codex_rows, write_rows, tool_rows, TOOL_OUTPUT, native_history


def test_failed_desktop_publication_preserves_replaced_transcript(tmp_path, monkeypatch):
    transcript, metadata = tmp_path/'transcript.jsonl', tmp_path/'metadata.json'
    publish = cs.publish_teleport_file

    def competing_writer(temporary, target):
        if Path(target).name == metadata.name:
            transcript.unlink()
            transcript.write_text('new session owned by another writer')
            raise OSError('metadata storage unavailable')
        publish(temporary, target)

    monkeypatch.setattr(cs, 'publish_teleport_file', competing_writer)
    with pytest.raises(OSError):
        cs._publish_teleport([(transcript, 'our import'), (metadata, '{}')], None)
    assert transcript.read_text() == 'new session owned by another writer'


@pytest.mark.parametrize('source_agent', ['claude', 'codex'])
def test_tool_output_never_becomes_a_user_instruction(tmp_path, source_agent):
    source = write_rows(tmp_path/'source.jsonl', tool_rows(source_agent,tmp_path))
    session = cs.read_portable_session(source)
    evidence = [m for m in session.messages if m.tool and TOOL_OUTPUT in json.dumps(m.tool)]
    assert evidence and all(m.role == 'tool' for m in evidence)


def test_claude_snapshot_failure_does_not_relabel_unknown_records_as_history(tmp_path):
    rows = claude_rows(tmp_path)
    rows[-1]['message']['content'] = [{'type':'future_control_item', 'text':'not supported'}]
    # A visible placeholder is acceptable; omission must never look lossless.
    session = cs.read_portable_session(write_rows(tmp_path/'source.jsonl',rows))
    assert session.notices
    assert 'unavailable' in session.messages[-1].text


def test_retry_of_partial_desktop_import_is_not_reported_successful(world, tmp_path):
    world.partition('account','org');world.sign_in('account','org')
    source=write_rows(tmp_path/'source.jsonl',codex_rows(tmp_path))
    base=['teleport',str(source),'--to','claude','--target-home',str(world.projects.parent),'--apply']
    cs.main(base)
    with pytest.raises(SystemExit):
        cs.main([*base,'--desktop-partition','active'])


def test_assistant_first_history_gets_explicit_import_context(tmp_path):
    rows=claude_rows(tmp_path)[1:];rows[0]['parentUuid']=None
    source=write_rows(tmp_path/'source.jsonl',rows)
    dest=tmp_path/'codex'
    cs.main(['teleport',str(source),'--to','codex','--target-home',str(dest),'--apply'])
    path=next(dest.rglob('*.jsonl'))
    native=native_history(path,'codex')
    assert native[0]['role']=='user'
    assert '[Import context]' in json.dumps(native[0])
    result=cs.read_portable_session(path)
    assert result.messages[0].role=='assistant'
    assert result.messages[-1].text==rows[0]['message']['content'][0]['text']


def test_mixed_claude_content_keeps_user_text_separate_from_tool_output(tmp_path):
    rows=tool_rows('claude',tmp_path)
    content=rows[-1]['message']['content']
    content.insert(0,dict(type='text',text='Before'))
    content[-1]['text']='After'
    result=cs.read_portable_session(write_rows(tmp_path/'source.jsonl',rows))
    messages=result.messages[-4:]
    assert [(m.role,m.text) for m in messages]==[('user','Before'),('tool',''),('tool',''),('user','After')]
    assert messages[2].tool['output']==TOOL_OUTPUT


@pytest.mark.parametrize('mutation',['replace','edit','delete'])
def test_cleanup_preserves_a_consumers_changes(tmp_path, monkeypatch, mutation):
    transcript, metadata = tmp_path/'transcript.jsonl',tmp_path/'metadata.json'
    publish=cs.publish_teleport_file
    changed='resumed session with new work'

    def change_after_first_publish(src,dst):
        if Path(dst).name==metadata.name:
            if mutation in ('replace','delete'):transcript.unlink()
            if mutation!='delete':transcript.write_text(changed)
            raise OSError('metadata write failed')
        publish(src,dst)

    monkeypatch.setattr(cs,'publish_teleport_file',change_after_first_publish)
    with pytest.raises(OSError,match='metadata write failed'):
        cs._publish_teleport([(transcript,'original'),(metadata,'{}')],None)
    if mutation=='delete':assert not transcript.exists()
    else:assert transcript.read_text()==changed
