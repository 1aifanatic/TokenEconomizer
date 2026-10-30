import json
import pytest
from tokeneconomizer.trace.claude import claude_subagent_files, claude_session_usage, claude_tool_usage

@pytest.mark.parametrize('folder', ['project[dev]', 'project[ab]', 'project'])
def test_subagents_are_found_under_literal_parent_paths(tmp_path, folder):
    parent = tmp_path / folder
    parent.mkdir()
    transcript = parent / 'session.jsonl'
    transcript.write_text('')
    directory = parent / 'session' / 'subagents'
    directory.mkdir(parents=True)
    child = directory / 'agent.jsonl'
    child.write_text(json.dumps({'type': 'assistant', 'message': {
        'id': 'child-message', 'model': 'claude-sonnet-4',
        'usage': {'input_tokens': 10, 'output_tokens': 2},
        'content': [{'type': 'tool_use', 'id': 'call', 'name': 'Read', 'input': {'path': 'x'}}]
    }}))
    # A similarly named sibling must not be included by glob expansion.
    sibling = tmp_path / 'projecta' / 'session' / 'subagents'
    sibling.mkdir(parents=True)
    (sibling / 'unrelated.jsonl').write_text('{}')
    assert claude_subagent_files(str(transcript)) == [str(child)]
    assert claude_session_usage(str(transcript))['input_tokens'] == 10
    assert claude_tool_usage(str(transcript)) == {'Read': 1}
