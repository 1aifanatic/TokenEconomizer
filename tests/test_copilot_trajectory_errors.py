import json
import pytest
from tokeneconomizer.trace import load_trajectory

@pytest.mark.parametrize('error', ['command not found', {'message': 'command not found'}])
@pytest.mark.parametrize('content', ['', 'partial stdout'])
def test_failed_calls_preserve_error_details(tmp_path, error, content):
    events = [
        {'type': 'tool.execution_start', 'data': {'toolCallId': '1', 'toolName': 'bash', 'arguments': {'command': 'missing'}}},
        {'type': 'tool.execution_complete', 'data': {'toolCallId': '1', 'success': False, 'result': {'content': content}, 'error': error}},
    ]
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(map(json.dumps, events)))
    calls = load_trajectory(path)
    assert len(calls) == 1
    assert calls[0][0] == 'bash'
    assert calls[0][3] is False
    assert 'command not found' in calls[0][2]
    if content:
        assert content in calls[0][2]


def test_success_does_not_include_stale_error_field(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.write_text(json.dumps({'type': 'tool.execution_complete', 'data': {'success': True, 'result': {'content': 'ok'}, 'error': 'old'}}))
    assert load_trajectory(path)[0][2] == 'ok'
