"""The live layer's plumbing: repository identity, state, config, session logs, host I/O.

Offline and synthetic. The transcript fixtures reproduce the two properties of real session logs
that the harness's own transcripts never had, and that the live readers exist to handle: Claude
Code writes one entry PER CONTENT BLOCK, each repeating its message's usage, and Codex's rollout
wraps exec-stream item fields in PascalCase `event_msg/item_completed` records.
"""
import json
import os

import pytest

from tokeneconomizer import trace
from te_live import config as live_config
from te_live import hosts, paths, transcripts
from te_live.store import Store


# ---- repository identity ---------------------------------------------------------------
def _git_repo(root, remote=None):
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    cfg = "[core]\n\trepositoryformatversion = 0\n"
    if remote:
        cfg += f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    with open(os.path.join(root, ".git", "config"), "w") as fh:
        fh.write(cfg)
    return root


def test_repo_identity_follows_the_remote_not_the_spelling(tmp_path):
    a = _git_repo(str(tmp_path / "a"), "git@github.com:Owner/Repo.git")
    b = _git_repo(str(tmp_path / "b" / "repo"), "https://github.com/owner/repo")
    ra, rb = paths.repo_identity(a), paths.repo_identity(b)
    assert ra.remote == rb.remote == "github.com/owner/repo"
    assert ra.label == "owner/repo"
    # the key carries the checkout's name for readability, and the remote for identity
    assert ra.key.endswith(rb.key.split("-")[-1])


def test_a_subdirectory_and_a_worktree_share_their_repository(tmp_path):
    main = _git_repo(str(tmp_path / "main"), "https://github.com/o/r.git")
    os.makedirs(os.path.join(main, "src", "pkg"))
    wt_git = os.path.join(main, ".git", "worktrees", "feature")
    os.makedirs(wt_git)
    with open(os.path.join(wt_git, "commondir"), "w") as fh:
        fh.write("../..")
    wt = str(tmp_path / "feature")
    os.makedirs(wt)
    with open(os.path.join(wt, ".git"), "w") as fh:
        fh.write(f"gitdir: {wt_git}\n")
    sub = paths.repo_identity(os.path.join(main, "src", "pkg"))
    assert sub.root == main
    assert paths.repo_identity(wt).remote == sub.remote == "github.com/o/r"


def test_a_directory_without_git_is_keyed_by_its_path(tmp_path, monkeypatch):
    d = str(tmp_path / "scratch")
    os.makedirs(d)
    # Fence the walk at tmp_path: a machine may well have a stray .git in /tmp itself.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    r = paths.repo_identity(d)
    assert r.remote == "" and r.label == "scratch" and r.root == d


def test_state_root_honours_the_environment(tmp_path):
    assert paths.state_root({"TOKENECONOMIZER_HOME": str(tmp_path)}) == str(tmp_path)


# ---- store and config ----------------------------------------------------------------------
def test_store_writes_atomically_and_logs(tmp_path):
    s = Store(str(tmp_path / "st"))
    s.write("a/b.json", {"x": 1})
    assert s.read("a/b.json") == {"x": 1}
    assert s.read("missing.json", default={}) == {}
    s.append("log.jsonl", {"n": 1})
    s.append("log.jsonl", {"n": 2})
    assert [r["n"] for r in s.read_lines("log.jsonl")] == [1, 2]
    assert [r["n"] for r in s.read_lines("log.jsonl", limit=1)] == [2]
    with s.locked("x") as got_x:
        with s.locked("y") as got_y:              # distinct locks never deadlock
            s.write("c.json", [1])
        with s.locked("x", timeout=0.1) as again:  # held: a hook's caller still proceeds,
            assert again is False                  # and is told it is unlocked
    assert got_x and got_y
    assert not [f for f in os.listdir(s.base) if f.endswith(".tmp")]


def test_config_layers_and_never_raises_on_bad_values(tmp_path):
    root = str(tmp_path)
    repo_dir = os.path.join(root, "repos", "r")
    os.makedirs(repo_dir)
    with open(os.path.join(root, "config.json"), "w") as fh:
        json.dump({"spiral": "guard", "untool_evidence": 7, "bogus": 1}, fh)
    with open(os.path.join(repo_dir, "config.json"), "w") as fh:
        json.dump({"spiral": "nope", "inject": "envfacts+distiller"}, fh)
    cfg = live_config.load_config(root, repo_dir=repo_dir, env={"TE_UNTOOL": "auto"})
    assert cfg.spiral == "guard"                  # the invalid repo value is ignored
    assert cfg.untool_evidence == 7 and cfg.untool == "auto"
    assert cfg.inject == "envfacts+distiller" and cfg.inject_effective == "envfacts"
    joined = " ".join(cfg.warnings)
    assert "bogus" in joined and "nope" in joined and "paid=false" in joined
    assert live_config.load_config(root, env={"TE_DISABLE": "1"}).enabled is False


def test_write_config_validates_before_writing(tmp_path):
    path = str(tmp_path / "config.json")
    live_config.write_config(path, {"spiral": "off", "paid": "true"})
    assert json.load(open(path)) == {"spiral": "off", "paid": True}
    with pytest.raises(ValueError):
        live_config.write_config(path, {"spiral": "sometimes"})
    assert json.load(open(path))["spiral"] == "off"


# ---- Claude Code transcripts -----------------------------------------------------------------
def _claude_lines(path, blocks_per_message=3, n_messages=2, model="claude-opus-5-5"):
    lines = []
    tag = os.path.splitext(os.path.basename(path))[0]     # message ids are unique per request
    for m in range(n_messages):
        usage = {"input_tokens": 10, "cache_read_input_tokens": 1000,
                 "cache_creation_input_tokens": 100, "output_tokens": 50}
        for b in range(blocks_per_message):     # one entry per content block, usage repeated
            lines.append({"type": "assistant", "message": {
                "id": f"msg_{tag}_{m}", "model": model, "role": "assistant", "usage": usage,
                "content": [{"type": "text", "text": f"block {b}"}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_claude_usage_counts_each_message_once_and_includes_subagents(tmp_path):
    main = _claude_lines(str(tmp_path / "sess.jsonl"))
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    _claude_lines(str(sub_dir / "agent-1.jsonl"), blocks_per_message=2, n_messages=1)
    u = transcripts.claude_session_usage(main)
    assert u["requests"] == 3                     # 2 main messages + 1 subagent message
    assert u["cache_read_input_tokens"] == 3000   # not 3 x 2 x 1000 + 2 x 1000
    assert u["model"] == "claude-opus-5-5" and u["files"] == 2
    assert transcripts.claude_session_usage(main, include_subagents=False)["requests"] == 2


def test_deferred_tools_are_read_from_attachments(tmp_path):
    p = str(tmp_path / "t.jsonl")
    with open(p, "w") as fh:
        fh.write(json.dumps({"type": "attachment", "attachment": {
            "type": "deferred_tools_delta", "addedNames": ["CronCreate", "WebSearch"],
            "removedNames": []}}) + "\n")
        fh.write(json.dumps({"type": "attachment", "attachment": {
            "type": "deferred_tools_delta", "addedNames": [], "removedNames": ["WebSearch"]}}) + "\n")
    assert transcripts.claude_deferred_tools(p) == {"CronCreate"}


# ---- Codex rollouts ---------------------------------------------------------------------------
def _rollout(path):
    recs = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": "/work/r",
                                             "cli_version": "0.153.1"}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6-astra"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "CommandExecution", "id": "c1", "command": ["/bin/bash", "-lc", "pytest -q"],
            "aggregated_output": "1 failed", "exit_code": 1, "status": "completed"}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "FileChange", "id": "f1", "changes": {"/work/r/pkg/a.py": {"type": "update"}},
            "status": "completed"}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "Reasoning", "id": "r1"}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1000, "cached_input_tokens": 600, "cache_write_input_tokens": 100,
            "output_tokens": 40, "reasoning_output_tokens": 10}}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 3000, "cached_input_tokens": 2000, "cache_write_input_tokens": 200,
            "output_tokens": 90, "reasoning_output_tokens": 30}}}},
    ]
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def test_codex_rollout_reads_through_the_existing_trace_seam(tmp_path):
    src = _rollout(str(tmp_path / "rollout-x.jsonl"))
    out = str(tmp_path / "exec.jsonl")
    assert transcripts.codex_rollout_to_exec(src, out) == 2       # reasoning is not a tool call
    traj = trace.load_trajectory(out)
    assert traj[0][0] == "shell" and traj[0][1]["command"] == "/bin/bash -lc 'pytest -q'"
    assert traj[0][2] == "1 failed" and traj[0][3] is False
    assert traj[1][0] == "apply_patch" and traj[1][1]["path"] == "/work/r/pkg/a.py"


def test_codex_usage_is_converted_as_the_harness_converts_it(tmp_path):
    u = transcripts.codex_session_usage(_rollout(str(tmp_path / "rollout-x.jsonl")))
    # last cumulative total; fresh input excludes both cache fields; reasoning joins output
    assert u["input_tokens"] == 3000 - 2000 - 200
    assert u["cache_read_input_tokens"] == 2000 and u["cache_creation_input_tokens"] == 200
    assert u["output_tokens"] == 90 + 30 and u["model"] == "gpt-6-astra"


def test_codex_capability_evidence_includes_the_models_own_exec_calls(tmp_path):
    """In code mode every tool call is one `exec` custom tool; `tools.view_image(` inside its
    input is image use, and only the capability trace carries it."""
    from tokeneconomizer.mechanisms import codex_untool as cu
    src = _rollout(str(tmp_path / "rollout-x.jsonl"))
    with open(src, "a") as fh:
        fh.write(json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "call_9",
            "status": "completed",
            "input": "const r = await tools.view_image({path: 'a.png'});"}}) + "\n")
    out = str(tmp_path / "cap.jsonl")
    assert transcripts.codex_capability_trace(src, out) == 3
    usage = cu.inspect_usage(out)
    assert usage["completed"]["image_media"] == 1 and usage["completed"]["collaboration"] == 0
    plain = str(tmp_path / "exec.jsonl")
    transcripts.codex_rollout_to_exec(src, plain)
    assert cu.inspect_usage(plain)["completed"]["image_media"] == 0


def test_a_compressed_rollout_is_reported_unreadable(tmp_path):
    p = str(tmp_path / "rollout-x.jsonl.zst")
    open(p, "wb").write(b"\x28\xb5\x2f\xfd")
    assert transcripts.codex_rollout_to_exec(p, str(tmp_path / "o.jsonl")) is None


# ---- hosts ---------------------------------------------------------------------------------
def test_host_detection():
    assert hosts.detect({"turn_id": "t"}).name == "codex"
    assert hosts.detect({"transcript_path": "/h/.codex/sessions/2026/rollout-a.jsonl"}).name == "codex"
    assert hosts.detect({"transcript_path": "/h/.claude/projects/x/s.jsonl"}).name == "claude"
    assert hosts.detect({"turn_id": "t"}, hint="claude").name == "claude"
    assert hosts.detect({"transcript_path": "/h/.copilot/session-state/s/events.jsonl"}
                        ).name == "copilot"
    # Copilot sends no transcript before Stop, and marks its hook processes; a Claude Code
    # session started from inside Copilot inherits the marker but always sends a transcript.
    copilot_env = {"COPILOT_PLUGIN_ROOT": "/p", "COPILOT_CLI": "1"}
    assert hosts.detect({"session_id": "s"}, copilot_env).name == "copilot"
    assert hosts.detect({"session_id": "s", "transcript_path": "/h/.claude/projects/x/s.jsonl"},
                        copilot_env).name == "claude"
    assert hosts.detect({"session_id": "s"}, {}).name == "claude"


def test_the_te_command_defaults_to_the_agent_whose_shell_runs_it():
    assert hosts.default_host({}) == "claude"
    assert hosts.default_host({"COPILOT_CLI": "1"}) == "copilot"
    assert hosts.default_host({"COPILOT_CLI": "1", "CLAUDECODE": "1"}) == "claude"
    assert hosts.default_host({"TE_HOST": "codex", "CLAUDECODE": "1"}) == "codex"
    assert hosts.default_host({"TE_HOST": "nonsense"}) == "claude"


# Payloads copilot 1.0.83 sent to a Claude-format plugin hook (PascalCase events), verbatim in
# shape, captured against a scripted local model endpoint on 2026-10-07.
COPILOT_POST = {"hook_event_name": "PostToolUse", "session_id": "fcb5fc6b",
                "timestamp": "2026-10-08T04:27:38.418Z", "cwd": "/w/repo", "tool_name": "Bash",
                "tool_input": {"command": "python3 -c 'import sys; sys.exit(3)'",
                               "description": "run it"},
                "tool_result": {"result_type": "success",
                                "text_result_for_llm": "boom\n<shellId: 0 completed with exit code 3>"}}
COPILOT_FAILURE = {"hook_event_name": "PostToolUseFailure", "session_id": "10bbc978",
                   "timestamp": "2026-10-08T04:27:51.286Z", "cwd": "/w/repo", "tool_name": "Read",
                   "tool_input": {"path": "/w/repo/nope.py"}, "error": "Path does not exist"}


def test_the_copilot_payloads_observed_live_are_read():
    c = hosts.detect(COPILOT_POST, {"COPILOT_PLUGIN_ROOT": "/p"})
    assert c.name == "copilot" and not c.can_stop_mid_turn and c.stop("why") is None
    name, args, text, code = c.tool_call(COPILOT_POST)
    assert (name, args["description"], code) == ("Bash", "run it", 3)   # exit code from the text
    assert text.startswith("boom")
    name, args, text, code = c.tool_call(COPILOT_FAILURE)
    assert (name, args["path"], code) == ("Read", "/w/repo/nope.py", None)
    assert "Path does not exist" in text
    # Copilot reads a TOP-LEVEL additionalContext and ignores hookSpecificOutput, and shows a
    # notice only as a progress line ahead of the output (both verified in its TUI)
    assert c.context("SessionStart", "x") == {"additionalContext": "x"}
    m = hosts.merge_outputs(c.context("SessionStart", "a"), c.notice("n"),
                            c.context("SessionStart", "b"))
    assert m == {"additionalContext": "a\n\nb", hosts.PROGRESS: ["n"]}


def test_a_copilot_session_log_is_found_from_the_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cp"))
    c = hosts.Copilot()
    assert c.transcript({"session_id": "abc"}) == str(
        tmp_path / "cp" / "session-state" / "abc" / "events.jsonl")
    assert c.transcript({"session_id": "abc", "transcript_path": "/x/events.jsonl"}) == \
        "/x/events.jsonl"
    assert c.cwd({}, {"COPILOT_PROJECT_DIR": "/w/repo"}) == "/w/repo"


def test_claude_tool_results_and_outputs():
    h = hosts.ClaudeCode()
    name, args, text, code = h.tool_call({
        "tool_name": "Bash", "tool_input": {"command": "pytest"},
        "tool_response": {"stdout": "1 failed", "stderr": "Traceback", "interrupted": False}})
    assert (name, args["command"], code) == ("Bash", "pytest", None)   # no exit code (verified)
    assert "1 failed" in text and "Traceback" in text
    assert h.context("PostToolUse", "x") == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x"}}
    assert h.stop("why") == {"continue": False, "stopReason": "why"}
    # the hook environment of a `claude -p` session, as observed on 2.1.285
    assert h.headless({"CLAUDE_CODE_SESSION_ATTENDED": "0", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert not h.headless({"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert h.headless({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) and not h.headless({})


def test_the_codex_posttooluse_payload_observed_live_is_read():
    """The payload codex-cli 0.153.1 sent for `echo one` (gpt-5.6-sol, 2026-09-30), verbatim in
    shape: tool_name "Bash", a string command, a plain-string result, turn_id and model."""
    payload = {"session_id": "01a0f5e1", "turn_id": "t1", "model": "gpt-5.6-sol",
               "permission_mode": "default", "hook_event_name": "PostToolUse",
               "cwd": "/w/repo", "transcript_path": "/h/.codex/sessions/2026/09/30/rollout-x.jsonl",
               "tool_name": "Bash", "tool_use_id": "call_1", "tool_input": {"command": "echo one"},
               "tool_response": "one\n"}
    host = hosts.detect(payload)
    assert host.name == "codex"
    assert host.tool_call(payload) == ("bash", {"command": "echo one"}, "one\n", None)


def test_codex_tool_calls_normalize_and_never_stop():
    c = hosts.Codex()
    assert c.stop("why") is None and not c.can_stop_mid_turn
    name, args, text, code = c.tool_call({
        "tool_name": "exec_command", "tool_input": {"command": ["bash", "-lc", "go test ./..."]},
        "tool_response": {"output": "FAIL", "exit_code": 1}})
    assert (name, args["command"], code) == ("bash", "bash -lc 'go test ./...'", 1)
    name, args, _, _ = c.tool_call({"tool_name": "apply_patch", "tool_input": {
        "input": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch"}})
    assert name == "apply_patch" and args["paths"] == ["src/a.py"]
    _, _, _, code = c.tool_call({"tool_name": "shell", "tool_input": {"command": "ls"},
                                 "tool_response": "Exit code: 2\nWall time: 0.1s\nOutput:\n"})
    assert code == 2


def test_merge_outputs_concatenates_contexts_and_notices():
    h = hosts.ClaudeCode()
    m = hosts.merge_outputs(h.context("SessionStart", "a"), h.notice("n1"),
                            h.context("SessionStart", "b"), h.notice("n2"), None, {})
    assert m["hookSpecificOutput"]["additionalContext"] == "a\n\nb"
    assert m["systemMessage"] == "n1\nn2"


# ---- Pi sessions -------------------------------------------------------------------------
def _pi_session(path, cwd="/w/repo", sid="pi-1", abandoned=False):
    """A Pi v3 session tree: header, a user prompt, a bash call that fails, an edit, a bash
    call that passes -- and, optionally, an abandoned branch hanging off the first prompt."""
    lines = [{"type": "session", "version": 3, "id": sid, "timestamp": "2026-09-30T00:00:00Z",
              "cwd": cwd}]
    state = {"parent": None, "n": 0}

    def add(entry, parent="__chain__"):
        state["n"] += 1
        eid = f"e{state['n']:03d}"
        entry.update(id=eid, timestamp="2026-09-30T00:00:01Z",
                     parentId=state["parent"] if parent == "__chain__" else parent)
        lines.append(entry)
        if parent == "__chain__":
            state["parent"] = eid
        return eid

    usage = {"input": 10, "output": 50, "cacheRead": 1000, "cacheWrite": 100, "reasoning": 5,
             "totalTokens": 1160, "cost": {"total": 0.002}}

    def call(name, args, text, err):
        tc = f"call{state['n']}"
        add({"type": "message", "message": {"role": "assistant", "provider": "openai",
             "model": "gpt-5.6-sol", "usage": usage,
             "content": [{"type": "toolCall", "id": tc, "name": name, "arguments": args}]}})
        add({"type": "message", "message": {"role": "toolResult", "toolCallId": tc,
             "toolName": name, "isError": err, "content": [{"type": "text", "text": text}]}})

    first = add({"type": "message", "message": {"role": "user", "content": "fix the bug"}})
    call("bash", {"command": "pytest -q"}, "bash: pytest: command not found", True)
    call("edit", {"path": "pkg/a.py", "oldText": "x", "newText": "y"}, "ok", False)
    call("bash", {"command": "python -m pytest -q"}, "1 passed", False)
    if abandoned:                        # a branch the user walked away from, earlier in the file
        lines.insert(3, {"type": "message", "id": "zzz", "parentId": first,
                         "timestamp": "2026-09-30T00:00:02Z", "message": {
                             "role": "assistant", "model": "gpt-5.6-sol", "usage": usage,
                             "content": [{"type": "toolCall", "id": "dead", "name": "bash",
                                          "arguments": {"command": "rm -rf build"}}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_a_pi_session_reads_through_the_trace_seam_on_its_active_branch(tmp_path):
    src = _pi_session(str(tmp_path / "s.jsonl"), abandoned=True)
    out = str(tmp_path / "trace.jsonl")
    assert transcripts.pi_session_to_claude(src, out) == 3
    traj = trace.load_trajectory(out)
    assert [(n, a.get("command") or a.get("path"), ok) for n, a, _, ok in traj] == [
        ("bash", "pytest -q", False), ("edit", "pkg/a.py", True), ("bash", "python -m pytest -q", True)]
    assert "rm -rf build" not in open(out).read()          # the abandoned branch is not read


def test_pi_usage_maps_directly_and_keeps_pis_own_cost(tmp_path):
    u = transcripts.pi_session_usage(_pi_session(str(tmp_path / "s.jsonl"), abandoned=True))
    # three assistant messages on the active branch; output already includes reasoning
    assert u["requests"] == 3
    assert (u["input_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"],
            u["output_tokens"]) == (30, 3000, 300, 150)
    assert u["cost_usd_vendor"] == pytest.approx(0.006) and u["model"] == "gpt-5.6-sol"
    assert transcripts.pi_session_header(str(tmp_path / "s.jsonl"))["cwd"] == "/w/repo"


def test_the_pi_host_reads_what_the_extension_sends():
    h = hosts.HOSTS["pi"]()
    assert h.can_stop_mid_turn and h.stop("why") == {"continue": False, "stopReason": "why"}
    name, args, text, code = h.tool_call({"tool_name": "bash", "tool_input": {"command": "go test"},
                                          "tool_response": {"content": "FAIL\nCommand exited with code 1",
                                                            "isError": True, "exit_code": 1}})
    assert (name, args["command"], code) == ("bash", "go test", 1) and "FAIL" in text
    assert h.headless({"TE_PI_MODE": "print"}) and not h.headless({"TE_PI_MODE": "tui"})
    assert hosts.detect({"turn_id": "t"}, hint="pi").name == "pi"


# ---- the guard sees edits made through the shell ---------------------------------------------
from te_live import guard  # noqa: E402


@pytest.mark.parametrize("cmd,want", [
    ("sed -i 's/a/b/' pkg/mod.py", ["mod.py"]),
    ("cd /t && sed -i.bak -e 's/a/b/' -e 's/c/d/' x.py y.py && pytest -q", ["x.py", "y.py"]),
    ("sed -i '' 's/a/b/' x.py", ["x.py"]),                         # BSD sed's empty suffix
    ("perl -pi -e 's/foo/bar/' src/a.rs", ["a.rs"]),
    ("cat > repro.py <<'EOF'\nprint(1 > 0)\nEOF\npython repro.py", ["repro.py"]),
    ("echo 'x' | tee -a setup.cfg", ["setup.cfg"]),
    # how Sonnet 5.5 edits: an inline script that reads the file and writes it back
    ("python - <<'E'\np='sympy/printing/str.py'; s=open(p).read()\nopen(p,'w').write(s)\nE",
     ["str.py"]),
    ("python - <<'E'\nfor p in ['x/a.py', 'x/b.py']:\n    open(p, 'w').write('')\nE",
     ["a.py", "b.py"]),
    ("python - <<'E'\nfrom pathlib import Path\nPath('m/n.py').write_text('x')\nE", ["n.py"]),
    ("git apply /tmp/fix.diff", ["(patch)"]),
    ("git checkout -- a.py b/c.py", ["a.py", "c.py"]),
    # not edits: output captures, scratch, reads, and operators inside quotes
    ("python -m pytest -q > /tmp/out.txt 2>&1; tail -5 /tmp/out.txt", []),
    ("grep -n 'x' a.py > hits.txt", []),
    ("grep -n '>' x.py", []),
    ("grep -nE 'a|b' x.py | head", []),
    ("python -c \"print(1>2)\"", []),
    ("git stash -q; python bin/test x; git stash pop -q", []),
    ("sed -n '1,20p' pkg/mod.py", []),
    # a quoted `|` in a sed script must not split the command (seen live: sympy-13091)
    ("sed -i -E 's/x (<|<=|>|>=) y/z/' numbers.py expr.py", ["numbers.py", "expr.py"]),
    ("sed -i 's/a/b/' x.py 2>&1 | tail -2", ["x.py"]),
])
def test_edits_made_through_the_shell_are_recognised(cmd, want):
    assert guard.shell_edit_targets(cmd) == want


def test_shell_edits_reach_the_state_the_judge_reads_and_the_ended_green_proxy():
    g = guard.TurnGuard(turn_key="t")
    edit = "python - <<'E'\np='pkg/mod.py'; s=open(p).read()\nopen(p,'w').write(s[::-1])\nE"
    for _ in range(2):
        g.fold("Bash", {"command": edit}, "")
    assert g.st.edit_churn == 2 and g.whole.distinct_edit_files == 1 and g.edits == 2
    digest = g.st.recent_digest()
    assert "edit_churn=2" in digest and "[edits mod.py]" in digest
    # one command that edits and then fails its tests: the turn did not end green
    g.fold("Bash", {"command": "sed -i 's/b/c/' pkg/mod.py && pytest -q"},
           "FAILED tests/test_m.py::t - AssertionError")
    assert g.edits == 3 and g.last_verify > g.last_edit and not g.resolved_proxy()


def test_the_judge_is_told_how_the_last_verification_went_not_that_tests_are_red():
    g = guard.TurnGuard(turn_key="t")
    vendored = "turns=3\nrepo_context: ... = 1.2x that typical length. Tests are still NOT green. A"
    assert "No test or verification run has been seen" in g.live_digest(vendored)
    g.fold("Bash", {"command": "python bin/test sympy/core"}, "tests finished: 10 passed")
    told = g.live_digest(vendored)
    assert "The last verification run passed." in told and "NOT green" not in told
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "a", "new_string": "b"}, "ok")
    assert g.live_digest(vendored).count("There have been edits since.") == 1


class _BackgroundJudge:
    """`te_live.judge.AsyncJudge`'s interface, with the verdict handed in by the test."""

    def __init__(self):
        self.started, self.verdict = [], None

    def start(self, g, digest):
        self.started.append(digest)
        return f"job{len(self.started)}"

    def poll(self, g):
        pending = g.judge_pending or {}
        if self.verdict is None or pending.get("id") != f"job{len(self.started)}":
            return False, None
        v, self.verdict = self.verdict, None
        return True, v


def _spin(g, judge, n, envelope=6.0):
    """An edit and an identical failing test run, alternately: a loop the gate opens on."""
    out = []
    for k in range(n):
        call = (("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
                if k % 2 == 0 else
                ("Bash", {"command": "pytest -q"}, "FAILED tests/test_a.py::t - AssertionError"))
        out.append(g.fold(*call, envelope=envelope, judge=judge))
    return out


def test_a_background_judge_starts_once_and_its_verdict_nudges_a_later_call():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    assert _spin(g, j, 10) == [None] * 10            # nothing is decided while it is out
    assert len(j.started) == 1                       # and no second job is started
    assert "NOT green" not in j.started[0]
    j.verdict = {"stuck": True, "confidence": 0.9, "reason": "loop",
                 "_nudge": "Read the failing assertion before editing again."}
    assert _spin(g, j, 1)[0] == ("nudge", "judge-stuck@0.90:loop")
    assert g.nudge_body(6.0) == "Read the failing assertion before editing again."
    assert g.nudge_body(6.0) != "Read the failing assertion before editing again."  # used once


def test_a_productive_verdict_backs_the_judge_off():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    _spin(g, j, 7)
    j.verdict = {"stuck": False, "confidence": 0.8, "reason": "progress"}
    assert _spin(g, j, 1) == [None]
    assert g.judge_gap == g.JUDGE_BASE_GAP * 2 and g.judge_pending is None


def test_a_job_that_never_answers_stops_blocking_the_next_one():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    _spin(g, j, 7)
    assert len(j.started) == 1 and g.judge_pending
    g.judge_pending["since"] = 0                     # its process died
    _spin(g, j, g.judge_gap)                         # well before the backstop at 2.5x
    assert len(j.started) == 2


# ---- Copilot session logs ------------------------------------------------------------------
def copilot_events(path, calls, *, shutdown=True, model="gpt-5.4", cwd="/w/repo", sid="cp1"):
    """A Copilot `session-state/<id>/events.jsonl`: session.start, one user message, the tool
    calls as execution_start/complete pairs, and (unless killed) a session.shutdown."""
    ev = [{"type": "session.start", "data": {"sessionId": sid, "copilotVersion": "1.0.83",
                                            "selectedModel": model, "context": {"cwd": cwd}}},
          {"type": "user.message", "data": {"content": "fix it", "turnId": "0"}}]
    for n, (name, args, text, ok) in enumerate(calls):
        ev.append({"type": "tool.execution_start", "data": {
            "toolCallId": f"c{n}", "toolName": name, "arguments": args, "turnId": "0"}})
        ev.append({"type": "tool.execution_complete", "data": {
            "toolCallId": f"c{n}", "success": ok, "turnId": "0",
            "result": {"content": text, "detailedContent": text}}})
    if shutdown:
        ev.append({"type": "session.shutdown", "data": {
            "modelMetrics": {model: {"requests": {"count": 4}, "usage": {
                "inputTokens": 4000, "outputTokens": 80, "cacheReadTokens": 2400,
                "cacheWriteTokens": 100, "reasoningTokens": 0}}},
            "toolDefinitionsTokens": 13149}})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(e) for e in ev) + "\n")
    return path


def test_copilot_logs_read_through_the_existing_trace_seam(tmp_path):
    p = copilot_events(str(tmp_path / "s" / "events.jsonl"), [
        ("bash", {"command": "pytest -q"}, "1 failed\n<shellId: 0 completed with exit code 1>",
         True),
        ("view", {"path": "/w/repo/a.py"}, "x = 1", True)])
    traj = trace.load_trajectory(p)
    assert [t[0] for t in traj] == ["bash", "view"] and traj[0][1]["command"] == "pytest -q"
    assert trace.tool_usage(p) == {"bash": 1, "view": 1}
    (turn,) = transcripts.copilot_turns(p)
    assert turn[0][0] == "bash" and turn[0][3] == 1 and turn[1][3] is None
    assert transcripts.copilot_session_meta(p)["cwd"] == "/w/repo"


def test_copilot_usage_is_converted_from_its_grand_input_total(tmp_path):
    p = copilot_events(str(tmp_path / "s" / "events.jsonl"), [])
    u = transcripts.copilot_session_usage(p)
    # inputTokens counts cache reads and writes (verified against a scripted endpoint)
    assert u["input_tokens"] == 4000 - 2400 - 100
    assert u["cache_read_input_tokens"] == 2400 and u["cache_creation_input_tokens"] == 100
    assert u["output_tokens"] == 80 and u["requests"] == 4 and u["model"] == "gpt-5.4"
    assert u["tool_definitions_tokens"] == 13149
    killed = transcripts.copilot_session_usage(
        copilot_events(str(tmp_path / "k" / "events.jsonl"), [], shutdown=False))
    assert killed["input_tokens"] is None and killed["model"] == "gpt-5.4"


def test_copilot_tools_are_grouped_by_mcp_server(tmp_path):
    servers = {"github-mcp-server", "github", "acme-db"}
    assert transcripts.mcp_server_of("github-mcp-server-list_issues", servers) == \
        "github-mcp-server"                                  # the longest prefix wins
    assert transcripts.mcp_server_of("acme-db-query", servers) == "acme-db"
    assert transcripts.mcp_server_of("bash", servers) is None
    home = tmp_path / "cp"
    home.mkdir()
    (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"acme-db": {}}}))
    assert transcripts.copilot_mcp_servers({"COPILOT_HOME": str(home)}) == {
        "github-mcp-server", "acme-db"}


def test_a_copilot_side_call_is_a_bare_completion_priced_from_its_own_log(tmp_path,
                                                                          monkeypatch):
    import subprocess
    from te_live import sidecall
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cp"))
    copilot_events(str(tmp_path / "cp" / "session-state" / "side1" / "events.jsonl"), [])
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        out = "\n".join(json.dumps(e) for e in (
            {"type": "assistant.message", "data": {"content": "- use python -m pytest"}},
            {"type": "result", "sessionId": "side1", "exitCode": 0}))
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.copilot_call("propose facts")
    assert text == "- use python -m pytest" and eff and eff > 0
    cmd = seen["cmd"]
    assert cmd[:3] == ["copilot", "-p", "propose facts"] and "--disable-builtin-mcps" in cmd
    assert cmd.count("--excluded-tools") == len(sidecall.COPILOT_TOOLS)
    assert seen["env"]["TE_SIDECALL"] == "1"            # its own hooks must not recurse
    cfg = live_config.LiveConfig(paid=True)
    assert sidecall.runner_for("copilot", cfg).__code__.co_names.count("copilot_call") == 1


def test_a_copilot_notice_is_a_progress_line_before_the_hook_output():
    import io
    from te_live import hook
    out = io.StringIO()
    hook.main([], io.StringIO("{}"), out, {"TE_DISABLE": "1"})
    assert out.getvalue() == "{}\n"
    # what hook.main writes for an output carrying a Copilot notice
    from unittest import mock
    with mock.patch.object(hook, "run", return_value=dict(
            hosts.Copilot().notice("hello"), additionalContext="ctx")):
        out = io.StringIO()
        hook.main([], io.StringIO('{"session_id": "s"}'), out, {})
    first, last = out.getvalue().splitlines()
    assert json.loads(first) == {"type": "progress", "message": "hello"}
    assert json.loads(last) == {"additionalContext": "ctx"}


def test_copilot_side_calls_name_a_model_a_byok_provider_can_serve():
    from te_live.sidecall import copilot_model
    byok = {"COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
            "COPILOT_PROVIDER_TYPE": "anthropic"}
    # `auto` is the subscription's router; a BYOK provider has none, and the call answered nothing
    assert copilot_model("", dict(byok, COPILOT_MODEL="auto")) == "claude-haiku-4-5"
    assert copilot_model("", dict(byok, COPILOT_MODEL="claude-sonnet-5")) == "claude-sonnet-5"
    assert copilot_model("claude-opus-5", byok) == "claude-opus-5"           # paid_model wins
    assert copilot_model("", {"COPILOT_MODEL": "auto"}) == ""                # subscription
    assert copilot_model("", {"COPILOT_PROVIDER_BASE_URL": "http://localhost:11434/v1"}) == ""
    # OpenRouter wants its own ids, whichever wire Copilot speaks to it
    for wire in ("anthropic", "openai"):
        assert copilot_model("", {"COPILOT_PROVIDER_BASE_URL": "https://openrouter.ai/api",
                                  "COPILOT_PROVIDER_TYPE": wire}) == "anthropic/claude-haiku-4.5"


def test_the_probe_repository_carries_its_trap(tmp_path):
    import subprocess
    from te_live import probe
    repo, trap = probe.make_repo(str(tmp_path))
    bare = subprocess.run([os.path.join(trap, "pytest"), "-q"], cwd=repo, capture_output=True,
                          text=True)
    assert bare.returncode == 127 and "command not found" in bare.stderr
    # pytest is "not installed" for any interpreter the session runs, whatever really is
    for py in ("python", "python3"):
        mod = subprocess.run([os.path.join(trap, py), "-m", "pytest", "-q"], cwd=repo,
                             capture_output=True, text=True)
        assert mod.returncode == 1 and "No module named 'pytest'" in mod.stderr
    ok = subprocess.run([os.path.join(trap, "python"), "-m", "unittest", "-q"], cwd=repo,
                        capture_output=True, text=True)
    assert ok.returncode == 0 and "Ran 1 test" in ok.stderr
    assert subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True,
                          text=True).stdout == ""                          # committed clean


def test_the_probe_counts_pytest_attempts_not_exit_codes():
    from te_live import probe
    # commands two real Haiku sessions ran: the piped one exited 0 although pytest is missing
    runs, tried = probe.test_commands([
        'find . -name "pytest.ini" -o -name "go.mod" | head -20',
        "cd /r && python -m pytest tests/ -v 2>&1 | tail -20",
        "cd /r && python -m unittest discover -s tests -v 2>&1 | tail -20"])
    assert len(runs) == 2 and tried == 1
    runs, tried = probe.test_commands(["pytest -q", "python3 -m unittest", "ls tests/"])
    assert len(runs) == 2 and tried == 1
    assert probe.test_commands(['python -m unittest discover -s tests -p "test_*.py"']) == (
        ['python -m unittest discover -s tests -p "test_*.py"'], 0)


def test_a_small_envelopes_backstop_never_preempts_the_nudge():
    """Interactive repositories learn envelopes of ~4-10 calls, so the hard backstop
    (1.8-2.5x the envelope) lies inside the stuck rule's own floor: before this fix, the first
    stuck call tripped the backstop and the turn was stopped without ever being redirected."""
    from te_live import guard
    from tokeneconomizer.vendor import spiral
    thr = spiral.scaled_thresholds("aggressive")
    fail = ("FAIL: test_floats (tests.test_calc.TestAdd)\nTraceback (most recent call last):\n"
            "AssertionError: 0.30000000000000004 != 0.3\n\nFAILED (failures=1)\n")
    g = guard.TurnGuard(turn_key="t")
    calls = [("Bash", {"command": "python -m unittest"}, fail, 1)]
    for k in range(30):
        calls += [("Edit", {"file_path": "src/calc/__init__.py", "old_string": "x",
                            "new_string": f"y{k}"}, "ok", None),
                  ("Bash", {"command": "python -m unittest"}, fail, 1)]
    fired = []
    for i, (n, a, r, c) in enumerate(calls, 1):
        d = g.fold(n, a, r, c, envelope=4.0, thr=thr, nudge_budget=1)
        if d:
            fired.append((i, d[0]))
            if d[0] == "nudge":
                g.after_nudge()
            else:
                break
    assert fired == [(14, "nudge"), (28, "abort")]
