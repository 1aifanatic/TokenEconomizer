import json
import pytest
from tokeneconomizer.trace import load_trajectory
from tokeneconomizer.trace.claude import claude_session_usage
from tokeneconomizer.trace.codex import codex_rollout_to_exec

NOISE = ['null', '[]', '42', '"diagnostic"', 'not json']

@pytest.mark.parametrize('agent', ['claude', 'codex', 'copilot'])
def test_nonobject_records_do_not_discard_valid_calls(tmp_path, agent):
    if agent == 'claude':
        events = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'c', 'name': 'bash', 'input': {'command': 'echo ok'}}]}}]
    elif agent == 'codex':
        events = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'echo ok', 'exit_code': 0}}]
    else:
        events = [{'type': 'tool.execution_start', 'data': {'toolCallId': 'c', 'toolName': 'bash', 'arguments': {'command': 'echo ok'}}}, {'type': 'tool.execution_complete', 'data': {'toolCallId': 'c', 'success': True}}]
    path = tmp_path / 'trace.jsonl'
    path.write_text('\n'.join(NOISE + list(map(json.dumps, events)) + NOISE))
    calls = load_trajectory(path)
    assert len(calls) == 1
    assert calls[0][1]['command'] == 'echo ok'


def test_rollout_converter_skips_nonobject_records(tmp_path):
    path, output = tmp_path / 'rollout.jsonl', tmp_path / 'out.jsonl'
    event = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'CommandExecution', 'command': ['echo', 'ok'], 'exit_code': 0}}}
    path.write_text('\n'.join(NOISE + [json.dumps(event)]))
    assert codex_rollout_to_exec(str(path), str(output)) == 1
    assert len(load_trajectory(output)) == 1


def test_usage_reader_ignores_nonobject_with_usage_marker(tmp_path):
    path = tmp_path / 'session.jsonl'
    path.write_text('["usage"]\n' + json.dumps({'type': 'assistant', 'message': {'id': 'm', 'usage': {'input_tokens': 5}}}))
    assert claude_session_usage(str(path))['input_tokens'] == 5


def test_copilot_directory_reader_skips_nonobjects():
    from tokeneconomizer.trace.copilot import copilot_session_cwd
    lines = ['["session.start"]', json.dumps({'type': 'session.start', 'data': {'context': {'cwd': '/repo'}}})]
    assert copilot_session_cwd(lines) == '/repo'
