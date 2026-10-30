import json
import pytest
from tokeneconomizer.trace.catalog import parse_tool_catalog

@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('description', ['Run a command', 'Read "quoted" paths', r'Use C:\tools\bin', 'First line\nSecond line', '', '雪のファイル'])
def test_descriptions_are_json_strings_not_trailing_schema(description, wrapped):
    payload = json.dumps({'tools': [{'name': 'bash', 'description': description,
                                    'parameters': {'type': 'object'}}]})
    log = json.dumps({'body': payload}) if wrapped else payload
    assert parse_tool_catalog('DEBUG ' + log) == {'bash': description.split('\n')[0]}


def test_limit_applies_to_decoded_description():
    payload = json.dumps({'name': 'read', 'description': '雪' * 20})
    assert parse_tool_catalog(payload, max_desc=5) == {'read': '雪' * 5}


def test_truncated_description_does_not_swallow_next_log_line():
    log = '{"name":"bash","description":"truncated\n' + json.dumps({'name': 'read', 'description': 'Read file'})
    assert parse_tool_catalog(log) == {'read': 'Read file'}
