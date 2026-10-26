"""The answer-key guard: one policy, two enforcement points, three agents.

A SWE-bench instance's fix commit is public on GitHub, so an agent that can fetch it is not
solving the task -- it is reading the answer key, and the resolve verdict it produces is
worthless. That is not an arm-level concern, so the block applies to every arm including
`baseline`.

The policy is ONE case-insensitive substring, `github`, and its narrowness is the whole
argument. Over 3,538 stored run dirs / 558 network-shell calls, every match reads a
repository's source, history, PR diffs or `.patch` files, with **0 false positives** (no
`pip`/`npm`/`npx`/`yarn` install matches) and **0 misses** (nothing naming a `.patch`, a
`/commit/`, a `/pull/`, a mirror, or a benchmark repo falls outside it). The corpus in
`_ABUSIVE` / `_LEGITIMATE` below is drawn from those calls so a future widening or narrowing
of the rule has to face them.

It is enforced twice, because a hook is only as good as the CLI's cooperation:

  * the PreToolUse HOOK -- exit 2 + a reason on stderr. Honoured by all three CLIs, each on
    its own terms: Claude Code's settings.json, the Copilot CLI's, and Codex's
    `$CODEX_HOME/hooks.json` under `--dangerously-bypass-hook-trust` (verified live on
    codex-cli 0.153.1). It is the layer that tells the MODEL why, which a DNS failure
    cannot.
  * the /etc/hosts BLOCK -- the answer-key hosts pointed at 127.0.0.1 inside the task
    container. Needs no cooperation from any CLI, so it is identical on all three lanes and
    holds when a CLI release breaks a hook contract.

Offline: no Docker, no network, no credentials. The container-side behaviour these pin was
verified by hand in a real SWE-bench image -- curl/wget/`git clone`/`urllib` all refused,
pypi.org and a real `pip download` still working.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

from te_harness.guards import net_guard

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Answer-key fetches the rule must block: patches, PR diffs, raw files, API searches, a
# clone and urllib, across the GitHub hosts (truncated).
_ABUSIVE = [
    "timeout 60 curl -sSL https://github.com/django/django/commit/2cbd3967.patch -o /tmp/fix.patch",
    "cd /tmp && curl -s -m 30 'https://patch-diff.githubusercontent.com/raw/scikit-learn/scikit-learn/pull/25102.diff'",
    "timeout 25 curl -s https://raw.githubusercontent.com/django/django/stable/3.0.x/tests/mail/tests.py",
    "cd /tmp && curl -s -m 30 'https://api.github.com/search/issues?q=repo:scikit-learn/scikit-learn+set_output'",
    "cd /tmp && timeout 120 git clone --filter=blob:none --no-checkout https://github.com/sympy/sympy.git sympy-git",
    'python -c "import urllib.request; u=\'https://raw.githubusercontent.com/sympy/sympy/master/x.py\'"',
    "timeout 90 curl -s 'https://api.github.com/repos/sympy/sympy/pulls/17340/files?per_page=100'",
    "curl -sL 'https://github.com/sympy/sympy/commit/9f4fb03c.diff' -o pr12419.diff",
]
# Legitimate network traffic the rule must let through. Blocking any of these would break a
# lane rather than protect it.
_LEGITIMATE = [
    "pip install -e .",
    "source activate testbed && pip install pytest",
    "pip download --no-deps -d /tmp/dl six",
    "npm install",
    "npx playwright test",
    "yarn --frozen-lockfile",
    "curl -s http://localhost:3000/",
    "curl -sS https://pypi.org/simple/",
    "curl -sS https://files.pythonhosted.org/packages/x.whl",
    "python -m pytest -q",
]


@pytest.mark.parametrize("cmd", _ABUSIVE)
def test_every_answer_key_fetch_is_blocked(cmd):
    assert net_guard.decide(cmd)[0] is True, cmd


@pytest.mark.parametrize("cmd", _LEGITIMATE)
def test_no_legitimate_call_is_blocked(cmd):
    assert net_guard.decide(cmd)[0] is False, cmd


def test_a_block_carries_a_reason_the_model_can_act_on():
    """Exit 2 with an empty stderr is a refusal the CLI reports as a hook failure."""
    blocked, reason = net_guard.decide(_ABUSIVE[0])
    assert blocked and reason.strip()
    assert "answer key" in reason or "grading" in reason


# ---- the two enforcement points must express the SAME policy ------------------------
def test_every_blocked_host_is_also_caught_by_the_shell_rule():
    """One policy, or the layers disagree about what is allowed.

    A host in the DNS block that the shell rule would let through (or vice versa) means an
    agent gets a different answer depending on which layer it happens to hit first, and the
    telemetry from the two stops being comparable.
    """
    for host in net_guard.ANSWER_KEY_HOSTS:
        assert net_guard.decide(f"curl https://{host}/x")[0] is True, host


def test_hosts_block_is_wellformed_and_covers_the_answer_key_hosts():
    text = net_guard.hosts_block()
    assert text.startswith(net_guard.HOSTS_MARKER)
    assert text.endswith("\n")
    entries = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        assert len(parts) == 2, line          # one host per line: simpler to grep and undo
        entries[parts[1]] = parts[0]
    # 127.0.0.1, not 0.0.0.0: nothing listens on 443 in a task container, so the connection
    # is REFUSED at once instead of hanging until a timeout.
    assert set(entries.values()) == {"127.0.0.1"}
    # The hosts that serve repository pages, the API, raw files, PR diffs and archives.
    for host in ("github.com", "raw.githubusercontent.com", "api.github.com",
                 "patch-diff.githubusercontent.com", "codeload.github.com"):
        assert host in entries, host


def test_hosts_block_leaves_the_package_registries_alone():
    """The block is a NAME LIST, not a network cut -- that distinction is the design."""
    blocked = set(net_guard.hosts_block().split())
    for host in ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
                 "localhost", "127.0.0.1:3000"):
        assert host not in blocked, host


# ---- the hook, end to end through the real script ------------------------------------
def _run_hook(payload):
    return subprocess.run(
        [sys.executable, os.path.join(_REPO, "te_harness/guards/net_guard.py")],
        input=json.dumps(payload), capture_output=True, text=True)


def test_hook_exits_2_with_a_reason_on_stderr():
    p = _run_hook({"tool_name": "Bash",
                   "tool_input": {"command": _ABUSIVE[0]}})
    assert p.returncode == 2
    assert p.stderr.strip()


def test_hook_allows_a_legitimate_command():
    p = _run_hook({"tool_name": "Bash", "tool_input": {"command": "pip install -e ."}})
    assert p.returncode == 0


def test_hook_fails_open_on_unreadable_input():
    """An unreadable payload must never wedge the agent -- it is telemetry, not a lock."""
    p = subprocess.run(
        [sys.executable, os.path.join(_REPO, "te_harness/guards/net_guard.py")],
        input="not json", capture_output=True, text=True)
    assert p.returncode == 0


def test_the_lift_switch_lifts_the_hook(monkeypatch):
    env = dict(os.environ, TE_ALLOW_NETWORK_TOOLS="1")
    p = subprocess.run(
        [sys.executable, os.path.join(_REPO, "te_harness/guards/net_guard.py")],
        input=json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": _ABUSIVE[0]}}),
        capture_output=True, text=True, env=env)
    assert p.returncode == 0, "TE_ALLOW_NETWORK_TOOLS must lift every layer, or none"


# ---- the agent-independent counter ---------------------------------------------------
def _transcript(commands):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "transcript.jsonl"), "w") as fh:
        for i, cmd in enumerate(commands):
            fh.write(json.dumps({"message": {"content": [
                {"type": "tool_use", "id": f"t{i}", "name": "bash",
                 "input": {"command": cmd}}]}}) + "\n")
            fh.write(json.dumps({"message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]}}) + "\n")
    return d


def test_answer_key_attempts_counts_from_the_trace():
    """ATTEMPTS, not refusals, and read through the one trace seam.

    This is the only answer-key number available on a lane whose CLI ignores the hook, so a
    count that climbs while `net_guard_blocks` stays flat is the signal to watch.
    """
    from te_harness import swebench
    d = _transcript(["python -m pytest -q", _ABUSIVE[0], "pip install six", _ABUSIVE[2]])
    n = swebench.ClaudeSwebenchRunner._answer_key_attempts(None, d)
    assert n == 2


def test_answer_key_attempts_is_none_when_there_is_no_transcript():
    """None, not 0: "no trace" and "a clean trace" must not read the same."""
    from te_harness import swebench
    assert swebench.ClaudeSwebenchRunner._answer_key_attempts(
        None, tempfile.mkdtemp()) is None


def test_both_domains_count_answer_key_attempts_through_one_seam():
    """Web-Bench rows carry it too, and off the same function.

    It is the agent-independent count, so on a row whose `net_guard_installed` is false --
    every Codex Web-Bench row, guarded by the namespace hosts block -- it is the ONLY
    per-row answer-key telemetry, and every Web-Bench row must carry it, all three agents.
    """
    from te_harness import swebench, webbench
    assert webbench.answer_key_attempts is swebench.answer_key_attempts
    d = _transcript(["ls -la", _ABUSIVE[0]])
    assert webbench.answer_key_attempts(d) == 1
    assert swebench.ClaudeSwebenchRunner._answer_key_attempts(None, d) == 1


# ---- per-agent enforcement -----------------------------------------------------------
def test_the_merge_program_appends_once_and_keeps_what_was_there(tmp_path):
    """The program the Claude and Codex lanes run in the container, run here on a real file:
    a scaffold's hook survives, the guard is appended once, and re-arming is a no-op."""
    from te_harness import swebench
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "rtk-rewrite"}]}]}, "other": 1}))
    entry = {"matcher": "Bash", "hooks": [{"type": "command",
                                           "command": "/usr/bin/python3 /x/net_guard.py"}]}
    prog = tmp_path / "merge.py"
    prog.write_text(swebench.ClaudeSwebenchRunner._net_guard_merge_program(entry))
    for _ in range(2):
        out = subprocess.run([sys.executable, str(prog), str(path)], capture_output=True,
                             text=True, check=True).stdout
        assert out.strip() == "armed"
    doc = json.loads(path.read_text())
    assert doc["other"] == 1
    assert [h["hooks"][0]["command"] for h in doc["hooks"]["PreToolUse"]] == [
        "rtk-rewrite", "/usr/bin/python3 /x/net_guard.py"]


def test_codex_arms_its_hook_in_codex_home_and_with_it_the_trust_bypass():
    """Codex honours the hook in `$CODEX_HOME/hooks.json` -- verified live on 0.153.1 -- but
    only under --dangerously-bypass-hook-trust, so registering it is what turns the flag on
    for every segment of the task, fresh and resumed alike."""
    from te_harness import codex, swebench
    r = object.__new__(swebench.CodexSwebenchRunner)
    calls = []

    def fake_exec(name, script, timeout=None):
        calls.append(script)
        return subprocess.CompletedProcess(script, 0, stdout="armed\n", stderr="")
    r._exec = fake_exec
    assert r._guard_python == ""                       # unarmed by default
    assert r._register_net_guard("ctr", "/usr/bin/python3") is True
    (script,) = calls
    assert script.rstrip().endswith(f"{codex.C_HOME}/hooks.json")
    assert "PreToolUse" in script and '\\"matcher\\": \\"Bash\\"' in script
    assert r._guard_python == "/usr/bin/python3"
    assert "--dangerously-bypass-hook-trust" in codex.build_argv(hook=bool(r._guard_python))
    assert "--dangerously-bypass-hook-trust" in codex.build_resume_argv(
        "th", hook=bool(r._guard_python))
    assert "--dangerously-bypass-hook-trust" not in codex.build_argv(hook=False)


def test_the_codex_config_declares_no_hooks_table():
    """The hook lives in hooks.json, never in config.toml: one config layer carrying both
    representations makes Codex warn and pick one, so the contract keeps exactly one."""
    from te_harness import codex
    routing = codex.resolve_codex_routing({"OPENAI_API_KEY": "sk-test"})
    txt = codex.config_toml(routing=routing, model="gpt-5.6-sol", effort="high")
    assert "[hooks]" not in txt
    assert "pre_tool_use" not in txt


# ---- per-task cleanup must never kill the stream -------------------------------------
def test_stop_never_raises_when_docker_hangs(monkeypatch):
    """`_stop` runs in run_task's `finally`, so anything it raises ends the whole STREAM.

    On a loaded host with rootless Docker, `docker rm -f` can outlast its 120 s timeout, and
    a `TimeoutExpired` must not escape the finally: by then the task is graded and its row
    built, so a container that is not removed costs disk (and `_reap_exited` collects it
    later), not results. `_start` follows the same rule.
    """
    import subprocess as sp
    from te_harness import swebench

    def boom(cmd, timeout=120, check=False):
        raise sp.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(swebench, "_run", boom)

    class Fake:
        keep = False
        _stop = swebench.ClaudeSwebenchRunner._stop
    swebench._LIVE_CONTAINERS.add("te-fake-container")
    Fake()._stop("te-fake-container")          # must not raise
    assert "te-fake-container" not in swebench._LIVE_CONTAINERS, (
        "_stop must still drop the container from the live set, or the signal handler "
        "will try to clean up a name that no longer exists")
