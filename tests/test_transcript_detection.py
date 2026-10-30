"""Transcript format detection must use event fields, not JSON substrings."""
import json

import pytest

from tokeneconomizer import trace


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
