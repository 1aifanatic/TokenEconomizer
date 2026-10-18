"""The Codex Web-Bench lane: the contract it writes, and the two answer-key layers it arms.

Web-Bench runs the agent on the HOST with no per-task container, which is what makes this
lane different from the Codex SWE-bench one rather than a copy of it. Two things therefore
have to be pinned here and nowhere else: the request contract must name HOST paths (a
container path would silently name a file that does not exist), and the answer-key layers
must live in the per-task CODEX_HOME and a namespace, because there is no container to hold
them -- the PreToolUse hook in `$CODEX_HOME/hooks.json` (which Codex honours under
`--dangerously-bypass-hook-trust`, verified live 2026-09-30), and the hosts block, which
needs no cooperation from the CLI at all.
"""
import os
import subprocess

from te_harness import webbench as wb
from te_harness import codex


def _stub(tmp_path, caps=(), ns=True):
    """A runner instance without construction: __init__ probes a CLI and a catalog, and
    none of the behaviour under test depends on either."""
    r = object.__new__(wb.CodexWebBenchRunner)
    r.routing = {"base_url": codex.OPENAI_DIRECT, "api_key": "tok"}
    r.api_key = "tok"
    r.model, r.effort = "gpt-5.6-terra", "high"
    r.base_prompt = "You are Codex\n\n# Personality\nchatty\n\n# Destructive Actions\ncare\n"
    r.system_skills = None
    r._capabilities = tuple(caps)
    r._lean_instructions_tokens = 0
    r.answer_key_ns = ns
    r.agent_bin = "/usr/bin/true"
    r.task_timeout = 5
    return r


# ---- the answer-key layer ---------------------------------------------------------------
def test_the_hook_is_armed_in_the_task_codex_home(tmp_path):
    """`$CODEX_HOME/hooks.json`, Claude Code's shape, and only reported armed once the guard
    has refused its probe -- the shared installer's rule, now on this lane too."""
    import json
    r = _stub(tmp_path)
    home = tmp_path / "run" / ".codex-home"
    home.mkdir(parents=True)
    assert r._install_net_guard(str(home), str(tmp_path)) is True
    assert r._net_guard_armed is True
    doc = json.loads((home / "hooks.json").read_text())
    (entry,) = doc["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["type"] == "command"
    assert entry["hooks"][0]["command"].endswith("net_guard.py")
    # the verification probe's own refusal is removed, so it is never counted as the agent's
    assert not (home / "net_guard_blocks.jsonl").exists()
    assert r._install_net_guard(str(home), str(tmp_path)) is True      # idempotent
    assert len(json.loads((home / "hooks.json").read_text())["hooks"]["PreToolUse"]) == 1


def test_only_an_armed_task_runs_with_the_trust_bypass(tmp_path, monkeypatch):
    """Codex skips every hook nobody approved in /hooks, so an armed task must pass
    --dangerously-bypass-hook-trust; an unarmed one keeps exactly the argv it always had.
    Either way the hook's log path reaches the agent's environment."""
    seen = []

    class _P:
        def __init__(self, cmd, **kw):
            seen.append((cmd, kw))
            self.stdin, self.pid = type("W", (), {"write": lambda s, x: None,
                                                  "close": lambda s: None})(), 1
            self.stdout, self.stderr = iter([]), type("R", (), {"read": lambda s: ""})()

        def wait(self, timeout=None): return 0
        def kill(self): pass

    from te_harness.guards import net_guard
    monkeypatch.setattr(subprocess, "Popen", _P)
    ws = tmp_path / "run" / "workspace"; ws.mkdir(parents=True)
    r = _stub(tmp_path)
    for armed in (False, True):
        r._net_guard_armed = armed
        with open(tmp_path / "t.jsonl", "w") as fh:
            r._segment(str(ws), "go", fh, [], 80, None)
        with open(tmp_path / "t.jsonl", "w") as fh:
            r._segment(str(ws), {"mode": "resume", "session_id": "th-1", "prompt": "nudge"},
                       fh, [], 80, None)
    (fresh_off, kw), (resume_off, _), (fresh_on, _), (resume_on, _) = seen
    flag = "--dangerously-bypass-hook-trust"
    assert flag not in fresh_off and flag not in resume_off
    assert flag in fresh_on and flag in resume_on
    assert kw["env"][net_guard.LOG_ENV].endswith("net_guard_blocks.jsonl")


def test_the_hosts_file_carries_the_answer_key_block_on_top_of_the_real_one(tmp_path):
    from te_harness.guards import net_guard
    r = _stub(tmp_path)
    path = r._hosts_file(str(tmp_path))
    text = open(path).read()
    assert net_guard.HOSTS_MARKER in text
    for host in ("github.com", "api.github.com", "raw.githubusercontent.com"):
        assert f" {host}" in text or f"\t{host}" in text
    # the real hosts file is preserved, or the agent loses localhost and every local
    # service it needs -- including the evaluator's own dev server
    assert "localhost" in text


def test_the_segment_runs_inside_a_namespace_that_bind_mounts_that_file(tmp_path, monkeypatch):
    """The namespace is the guard. Everything the agent spawns inherits it, which is the
    property the hook lacked -- it needs no cooperation from the CLI."""
    seen = {}

    class _P:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            self.stdin, self.pid = _W(), 1
            self.stdout, self.stderr = iter([]), _R()

        def wait(self, timeout=None): return 0
        def kill(self): pass

    class _W:
        def write(self, s): seen["prompt"] = s
        def close(self): pass

    class _R:
        def read(self): return ""

    monkeypatch.setattr(subprocess, "Popen", _P)
    ws = tmp_path / "run" / "workspace"; ws.mkdir(parents=True)
    r = _stub(tmp_path)
    with open(tmp_path / "t.jsonl", "w") as fh:
        r._segment(str(ws), "do the thing", fh, [], 80, None)

    cmd = seen["cmd"]
    assert cmd[:4] == ["unshare", "--user", "--map-root-user", "--mount"]
    assert cmd[4:6] == ["bash", "-c"]
    inner = cmd[6]
    assert "mount --bind" in inner and "/etc/hosts" in inner and inner.endswith('exec "$@"')
    # the agent's own argv follows, and the workspace replaced the container's /testbed
    assert r.agent_bin in cmd and "/testbed" not in cmd
    assert str(ws) in cmd
    assert seen["prompt"] == "do the thing"
    # the credential reaches the CLI under the name the provider block reads
    assert seen["kw"]["env"]["OPENAI_API_KEY"] == "tok"
    assert seen["kw"]["cwd"] == str(ws)


def test_without_a_usable_namespace_the_lane_runs_bare_and_says_so(tmp_path, monkeypatch):
    """A host that refuses user namespaces must not silently produce unguarded rows that
    look guarded: the segment drops the wrapper and `answer_key_hosts_blocked` goes false."""
    seen = {}

    class _P:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            self.stdin, self.pid = type("W", (), {"write": lambda s, x: None,
                                                  "close": lambda s: None})(), 1
            self.stdout, self.stderr = iter([]), type("R", (), {"read": lambda s: ""})()

        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(subprocess, "Popen", _P)
    ws = tmp_path / "run" / "workspace"; ws.mkdir(parents=True)
    r = _stub(tmp_path, ns=False)
    with open(tmp_path / "t.jsonl", "w") as fh:
        r._segment(str(ws), "go", fh, [], 80, None)
    assert seen["cmd"][0] != "unshare"


# ---- the request contract ---------------------------------------------------------------
def test_the_contract_names_host_paths_not_container_paths(tmp_path):
    """`model_instructions_file` pointing at a container path would name a file that does
    not exist on the host, so the base prompt would not be replaced -- and the untooling arm
    would quietly keep the sections it reports having dropped."""
    ws = str(tmp_path / "workspace")
    home = str(tmp_path / ".codex-home")
    r = _stub(tmp_path, caps=("interactive_guidance",))
    cfg = r._write_config(home, ws)
    assert f'model_instructions_file = "{home}/instructions.md"' in cfg
    assert codex.C_HOME not in cfg
    assert os.path.exists(os.path.join(home, "instructions.md"))
    assert r._lean_instructions_tokens > 0
    # the WORKSPACE is the trusted project here, not /testbed
    assert f'[projects."{ws}"]' in cfg and "/testbed" not in cfg
    # and the endpoint is the OpenAI API the lane resolved, exactly as on SWE-bench
    assert f'base_url = "{codex.OPENAI_DIRECT}"' in cfg


def test_a_mask_that_prunes_nothing_writes_no_instructions_file(tmp_path):
    r = _stub(tmp_path, caps=())
    cfg = r._write_config(str(tmp_path / "h"), str(tmp_path / "w"))
    assert "model_instructions_file" not in cfg
    assert r._lean_instructions_tokens == 0


# ---- accounting + guard ------------------------------------------------------------------
def test_a_killed_segment_is_flagged_not_counted_as_free(tmp_path):
    """Same predicate as the SWE-bench Codex runner, so one filter covers both domains."""
    r = _stub(tmp_path)
    good = r._row_from_segment({"tokens_source": "stream_usage", "output_tokens": 10})
    assert good["usage_incomplete"] is False
    bad = r._row_from_segment({"tokens_source": "no_turn_event", "output_tokens": None})
    assert bad["usage_incomplete"] is True and bad["output_tokens_missing"] is True
    assert "no_turn_event" in bad["usage_incomplete_reason"]


def test_the_codex_controller_is_selected_not_the_generic_guard(tmp_path):
    """The generic NudgeGuard parses a Claude stream and reprompts; feeding it a
    `codex exec --json` stream would arm a guard that can never fire."""
    from te_bench import codex_spiral
    r = _stub(tmp_path)
    assert r._make_guard({}) is None
    assert r._make_guard({"spiral_guard": True}) is None      # generic flags do not arm it
    g = r._make_guard({"codex_spiral_policy": codex_spiral.shipped_policy(20)})
    assert isinstance(g, codex_spiral.CodexResumeGuard)


def test_only_rtk_is_hostable_and_it_is_the_instruction_mode(tmp_path, monkeypatch):
    """Web-Bench hosts one scaffold for any agent, rtk. On Codex that is rtk's INSTRUCTION
    mode (files under CODEX_HOME), because the hook mode is one this CLI ignores. Anything
    else is refused rather than installed and ignored."""
    import pytest
    for other in ("repomap", "serena"):
        with pytest.raises(RuntimeError, match="not wired for the Codex Web-Bench lane"):
            wb.CodexWebBenchRunner(agent="codex", scaffold=other)
    # every Codex ARM that carries an unhostable scaffold is refused the same way -- today
    # exactly one, the repomap variant of codex-trimech (its generator walks `*.py`)
    from te_bench import baselines
    unhostable = sorted(a for a, s in baselines.ARM_SCAFFOLDS.items()
                        if a.startswith("codex-") and s not in wb.WEBBENCH_SCAFFOLDS)
    assert unhostable == ["codex-trimech-repomap"], unhostable
    for arm in unhostable:
        with pytest.raises(RuntimeError, match="not wired for the Codex Web-Bench lane"):
            wb.CodexWebBenchRunner(agent="codex", scaffold=baselines.ARM_SCAFFOLDS[arm])
    # rtk gets past the scaffold gate; with no key the next stop is the endpoint
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="nothing to bill"):
        wb.CodexWebBenchRunner(agent="codex", scaffold="rtk")
    # and the profile the scaffold sees points its install at THIS task's CODEX_HOME
    r = _stub(tmp_path)
    home = str(tmp_path / "run" / ".codex-home")
    prof = r.scaffold_profile_for(home)
    assert prof.kind == "codex" and prof.env == {"CODEX_HOME": home}
    assert prof.instructions_file == "AGENTS.md" and "RTK.md" in prof.patch_excludes
    assert prof.bin_dir == wb.WebBenchRunner.scaffold_bin_dir(home)
    assert r.scaffold_exec_env(str(tmp_path / "run")) == {"CODEX_HOME": home}


def test_the_scaffold_bin_dir_is_first_on_the_agents_path(tmp_path, monkeypatch):
    """`rtk` is copied into a per-run bin dir on the host; the agent's own shell must find it
    or every prefixed command fails with `rtk: command not found` on a row that reports the
    scaffold healthy."""
    seen = {}

    class _P:
        def __init__(self, cmd, **kw):
            seen["env"] = kw["env"]
            self.stdin, self.pid = type("W", (), {"write": lambda s, x: None,
                                                  "close": lambda s: None})(), 1
            self.stdout, self.stderr = iter([]), type("R", (), {"read": lambda s: ""})()

        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(subprocess, "Popen", _P)
    ws = tmp_path / "run" / "workspace"; ws.mkdir(parents=True)
    r = _stub(tmp_path, ns=False)
    with open(tmp_path / "t.jsonl", "w") as fh:
        r._segment(str(ws), "go", fh, [], 80, None)
    home = r.agent_home(str(tmp_path / "run"))
    assert seen["env"]["PATH"].split(os.pathsep)[0] == wb.WebBenchRunner.scaffold_bin_dir(home)


def test_pruning_keeps_the_rtk_instructions_as_provenance(tmp_path):
    r = _stub(tmp_path)
    run_dir = tmp_path / "run"
    home = run_dir / ".codex-home"; home.mkdir(parents=True)
    for name in ("config.toml", "AGENTS.md", "RTK.md", "state_5.sqlite"):
        (home / name).write_text("x")
    r.keep_workspaces = False
    r._prune_agent_home(str(run_dir))
    assert sorted(os.listdir(home)) == ["AGENTS.md", "RTK.md", "config.toml"]


def test_the_codex_runner_sets_every_field_the_inherited_lifecycle_reads():
    """The Codex runner does NOT call WebBenchRunner.__init__ -- that resolves the Anthropic
    key -- so every attribute the inherited methods read has to be set here by hand.

    Pinned because of how the gap surfaces: not at construction, but as an AttributeError
    deep in `run_task`, after the evaluator has started and the first task has been paid for.
    That is exactly how `routing_base` was found, on a live Web-Bench run.
    """
    import inspect, re
    base = inspect.getsource(wb.WebBenchRunner)
    init = inspect.getsource(wb.CodexWebBenchRunner.__init__)
    read = set(re.findall(r"self\.([a-z_][a-z0-9_]*)", base))
    assigned = set(re.findall(r"self\.([a-z_][a-z0-9_]*)\s*=", init))
    # set by the base helper this __init__ calls at the end
    assigned |= set(re.findall(r"self\.([a-z_][a-z0-9_]*)\s*=",
                               inspect.getsource(wb.WebBenchRunner._init_stream_state)))
    known = assigned | set(vars(wb.CodexWebBenchRunner)) | set(vars(wb.WebBenchRunner))
    missing = sorted(a for a in read if a not in known and not a.startswith("_current"))
    assert not missing, f"inherited code reads these, Codex never sets them: {missing}"


def test_the_per_task_codex_home_is_pruned_to_its_provenance(tmp_path):
    """The CLI's own state is ~38 MB per task-run and would put ~19 GB of sqlite WALs,
    bundled skills and shell snapshots in the results tree over a five-project, five-seed
    sweep. The SWE-bench lane has no such problem -- its CODEX_HOME dies with the container --
    so the host lane prunes to restore that parity, keeping the contract, which is provenance.
    """
    r = _stub(tmp_path)
    run_dir = tmp_path / "run"
    home = run_dir / ".codex-home"; home.mkdir(parents=True)
    (home / "config.toml").write_text("model = 'x'\n")
    (home / "instructions.md").write_text("lean\n")
    (home / "state_5.sqlite-wal").write_text("x" * 1000)
    (home / "sessions").mkdir(); (home / "sessions" / "roll.jsonl").write_text("{}")
    (run_dir / ".host-home").mkdir()

    r.keep_workspaces = False
    r._prune_agent_home(str(run_dir))
    assert sorted(os.listdir(home)) == ["config.toml", "instructions.md"]
    assert not (run_dir / ".host-home").exists()


def test_keep_workspaces_keeps_the_whole_agent_home(tmp_path):
    """The same knob that keeps the workspace for a post-mortem keeps this too -- one
    switch, so an operator debugging a task does not have to know about two."""
    r = _stub(tmp_path)
    run_dir = tmp_path / "run"
    home = run_dir / ".codex-home"; home.mkdir(parents=True)
    (home / "state_5.sqlite").write_text("x")
    r.keep_workspaces = True
    r._prune_agent_home(str(run_dir))
    assert (home / "state_5.sqlite").exists()
