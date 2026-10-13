"""Exactly ONE Anthropic credential reaches the Claude Code process: ANTHROPIC_API_KEY.

The Claude lane bills api.anthropic.com directly. Nothing that could point the CLI at
another endpoint, credential or model catalog (`swebench.IGNORED_ANTHROPIC_VARS`) may reach
the agent, whichever way its environment is built:

  * SWE-bench runs the agent in a task container whose environment is a list of NAMED
    variables, so the key is the only Anthropic one in that list -- passed by name, never
    by value, so it stays out of argv;
  * Web-Bench runs the agent on the HOST, where it inherits the operator's whole shell, so
    the steering variables are removed from that copy before the CLI starts.

Offline: the agent process is a stand-in that records what it was handed.
"""
import json

from te_harness import swebench as sw
from te_harness import webbench as wb

_STEERING = {"ANTHROPIC_BASE_URL": "https://example.invalid",
             "ANTHROPIC_AUTH_TOKEN": "tok-SECRET",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4.5"}


class _Proc:
    """A Popen stand-in: no output, exits cleanly, remembers argv and env."""

    seen = {}

    def __init__(self, cmd, **kw):
        _Proc.seen = {"cmd": list(cmd), "env": dict(kw.get("env") or {})}
        self.pid = 1
        self.stdin = type("W", (), {"write": lambda s, x: None, "close": lambda s: None})()
        self.stdout = iter([])
        self.stderr = type("R", (), {"read": lambda s: ""})()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_host_agent_env_keeps_only_the_key():
    """Web-Bench's agent inherits the operator's shell; the steering variables go."""
    env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-leftover", **_STEERING}
    assert sw.agent_credential_env(env, api_key="sk-ant-x") is env
    assert env == {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-x"}


def test_the_swebench_container_is_handed_the_key_by_name_and_nothing_else(monkeypatch):
    for k, v in _STEERING.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sw.subprocess, "Popen", _Proc)
    r = sw.ClaudeSwebenchRunner.__new__(sw.ClaudeSwebenchRunner)
    r.api_key, r.model, r.effort = "sk-ant-SECRET", "claude-opus-5", "high"
    r.protected_tools, r.scaffold, r.task_timeout = sw.PROTECTED_TOOLS, None, 5
    r._session_log_text = lambda name: ""
    with open("/dev/null", "w") as fh:
        r._segment("ctr", "go", fh, [], 80, None)
    cmd = " ".join(_Proc.seen["cmd"])
    assert "-e ANTHROPIC_API_KEY " in cmd                    # by name...
    assert "sk-ant-SECRET" not in cmd                        # ...never by value
    for var in sw.IGNORED_ANTHROPIC_VARS:
        assert var not in cmd, var
    assert _Proc.seen["env"]["ANTHROPIC_API_KEY"] == "sk-ant-SECRET"


def test_the_webbench_agent_never_inherits_a_steering_variable(tmp_path, monkeypatch):
    for k, v in _STEERING.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(wb.subprocess, "Popen", _Proc)
    r = wb.WebBenchRunner.__new__(wb.WebBenchRunner)
    r.claude, r.api_key, r.model, r.effort = "/usr/bin/true", "sk-ant-x", "claude-opus-5", "high"
    r.protected_tools, r.task_timeout = wb.PROTECTED_TOOLS, 5
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True)
    with open(tmp_path / "t.jsonl", "w") as fh:
        r._segment(str(ws), "go", fh, [], 80, None)
    env = _Proc.seen["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x"
    for var in sw.IGNORED_ANTHROPIC_VARS:
        assert var not in env, var


def test_the_optimizer_log_names_the_agent(tmp_path, monkeypatch):
    """One log per results root, `cell` names no tree: a Claude and a Copilot lane over the
    same arm/repo/seed collide on the key, so the agent has to be on the line."""
    log = tmp_path / "optimizer_calls.jsonl"
    monkeypatch.setenv("TE_OPTIMIZER_LOG", str(log))
    sw.set_optimizer_context(arm="trimech", instance_id="sympy__sympy-1", model="claude-opus-4-8",
                             agent="copilot", cell="trimech/sympy_s0", run_dir="r")
    sw._log_optimizer_call("claude-opus-4-8", "You configure the toolset for an autonomous "
                           "coding agent ...", "{}", {"input_tokens": 1}, "end_turn")
    row = json.loads(log.read_text().splitlines()[0])
    assert row["agent"] == "copilot" and row["cell"] == "trimech/sympy_s0"
    assert row["kind"] == "tool_judge"
    sw.set_optimizer_context()
