"""Trace seam: load_trajectory / tool_usage / parse_catalog over synthetic transcripts."""
import os

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
from tokeneconomizer.vendor import reflect as _reflect

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
    cat = _reflect.parse_tool_catalog_flat(_FLAT_LOG)
    assert set(cat) == {"bash", "view", "edit"}
    assert all(v == "" for v in cat.values())      # names only, no descriptions
    assert _reflect.parse_tool_catalog(_FLAT_LOG) == {}   # schema reader stays schema-only


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
    """The regression that hid the bug: {} must be distinguishable from 'no tools'."""
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
