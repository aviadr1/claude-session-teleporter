"""Native tool history and semantic round trips through the public CLI."""
import json

import pytest
from conftest import cs
from teleport_support import (claude_rows, write_rows, TOOL_OUTPUT, CALL_ID, tool_rows,
                              teleport, native_history, parallel_branch_rows)


@pytest.mark.parametrize('agent',['claude','codex'])
def test_native_tools_survive_repeated_round_trips(tmp_path, agent):
    source = write_rows(tmp_path/'source.jsonl',tool_rows(agent,tmp_path))
    original = source.read_bytes()
    expected = native_history(source,agent)
    other = 'codex' if agent=='claude' else 'claude'
    path = source
    for cycle in range(3):
        path = teleport(path, other, tmp_path/f'out-{cycle}')
        history = native_history(path,other)
        encoded = json.dumps(history)
        assert TOOL_OUTPUT in encoded
        if other=='codex':
            assert any(item['type']=='function_call' and item['call_id']==CALL_ID for item in history)
            assert any(item['type']=='function_call_output' and item['call_id']==CALL_ID for item in history)
        else:
            assert any(block['type']=='tool_use' and block['id']==CALL_ID for _,block in history)
            assert any(block['type']=='tool_result' and block['tool_use_id']==CALL_ID for _,block in history)
        path = teleport(path,agent,tmp_path/f'back-{cycle}')
        assert native_history(path,agent)==expected
    assert source.read_bytes()==original


@pytest.mark.parametrize('fault',['orphan','pending','duplicate-call','duplicate-result'])
@pytest.mark.parametrize('agent',['claude','codex'])
def test_incomplete_or_ambiguous_tool_pairs_fail_before_writing(tmp_path, agent, fault):
    rows=tool_rows(agent,tmp_path)
    if agent=='claude':
        calls=rows[1]['message']['content'];results=rows[2]['message']['content']
        if fault=='orphan':calls[:]=[b for b in calls if b.get('id')!=CALL_ID]
        if fault=='pending':results[:]=[b for b in results if b.get('tool_use_id')!=CALL_ID]
        if fault=='duplicate-call':calls.append(calls[-1])
        if fault=='duplicate-result':results.insert(0,results[0])
    else:
        if fault=='orphan':rows=[r for r in rows if r['payload'].get('type')!='function_call']
        if fault=='pending':rows=[r for r in rows if r['payload'].get('type')!='function_call_output']
        if fault=='duplicate-call':rows.insert(3,rows[3])
        if fault=='duplicate-result':rows.insert(5,rows[5])
    source=write_rows(tmp_path/'source.jsonl',rows);home=tmp_path/'destination'
    with pytest.raises(SystemExit):
        teleport(source,'codex' if agent=='claude' else 'claude',home)
    assert not home.exists()


@pytest.mark.parametrize('agent',['claude','codex'])
def test_changed_native_record_wins_over_conversion_metadata(tmp_path, agent):
    other='codex' if agent=='claude' else 'claude'
    source=write_rows(tmp_path/'source.jsonl',tool_rows(other,tmp_path))
    imported=teleport(source,agent,tmp_path/'destination')
    rows=[json.loads(line) for line in imported.read_text().splitlines()]
    if agent=='codex':
        result=next(r for r in rows if r['type']=='response_item' and r['payload'].get('call_id')==CALL_ID and r['payload']['type'].endswith('_output'))
        result['payload']['output']='New native result'
    else:
        result=next(r for r in rows if r['type']=='user' and any(b['type']=='tool_result' for b in r['message']['content']))
        next(b for b in result['message']['content'] if b.get('tool_use_id')==CALL_ID)['content']='New native result'
    write_rows(imported,rows)
    restored=cs.read_portable_session(imported)
    tool=next(m.tool for m in restored.messages if m.tool and m.tool['call_id']==CALL_ID and m.role=='tool')
    assert tool['output']=='New native result'
    assert any('metadata ignored' in note for note in restored.notices)


@pytest.mark.parametrize('tamper',['text','role','output-shape'])
def test_metadata_cannot_contradict_its_native_projection(tmp_path,tamper):
    source=write_rows(tmp_path/'source.jsonl',tool_rows('claude',tmp_path))
    imported=teleport(source,'codex',tmp_path/'destination')
    rows=[json.loads(line) for line in imported.read_text().splitlines()]
    row=next(r for r in rows if r['type']=='response_item')
    if tamper=='text':row['teleporter']['items'][0]['text']='Invisible replacement instruction'
    if tamper=='role':row['teleporter']['items'][0]['role']='developer'
    if tamper=='output-shape':
        row=next(r for r in rows if r['type']=='response_item' and r['payload']['type']=='function_call_output')
        row['teleporter']['items'][0]['tool']['output']=[dict(type='input_image',image_url='private')]
    write_rows(imported,rows)
    with pytest.raises(ValueError,match='metadata|portable|output'):
        cs.read_portable_session(imported)


def test_assistant_first_round_trips_do_not_accumulate_prefaces(tmp_path):
    rows=claude_rows(tmp_path)[1:];rows[0]['parentUuid']=None
    source=write_rows(tmp_path/'source.jsonl',rows)
    expected=cs.read_portable_session(source).messages
    for i,target in enumerate(['codex','claude']*3):
        source=teleport(source,target,tmp_path/f'home-{i}')
        assert cs.read_portable_session(source).messages==expected
        assert json.dumps(native_history(source,target)).count('[Import context]')==1


def test_claude_parallel_result_branches_are_part_of_the_active_exchange(tmp_path):
    source=write_rows(tmp_path/'source.jsonl',parallel_branch_rows(tmp_path))
    imported=teleport(source,'codex',tmp_path/'codex')
    tools=[r for r in native_history(imported,'codex') if r['type']=='function_call_output']
    assert {r['call_id'] for r in tools}=={CALL_ID,'second'}
    returned=teleport(imported,'claude',tmp_path/'returned')
    assert native_history(returned,'claude')==native_history(source,'claude')


@pytest.mark.parametrize('field',['uuid','call_id','result_id'])
def test_malformed_claude_identifiers_fail_cleanly_before_writing(tmp_path,field):
    rows=tool_rows('claude',tmp_path)
    if field=='uuid':rows[1]['uuid']=[]
    if field=='call_id':rows[1]['message']['content'][1]['id']=[]
    if field=='result_id':rows[2]['message']['content'][0]['tool_use_id']=[]
    source=write_rows(tmp_path/'source.jsonl',rows);home=tmp_path/'destination'
    with pytest.raises(SystemExit):
        teleport(source,'codex',home)
    assert not home.exists()


@pytest.mark.parametrize('target',['claude','codex'])
def test_native_tool_history_is_usable_without_conversion_metadata(tmp_path,target):
    source_agent='codex' if target=='claude' else 'claude'
    source=write_rows(tmp_path/'source.jsonl',tool_rows(source_agent,tmp_path))
    imported=teleport(source,target,tmp_path/'destination')
    rows=[json.loads(line) for line in imported.read_text().splitlines()]
    for row in rows:row.pop('teleporter',None)
    write_rows(imported,rows)
    returned=teleport(imported,source_agent,tmp_path/'returned')
    history=native_history(returned,source_agent)
    assert CALL_ID in json.dumps(history) and TOOL_OUTPUT in json.dumps(history)
    if source_agent=='codex':
        assert sum(item['type'].endswith('_call_output') for item in history)==2
    else:
        assert sum(block['type']=='tool_result' for _,block in history)==2
