import json
from tokeneconomizer.trace.claude import claude_session_usage


def entry(mid, model, tokens=10):
    return {'type': 'assistant', 'message': {'id': mid, 'model': model,
                                           'usage': {'input_tokens': tokens}}}


def test_model_selection_counts_requests_not_content_blocks(tmp_path):
    path = tmp_path / 'session.jsonl'
    events = [entry('a', 'claude-haiku-4.5', n) for n in range(1, 6)]
    events += [entry('b', 'claude-sonnet-4.5'), entry('c', 'claude-sonnet-4.5')]
    path.write_text('\n'.join(map(json.dumps, events)))
    usage = claude_session_usage(str(path))
    assert usage['model'] == 'claude-sonnet-4.5'
    assert usage['requests'] == 3
    assert usage['input_tokens'] == 25


def test_repeated_message_uses_latest_model_metadata(tmp_path):
    path = tmp_path / 'session.jsonl'
    events = [entry('a', 'old-model'), entry('a', 'final-model')]
    path.write_text('\n'.join(map(json.dumps, events)))
    usage = claude_session_usage(str(path))
    assert usage['model'] == 'final-model'
    assert usage['requests'] == 1
