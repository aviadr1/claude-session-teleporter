"""App-supplied context must not masquerade as the user's typed prompt."""
import json

import pytest
from conftest import cs
from teleport_support import PROMPT, codex_rows, claude_rows, write_rows, teleport, native_history

from teleport_support import BROWSER_PREFIX


def test_codex_browser_context_stays_out_of_claude_prompts_and_round_trips(tmp_path):
    rows=codex_rows(tmp_path)
    request=PROMPT+'\n\n## My request:\nKeep this heading I typed.\n'
    rows[1]['payload']['content'][0]['text']=BROWSER_PREFIX+request
    source=write_rows(tmp_path/'codex.jsonl',rows)
    expected=native_history(source,'codex')
    for cycle in range(2):
        imported=teleport(source,'claude',tmp_path/f'claude-{cycle}')
        history=native_history(imported,'claude')
        assert history[0]==('user',dict(type='text',text=request))
        source=teleport(imported,'codex',tmp_path/f'codex-{cycle}')
        assert native_history(source,'codex')==expected
        events=[json.loads(l)['payload'] for l in source.read_text().splitlines() if json.loads(l)['type']=='event_msg']
        assert next(e['message'] for e in events if e['type']=='user_message')==request


@pytest.mark.parametrize('text',[
    '## My request:\nA heading I wrote.',
    'Discuss this example:\n'+BROWSER_PREFIX+PROMPT,
    '```xml\n'+BROWSER_PREFIX+PROMPT+'\n```',
    BROWSER_PREFIX.replace('ambient-ui-state','user-example')+PROMPT,
    BROWSER_PREFIX.replace('## My request:','## Quoted example:')+PROMPT,
    BROWSER_PREFIX.replace("This block is automatically supplied ambient UI state, not part of the user's request.",'An example I typed.')+PROMPT,
])
def test_ordinary_and_quoted_user_content_is_preserved(tmp_path,text):
    rows=codex_rows(tmp_path);rows[1]['payload']['content'][0]['text']=text
    source=write_rows(tmp_path/'source.jsonl',rows)
    result=teleport(source,'claude',tmp_path/'claude')
    assert native_history(result,'claude')[0]==('user',dict(type='text',text=text))


def test_native_claude_text_is_not_treated_as_codex_injection(tmp_path):
    rows=claude_rows(tmp_path);rows[0]['message']['content'][0]['text']=BROWSER_PREFIX+PROMPT
    source=write_rows(tmp_path/'source.jsonl',rows)
    result=teleport(source,'codex',tmp_path/'codex')
    assert native_history(result,'codex')[0]['content'][0]['text']==BROWSER_PREFIX+PROMPT
    returned=teleport(result,'claude',tmp_path/'returned')
    assert native_history(returned,'claude')[0]==('user',dict(type='text',text=BROWSER_PREFIX+PROMPT))
