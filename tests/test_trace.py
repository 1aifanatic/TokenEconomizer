"""Trace seam: load_trajectory / tool_usage / parse_catalog over synthetic transcripts."""
import os
import json

import pytest

from tokeneconomizer.trace import load_trajectory
from tokeneconomizer.trace.catalog import parse_tool_catalog
from tokeneconomizer.trace.claude import claude_session_usage, claude_subagent_files, claude_tool_usage
from tokeneconomizer.trace.codex import codex_rollout_to_exec

from tokeneconomizer import trace
from fixtures import write_claude_transcript, write_codex_transcript


def test_load_trajectory_claude(tmp_path):
    p = str(tmp_path / "t.jsonl")
    write_claude_transcript(p, [
        ("bash", {"command": "python -m pytest tests/test_x.py"}, "2 passed"),
        ("edit", {"file_path": "pkg/mod.py"}, "ok")])
    traj = trace.load_trajectory(p)
    names = [name for name, _a, _r, _ok in traj]
    assert "bash" in names and "edit" in names


def test_a_claude_request_blob_repeating_the_conversation_adds_no_call(tmp_path):
    """Claude Code 2.1.300 writes `api-request-blob` entries that repeat a request's messages,
    earlier tool calls and results included. Read as calls, each one counted twice."""
    import json
    use = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q"}}
    res = {"type": "tool_result", "tool_use_id": "toolu_1",
           "content": "/bin/bash: line 1: pytest: command not found", "is_error": True}
    lines = [{"type": "assistant", "cwd": "/w/repo", "message": {"role": "assistant",
                                                                "content": [use]}},
             {"type": "user", "message": {"role": "user", "content": [res]}},
             {"type": "api-request-blob", "message": {"role": "assistant", "content": [use]}},
             {"type": "api-request-blob", "message": {"role": "user", "content": [res]}},
             {"type": "assistant", "message": {"role": "assistant", "content": [use]}}]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines))
    calls = trace.load_trajectory(str(p))
    assert [(c[0], c[1]["command"], c[3]) for c in calls] == [("Bash", "pytest -q", False)]
    assert trace.call_cwds(str(p)) == ["/w/repo"]
    assert trace.tool_usage(str(p)) == {"Bash": 1}
    from tokeneconomizer.microskills import distiller
    assert len(distiller.load_calls(str(p))) == 1


def test_tool_usage_counts(tmp_path):
    p = str(tmp_path / "t.jsonl")
    write_claude_transcript(p, [
        ("bash", {"command": "ls"}, ""),
        ("bash", {"command": "pwd"}, ""),
        ("edit", {"file_path": "a.py"}, "")])
    usage = trace.tool_usage(p)
    assert usage.get("bash", 0) >= 2


def test_parse_catalog_codex_default():
    cat = trace.parse_catalog("/nonexistent", "codex")
    assert "shell" in cat and "apply_patch" in cat


def test_parse_catalog_missing_returns_empty(tmp_path):
    # Claude path with no transcript file -> empty catalog, no crash.
    assert trace.parse_catalog(str(tmp_path), "claude") == {}


# ---- Copilot tool-catalog sources: one pipeline across CLI builds -------------------
#
# The CLI has moved the loaded tool catalog twice, and each move made the then-current
# reader return {} -- which untooling cannot tell apart from "no tools loaded", so it
# degraded to a silent no-op for a whole sweep (measured: CLI 1.0.82 dropped 8 of 17 tools
# on 30/31 tasks; 1.0.83, same harness and repo, dropped nothing on 31/31). These tests
# pin all three source formats and the auto-selection between them.
import json as _json

from tokeneconomizer import trace as _trace
from tokeneconomizer.trace import catalog as _catalog

_SCHEMA_LOG = (
    '{"body":"{\\"tools\\":[{\\"name\\":\\"bash\\",\\"title\\":null,'
    '\\"description\\":\\"Run a shell command\\"},'
    '{\\"name\\":\\"view\\",\\"description\\":\\"Read a file\\"}]}"}'
)
_FLAT_LOG = '{\n  "tools": [\n    "bash:66921ccc",\n    "view:0963ceac",\n    "edit:9e640c64"\n  ]\n}'


def _checkpoint_transcript(*names):
    return _json.dumps({
        "type": "session.usage_checkpoint",
        "data": {"promptCacheBreakState": [
            {"models": {"m": {"tools": [{"name": n} for n in names]}}}]},
    })


def test_flat_catalog_parses_name_fingerprint_form():
    """CLI >= 1.0.82 publishes `"tools": ["bash:66921ccc", ...]` and no schemas."""
    cat = _catalog.parse_tool_catalog_flat(_FLAT_LOG)
    assert set(cat) == {"bash", "view", "edit"}
    assert all(v == "" for v in cat.values())      # names only, no descriptions
    assert _catalog.parse_tool_catalog(_FLAT_LOG) == {}   # schema reader stays schema-only


def test_schema_catalog_still_wins_and_keeps_descriptions(tmp_path):
    (tmp_path / "copilot_debug.log").write_text(_SCHEMA_LOG + "\n" + _FLAT_LOG)
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "schema_debug_log"               # richest: it has descriptions
    assert cat["bash"].startswith("Run a shell command")


def test_flat_used_when_schemas_and_checkpoint_are_gone(tmp_path):
    """The 1.0.83 shape: no schemas, no usage_checkpoint -- must NOT parse empty."""
    (tmp_path / "copilot_debug.log").write_text(_FLAT_LOG)
    (tmp_path / "transcript.jsonl").write_text('{"type":"assistant.message","data":{}}')
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "flat_debug_log"
    assert set(cat) == {"bash", "view", "edit"}


def test_usage_checkpoint_source_is_still_read(tmp_path):
    """The 1.0.82 shape must keep working -- supporting the new build is not a swap."""
    (tmp_path / "transcript.jsonl").write_text(_checkpoint_transcript("bash", "view"))
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "usage_checkpoint"
    assert set(cat) == {"bash", "view"}


def test_no_source_reports_none_not_empty_catalog(tmp_path):
    """No source must be distinguishable from an empty catalog: {} is not 'no tools'."""
    (tmp_path / "copilot_debug.log").write_text("nothing useful here")
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert cat == {}
    assert src is None                              # defect signal, not an empty catalog


# ---- Web-Bench pass@1 vs pass@N ------------------------------------------------------
#
# Web-Bench grades each task with up to `retry` attempts, so a task has TWO defensible
# resolve metrics, and they differ materially (measured on prisma: 1-4 tasks per arm).
# Web-Bench's own headline is pass@1. The runner breaks out of the attempt loop on the
# first pass, which makes pass@1 recoverable from older rows that predate the explicit
# fields -- these pin that derivation, since getting it wrong reports pass@N under the
# pass@1 column.
def _p1p2(row):
    """The aggregator's per-row pass@1/pass@N resolution, mirrored for test."""
    p1, p2 = row.get("resolved_p1", ""), row.get("resolved_p2", "")
    att = row.get("webbench_attempts", "")
    if p1 == "" and p2 == "":
        if att != "":
            att = int(att or 0)
            if att == 0:
                return None, None
            p2 = row["resolved"] is True
            p1 = p2 and att == 1
        else:
            p1 = p2 = row["resolved"]
    return p1, p2


def test_explicit_attempt_fields_win():
    assert _p1p2({"resolved": True, "resolved_p1": False,
                  "resolved_p2": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_second_attempt_pass_is_not_pass_at_1():
    """resolved=True after 2 attempts means attempt 1 FAILED."""
    assert _p1p2({"resolved": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_first_attempt_pass():
    assert _p1p2({"resolved": True, "webbench_attempts": 1}) == (True, True)


def test_legacy_row_failed_every_attempt():
    assert _p1p2({"resolved": False, "webbench_attempts": 2}) == (False, False)


def test_unreached_task_counts_in_neither_metric():
    """Never attempted is not a miss -- it must not enter numerator or denominator."""
    assert _p1p2({"resolved": False, "webbench_attempts": 0}) == (None, None)


def test_single_attempt_domain_collapses_to_resolved():
    """SWE-bench rows carry no attempt fields; pass@1 and pass@N are the same number."""
    assert _p1p2({"resolved": True}) == (True, True)
    assert _p1p2({"resolved": False}) == (False, False)


def _jsonl(path, records):
    import json
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def test_a_claude_session_counts_the_prompts_the_person_gave(tmp_path):
    """Typed prompts and slash commands; not tool results, a local command's output, an
    interrupt marker, a compaction summary, meta text, or a subagent's prompts."""
    def user(content, **flags):
        return dict({"type": "user", "message": {"role": "user", "content": content}}, **flags)
    p = _jsonl(tmp_path / "c.jsonl", [
        user("fix the test"),
        user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        user([{"type": "text", "text": "and the docs"}, {"type": "image", "source": {}}]),
        user("<command-name>/tokeneconomizer:status</command-name>"),
        user("<local-command-stdout>done</local-command-stdout>"),
        user("[Request interrupted by user]"),
        user("Caveat: the messages below were generated by the user", isMeta=True),
        user("This session is being continued...", isCompactSummary=True),
        user("subagent task", isSidechain=True),
        {"type": "assistant", "message": {"role": "assistant", "content": "done"}}])
    assert trace.claude_prompt_count(p) == 3


def test_a_codex_session_counts_one_prompt_per_turn(tmp_path):
    turn = [{"type": "turn_context", "payload": {"turn_id": "t"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user"}}]
    assert trace.codex_prompt_count(_jsonl(tmp_path / "r.jsonl", turn * 3)) == 3
    # a rollout without task_started events counts its turn contexts
    older = [r for r in turn if r["type"] != "event_msg"]
    assert trace.codex_prompt_count(_jsonl(tmp_path / "o.jsonl", older * 2)) == 2


def test_a_copilot_session_counts_its_user_messages(tmp_path):
    p = _jsonl(tmp_path / "events.jsonl", [
        {"type": "session.start", "data": {}}, {"type": "user.message", "data": {}},
        {"type": "tool.execution_start", "data": {}}, {"type": "user.message", "data": {}}])
    assert trace.copilot_prompt_count(p) == 2



def test_a_pi_session_counts_the_prompts_on_its_active_branch(tmp_path):
    """Pi keeps a conversation as a tree; a prompt on a branch the person left is not counted."""
    def msg(eid, parent, role):
        return {"type": "message", "id": eid, "parentId": parent, "message": {"role": role}}
    p = _jsonl(tmp_path / "pi.jsonl", [
        {"type": "session", "id": "s"}, msg("a", None, "user"), msg("b", "a", "assistant"),
        msg("x", "b", "user"), msg("y", "x", "assistant"),        # abandoned
        msg("c", "b", "user"), msg("d", "c", "toolResult"), msg("e", "d", "assistant")])
    assert trace.pi_prompt_count(p) == 2


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


_NONOBJECT_RECORDS = ['null', '[]', '42', '"diagnostic"', 'not json']

@pytest.mark.parametrize('agent', ['claude', 'codex', 'copilot'])
def test_nonobject_records_do_not_discard_valid_calls(tmp_path, agent):
    if agent == 'claude':
        events = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'c', 'name': 'bash', 'input': {'command': 'echo ok'}}]}}]
    elif agent == 'codex':
        events = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'echo ok', 'exit_code': 0}}]
    else:
        events = [{'type': 'tool.execution_start', 'data': {'toolCallId': 'c', 'toolName': 'bash', 'arguments': {'command': 'echo ok'}}}, {'type': 'tool.execution_complete', 'data': {'toolCallId': 'c', 'success': True}}]
    path = tmp_path / 'trace.jsonl'
    path.write_text('\n'.join(_NONOBJECT_RECORDS + list(map(json.dumps, events)) + _NONOBJECT_RECORDS))
    calls = load_trajectory(path)
    assert len(calls) == 1
    assert calls[0][1]['command'] == 'echo ok'


def test_rollout_converter_skips_nonobject_records(tmp_path):
    path, output = tmp_path / 'rollout.jsonl', tmp_path / 'out.jsonl'
    event = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'CommandExecution', 'command': ['echo', 'ok'], 'exit_code': 0}}}
    path.write_text('\n'.join(_NONOBJECT_RECORDS + [json.dumps(event)]))
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


def _claude_usage_entry(mid, model, tokens=10):
    return {'type': 'assistant', 'message': {'id': mid, 'model': model,
                                           'usage': {'input_tokens': tokens}}}


def test_model_selection_counts_requests_not_content_blocks(tmp_path):
    path = tmp_path / 'session.jsonl'
    events = [_claude_usage_entry('a', 'claude-haiku-4.5', n) for n in range(1, 6)]
    events += [_claude_usage_entry('b', 'claude-sonnet-4.5'), _claude_usage_entry('c', 'claude-sonnet-4.5')]
    path.write_text('\n'.join(map(json.dumps, events)))
    usage = claude_session_usage(str(path))
    assert usage['model'] == 'claude-sonnet-4.5'
    assert usage['requests'] == 3
    assert usage['input_tokens'] == 25


def test_repeated_message_uses_latest_model_metadata(tmp_path):
    path = tmp_path / 'session.jsonl'
    events = [_claude_usage_entry('a', 'old-model'), _claude_usage_entry('a', 'final-model')]
    path.write_text('\n'.join(map(json.dumps, events)))
    usage = claude_session_usage(str(path))
    assert usage['model'] == 'final-model'
    assert usage['requests'] == 1


@pytest.mark.parametrize("separator", [":", ": ", ":\t"])
def test_codex_detection_accepts_json_whitespace(tmp_path, separator):
    path = tmp_path / "codex.jsonl"
    record = {"type": "item.completed", "item": {"type": "command_execution",
              "command": "pytest -q", "aggregated_output": "1 passed", "exit_code": 0}}
    path.write_text(json.dumps(record, separators=(",", separator)) + "\n")
    assert trace.load_trajectory(str(path)) == [("shell", {"command": "pytest -q"}, "1 passed", True)]
    assert trace.call_cwds(str(path)) == [None]
    assert trace.tool_usage(str(path)) == {"shell": 1}


def test_nested_codex_event_does_not_change_claude_transcript_format(tmp_path):
    path = tmp_path / "claude.jsonl"
    args = {"payload": {"type": "item.completed", "item": {"type": "agent_message"}}}
    records = [
        {"type": "assistant", "cwd": "/repo", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call-1", "name": "send_event", "input": args}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]}},
    ]
    path.write_text("\n".join(json.dumps(record) for record in records))
    assert trace.load_trajectory(str(path)) == [("send_event", args, "ok", True)]
    assert trace.call_cwds(str(path)) == ["/repo"]
    assert trace.tool_usage(str(path)) == {"send_event": 1}


@pytest.mark.parametrize("payload", [{"type": "assistant"}, {"type": "tool_use"}])
def test_nested_claude_data_does_not_change_copilot_format(tmp_path, payload):
    path = tmp_path / "copilot.jsonl"
    records = [
        {"type": "session.start", "data": {"context": {"cwd": "/repo"}}},
        {"type": "tool.execution_start", "data": {"toolCallId": "1", "toolName": "send_event",
                                                     "arguments": {"payload": payload}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "1", "success": True,
                                                        "result": {"content": "ok"}}},
    ]
    path.write_text("\n".join(json.dumps(record) for record in records))
    assert trace.load_trajectory(str(path)) == [("send_event", {"payload": payload}, "ok", True)]
    assert trace.call_cwds(str(path)) == ["/repo"]
    assert trace.tool_usage(str(path)) == {"send_event": 1}
