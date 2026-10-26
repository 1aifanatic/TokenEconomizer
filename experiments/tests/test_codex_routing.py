"""The Codex lane bills the OpenAI API directly, and the pieces that must agree about it.

    OPENAI_API_KEY    the credential -- required; the lane refuses at construction without it
    OPENAI_BASE_URL   OpenAI's own regional endpoint, for a region-scoped key; the global
                      endpoint otherwise

Nothing else selects an endpoint. Pinned here, because a launch banner stays green when any
of them is wrong:

  * the RESOLUTION, including that no other variable moves the lane off the OpenAI API;
  * the REQUEST CONTRACT config.toml expresses, and that it carries the key by NAME only;
  * the CACHE ARITHMETIC in Codex's usage block, where `input_tokens` is the GRAND total
    and forgetting to subtract the cache fields prices ~90% cached re-reads at 1.0x.

All offline: an explicit env dict, no network, no CLI binary, no credentials.
"""
import json
import os
import subprocess

import pytest

from te_harness import codex
from te_harness.guards import net_guard
from te_bench import spiral_stage as spiral_mech
from tokeneconomizer.toolprune import codex as codex_untool, stage as tooling

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_KEY = "sk-test-key-abcd"
_DIRECT = {"base_url": codex.OPENAI_DIRECT, "api_key": _KEY}


@pytest.mark.parametrize("env,base", [
    ({"OPENAI_API_KEY": _KEY}, "https://api.openai.com/v1"),
    ({"OPENAI_API_KEY": _KEY, "OPENAI_BASE_URL": "https://us.api.openai.com/v1/"},
     "https://us.api.openai.com/v1"),
    # Not consulted: nothing but OPENAI_BASE_URL names the endpoint.
    ({"OPENAI_API_KEY": _KEY, "ANTHROPIC_BASE_URL": "https://example.invalid"},
     "https://api.openai.com/v1"),
])
def test_the_lane_resolves_to_the_openai_api(env, base):
    assert codex.resolve_codex_routing(dict(env)) == {"base_url": base, "api_key": _KEY}


@pytest.mark.parametrize("env", [
    {}, {"OPENAI_BASE_URL": "https://us.api.openai.com/v1", "ANTHROPIC_API_KEY": "sk-ant-x"}])
def test_without_the_openai_key_the_lane_is_refused_at_construction(env):
    """Not once per task: a lane with nothing to bill must die before task 0, and no other
    variable stands in for the key."""
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        codex.resolve_codex_routing(dict(env))


def test_validate_priceable_refuses_an_unknown_id():
    """`weights_for` hard-fails on an unpriced id at the END of a paid rollout, so the
    runner checks at construction instead."""
    codex.validate_priceable("gpt-5.6-sol", "codex")                 # must not raise
    with pytest.raises(ValueError):
        codex.validate_priceable("not-a-model", "codex")


# ---- the request contract -------------------------------------------------------------
def test_config_toggles_cover_exactly_the_mechanism_s_config_capabilities():
    """Layer 1 nominates the packages; the runner owns the mapping. They must line up.

    A capability the mechanism reports as config-pruned but the runner has no toggle for is
    an OVERSTATED saving on the arm whose entire claim is the saving.
    """
    assert codex.CONFIG_CAPABILITIES == codex_untool.CONFIG_CAPABILITIES


def test_config_toggles_refuse_a_protected_capability():
    with pytest.raises(RuntimeError, match="protected"):
        codex.config_toggles(["collaboration", "apply_patch"])


def test_web_is_blocked_on_every_arm_including_an_empty_mask():
    """Contamination control, not a mechanism: the answer key is a fetch away.

    Mirrors `swebench.network_tool_block` on the other two lanes. The consequence is
    deliberate and documented -- with web off in the baseline too, the `web` package
    contributes no untooling saving on this benchmark.
    """
    top, tables = codex.config_toggles([])
    # The TOP-LEVEL spelling only. codex-cli 0.153.1 deprecated `[features].web_search`
    # ("web search is enabled by default; set `web_search` to disabled at the top level")
    # and emits an error item into the transcript when it is present, so no table entry is
    # written -- see codex._TABLE_TOGGLES["web"].
    assert 'web_search = "disabled"' in top
    assert not any(t == "features" and "web_search" in line for t, line in tables)
    assert not any(t == "tools" and "web_search" in line for t, line in tables)
    # ...and asking for `web` as well must not duplicate a key: Codex rejects that.
    top2, tables2 = codex.config_toggles(["web"])
    assert len(top2) == len(set(top2)) and len(tables2) == len(set(tables2))
    assert sorted(top) == sorted(top2)


def test_network_block_can_be_lifted(monkeypatch):
    monkeypatch.setenv("TE_ALLOW_NETWORK_TOOLS", "1")
    top, tables = codex.config_toggles([])
    assert top == [] and tables == []


def test_config_toml_names_the_openai_api_and_the_key_by_name_only():
    txt = codex.config_toml(routing=_DIRECT, model="gpt-5.6-sol", effort="high",
                            capabilities=())
    assert 'model_provider = "openai_direct"' in txt
    assert "[model_providers.openai_direct]" in txt
    assert txt.count("[model_providers.") == 1
    assert f'base_url = "{codex.OPENAI_DIRECT}"' in txt
    assert 'env_key = "OPENAI_API_KEY"' in txt and codex.KEY_ENV == "OPENAI_API_KEY"
    assert 'wire_api = "responses"' in txt
    assert 'trust_level = "trusted"' in txt
    assert "requires_openai_auth" not in txt
    # The credential VALUE never goes in the file -- only the variable name.
    assert _KEY not in txt


def test_a_regional_endpoint_is_written_as_given():
    r = codex.resolve_codex_routing({"OPENAI_API_KEY": _KEY,
                                     "OPENAI_BASE_URL": "https://eu.api.openai.com/v1"})
    txt = codex.config_toml(routing=r, model="gpt-5.6-sol", effort="high")
    assert 'base_url = "https://eu.api.openai.com/v1"' in txt


def test_the_key_reaches_the_container_by_name_only():
    assert codex.KEY_ENV not in codex.run_env()
    assert _KEY not in codex.run_env().values()


# ---- accounting: the cache arithmetic -------------------------------------------------
def _stream(*events):
    return "\n".join(json.dumps(e) for e in events)


def test_row_usage_decomposes_the_grand_input_total():
    """`input_tokens` INCLUDES both cache fields; fresh input is what is left.

    Folding the cache fields into input_tokens instead would price cached re-reads at 1.0x
    where the vendor bills 0.10x -- a one-directional error that would make this lane's rows
    look more expensive than the other lanes' on identical work.
    """
    row = codex.row_usage(_stream(
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.completed", "item": {"type": "command_execution",
                                            "aggregated_output": "ok"}},
        {"type": "item.completed", "item": {"type": "file_change"}},
        {"type": "item.completed", "item": {"type": "agent_message"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 10_000, "cached_input_tokens": 9_000,
            "cache_write_input_tokens": 400, "output_tokens": 300,
            "reasoning_output_tokens": 120}},
    ))
    assert row["input_tokens"] == 600          # 10000 - 9000 - 400
    assert row["cache_read_input_tokens"] == 9_000
    assert row["cache_creation_input_tokens"] == 400
    assert row["output_tokens"] == 420         # output + reasoning, as billed
    assert row["tokens_total"] == 600 + 9_000 + 400 + 420
    assert row["num_turns"] == 3               # steps, not turn.completed events
    assert row["turns_basis"] == "codex_steps"
    assert row["thread_id"] == "th-1"
    assert row["tokens_source"] == "stream_usage"


def test_row_usage_accumulates_across_turns():
    row = codex.row_usage(_stream(
        {"type": "turn.completed", "usage": {"input_tokens": 100,
                                             "cached_input_tokens": 10,
                                             "output_tokens": 5}},
        {"type": "turn.completed", "usage": {"input_tokens": 200,
                                             "cached_input_tokens": 20,
                                             "output_tokens": 7}},
    ))
    assert row["input_tokens"] == 270 and row["cache_read_input_tokens"] == 30
    assert row["output_tokens"] == 12


def test_a_killed_stream_records_unknown_not_zero():
    """A guard/timeout kill takes the usage block with it.

    None, not 0: a task recorded as free would be averaged in as a cheap success on
    precisely the guarded arm the spiral mechanism must show saving on.
    """
    row = codex.row_usage(_stream(
        {"type": "item.completed", "item": {"type": "command_execution"}}))
    assert row["input_tokens"] is None
    assert row["tokens_total"] is None
    assert row["tokens_source"] == "no_turn_event"


def test_merge_usage_propagates_unknown():
    good = codex.row_usage(_stream({"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 5}}))
    dead = codex.row_usage("")
    merged = codex.merge_usage(good, dead)
    assert merged["input_tokens"] is None, "a half-known total must not read as known"
    assert merged["tokens_source"] != "stream_usage"
    both = codex.merge_usage(good, good)
    assert both["input_tokens"] == 180 and both["tokens_source"] == "stream_usage"


def test_merge_keeps_the_first_thread_id():
    """A later resume must continue the FIRST segment's thread, never a new session."""
    a = codex.row_usage(_stream({"type": "thread.started", "thread_id": "th-A"},
                                {"type": "turn.completed", "usage": {"input_tokens": 1}}))
    b = codex.row_usage(_stream({"type": "thread.started", "thread_id": "th-B"},
                                {"type": "turn.completed", "usage": {"input_tokens": 1}}))
    assert codex.merge_usage(a, b)["thread_id"] == "th-A"


# ---- the net guard's payload spelling -------------------------------------------------
@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": "curl https://github.com/x"}},
    {"toolName": "bash", "toolArgs": {"command": "curl https://github.com/x"}},
    {"tool": {"name": "shell",
              "input": {"command": ["bash", "-lc", "curl https://github.com/x"]}}},
    {"item": {"type": "command_execution", "command": "curl https://github.com/x"}},
])
def test_guard_reads_every_payload_spelling(payload):
    """Codex's `pre_tool_use` spelling is not captured off a live hook, and its hooks table
    is not strictly validated -- so the guard must not depend on one key name. An
    unreadable payload means `decide("")`, exit 0, and a lane that looks armed and guards
    nothing."""
    cmd = net_guard.hook_command(payload)
    assert cmd, payload
    assert net_guard.decide(cmd)[0] is True


def test_guard_still_returns_empty_for_a_payload_with_no_command():
    assert net_guard.hook_command({"nothing": 1}) == ""
    assert net_guard.decide("")[0] is False


# ---- the mechanism gates --------------------------------------------------------------
def test_the_codex_label_gets_the_codex_mechanisms():
    """The harness registers exactly one Codex label, and both mechanism gates must open on
    it -- a gate keyed on a label the registry does not have would construct no controller
    while every row still carried the arm's name."""
    from te_harness import swebench, webbench
    assert "codex" in tooling._CODEX_AGENTS and "codex" in spiral_mech._CODEX_AGENTS
    for runners in (swebench.RUNNERS, webbench.RUNNERS):
        assert [a for a in runners if a.startswith("codex")] == ["codex"]


# ---- the results tree ---------------------------------------------------------------
def _sh(script):
    p = subprocess.run(["bash", "-c", ". scripts/launch/_env.sh; " + script], cwd=_REPO,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_the_codex_tree_is_named_by_agent_domain_and_model():
    assert _sh('te_tree_name codex swebench "$(te_model_tag gpt-5.6-sol)"') == \
        "codex_swebench_gpt56sol"


def test_a_model_tag_never_contains_a_path_separator():
    """A slash in the tag would turn one tree into a nested PATH and split the cell."""
    for model in ("vendor/gpt-5.6-sol", "gpt-5.6-sol"):
        assert "/" not in _sh(f'te_model_tag "{model}"'), model


def test_claude_model_tags_are_unchanged():
    """The tag helper is shared, so the Codex lane must not move any stored tree's name."""
    for model, expect in (("claude-opus-4-8", "opus48"), ("claude-opus-4.8", "opus48"),
                          ("claude-sonnet-5", "sonnet5"), ("auto", ""), ("", "")):
        assert _sh(f'te_model_tag "{model}"') == expect, model


def test_the_mechanism_call_is_a_tool_free_responses_request_to_the_openai_api(monkeypatch):
    """The Codex twin of the Claude lane's Messages call: the lane's own model, on its own
    key, at its own endpoint, with no tools offered -- a pure text completion."""
    import io, urllib.request
    seen = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["body"] = json.loads(req.data.decode())
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp(json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "ok"}]}], "usage": {"input_tokens": 3,
            "output_tokens": 1}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    text, eff = codex.optimizer_call("Reply with the single word: ok",
                                     configured_model="gpt-5.6-terra",
                                     routing=_DIRECT, max_tokens=16)
    assert text == "ok" and eff > 0
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert seen["headers"]["authorization"] == f"Bearer {_KEY}"
    assert "user-agent" not in seen["headers"]
    body = seen["body"]
    assert body["model"] == "gpt-5.6-terra"
    assert "tools" not in body and "tool_choice" not in body
    assert body["max_output_tokens"] == 16 and body["store"] is False


def test_the_model_check_reads_the_openai_catalog(monkeypatch):
    import io, urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return _Resp(json.dumps({"object": "list", "data": [
            {"id": "gpt-5.6-sol"}, {"id": "gpt-5.6-terra"}]}).encode())

    codex._CATALOG.clear()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert codex.verify_model("gpt-5.6-sol", _DIRECT) == (True, "")
    ok, why = codex.verify_model("gpt-9", _DIRECT)
    assert ok is False and "gpt-5.6-terra" in why
    assert seen == {"url": "https://api.openai.com/v1/models", "auth": f"Bearer {_KEY}"}
    codex._CATALOG.clear()


def test_an_unreadable_catalog_is_inconclusive_not_a_refusal(monkeypatch):
    import urllib.request

    def _boom(*a, **k):
        raise OSError("connection refused")

    codex._CATALOG.clear()
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    served, why = codex.verify_model("gpt-5.6-sol", _DIRECT)
    assert served is None and "unreadable" in why
    codex._CATALOG.clear()


def test_the_skills_package_is_pruned_by_disabling_every_bundled_system_skill():
    """0.153.1 has no switch for the <skills_instructions> listing, but disabling each
    bundled skill through `[[skills.config]]` removes the whole block. The entries name
    in-container paths and come last in the file, because an array-of-tables header
    swallows any key after it."""
    txt = codex.config_toml(routing=_DIRECT, model="gpt-5.6-terra", effort="high",
                            capabilities=["skills"], system_skills=("alpha", "beta"))
    blocks = txt.split("[[skills.config]]")
    assert len(blocks) == 3
    assert f'path = "{codex.C_HOME}/skills/.system/alpha/SKILL.md"' in blocks[1]
    assert "enabled = false" in blocks[1] and "enabled = false" in blocks[2]
    assert txt.index("[projects.") < txt.index("[[skills.config]]")
    # without the package in the mask no entry is written; the default set covers 0.153.1
    plain = codex.config_toml(routing=_DIRECT, model="gpt-5.6-terra", effort="high",
                              capabilities=[])
    assert "skills.config" not in plain
    assert len(codex.skills_config_entries(None)) == len(codex.DEFAULT_SYSTEM_SKILLS) == 6
    assert "skills" in codex.CONFIG_CAPABILITIES


def _alive(pid):
    """True while `pid` runs (a zombie awaiting its reaper counts as gone)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return False


def _fake_cli(tmp_path, body):
    """A stand-in for the npm `codex` shim: it starts a long-lived CHILD, as the real shim
    execs the native ELF, records the child's pid, then runs `body`."""
    pidfile = tmp_path / "child.pid"
    shim = tmp_path / "codex"
    shim.write_text(f"#!/bin/sh\nsleep 600 &\necho $! > {pidfile}\n{body}\n")
    shim.chmod(0o755)
    return str(shim), pidfile


def _assert_reaped(pidfile):
    import time
    pid = int(pidfile.read_text())
    for _ in range(50):
        if not _alive(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"the CLI's child {pid} outlived the probe")


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_the_skills_probe_kills_the_cli_it_started_not_just_the_shim(tmp_path):
    """`proc.kill()` reaches only the Node shim, and the native CLI the shim started would
    sit "waiting for network" for good on an offline host. The probe owns a process group
    and kills all of it."""
    shim, pidfile = _fake_cli(tmp_path, (
        'mkdir -p "$CODEX_HOME/skills/.system/alpha"\n'
        'touch "$CODEX_HOME/skills/.system/alpha/SKILL.md"\n'
        'touch "$CODEX_HOME/skills/.system/.codex-system-skills.marker"\n'
        "wait"))
    assert codex.bundled_system_skills(binary=shim, home=str(tmp_path), timeout=20) == ["alpha"]
    _assert_reaped(pidfile)


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_the_config_validator_kills_a_hung_cli_and_its_children(tmp_path):
    shim, pidfile = _fake_cli(tmp_path, "sleep 600")
    err = codex.validate_config('model = "gpt-5.6-sol"\n', binary=shim, home=str(tmp_path),
                                timeout=2)
    assert err.startswith("could not run the config validator")
    _assert_reaped(pidfile)


def test_bundled_system_skills_are_read_from_the_installed_cli():
    """The skills toggle disables bundled skills BY PATH, so the set must come from the CLI
    that will run, not from a list written for one version. Offline: the throwaway run is
    killed at the skills marker before it can wait for a network it does not have."""
    import shutil
    if not (os.environ.get("TE_CODEX_BIN") or shutil.which("codex")):
        pytest.skip("codex-cli not installed")
    found = codex.bundled_system_skills()
    assert found, "discovery returned nothing; the runner would fall back to the static set"
    assert all("/" not in n for n in found)
    # 0.153.1 ships these; a newer CLI may add to the set, which is exactly why it is read.
    assert {"skill-creator", "skill-installer"} <= set(found)


# ---------------------------------------------------------------------------------------
# The instruction package (2026-09-11): chat-session guidance in the base prompt is
# removed through a lean copy written to model_instructions_file; operational sections
# stay byte-for-byte.
# ---------------------------------------------------------------------------------------
_FAKE_BASE = (
    "You are Codex.\n\n# Personality\n\nBe charming.\n\n## Writing style\n\nShort.\n\n"
    "# Working with the user\n\nChannels.\n\nYou end your turn by sending a final message.\n\n"
    "When you run out of context, continue.\n\n"
    "# Rules for getting work done\n\n- use rg\n\n## Autonomy and persistence\n\nAct.\n\n"
    "# Using skills\n\nSkills intro.\n\n### How to use skills\n\nRead SKILL.md.\n")


def test_lean_instructions_drops_only_the_pruned_sections_and_keeps_the_rest_verbatim():
    lean = codex.lean_instructions(_FAKE_BASE, ["interactive_guidance"])
    assert "# Personality" not in lean and "## Writing style" not in lean
    assert "# Working with the user" not in lean and "Channels." not in lean
    assert "When you run out of context, continue." in lean          # compaction paragraph kept
    # the stopping rule is operational and is kept too: without it the agent has no
    # statement of how a turn ends, and runs more model calls than its baseline.
    assert "You end your turn by sending a final message." in lean
    for kept in ("You are Codex.", "# Rules for getting work done\n\n- use rg",
                 "## Autonomy and persistence\n\nAct.", "# Using skills", "### How to use skills"):
        assert kept in lean
    both = codex.lean_instructions(_FAKE_BASE, ["interactive_guidance", "skills"])
    assert "# Using skills" not in both and "Read SKILL.md." not in both
    assert "# Rules for getting work done" in both
    # nothing to drop -> no file, the CLI keeps its built-in prompt
    assert codex.lean_instructions(_FAKE_BASE, ["collaboration", "goals"]) is None
    assert codex.lean_instructions(None, ["interactive_guidance"]) is None


def test_the_captured_base_prompt_exists_for_the_pinned_cli_and_for_no_other():
    base = codex.base_prompt_for("codex-cli 0.153.1")
    assert base and base.startswith("You are Codex") and len(base) // 4 > 4000
    assert codex.base_prompt_for("codex-cli 9.9.9") is None
    assert codex.base_prompt_for("") is None
    lean = codex.lean_instructions(base, ["interactive_guidance", "skills"])
    assert len(lean) < 0.45 * len(base)
    for header in ("# Rules for getting work done", "## File editing constraints",
                   "## Autonomy and persistence", "# Destructive Actions"):
        assert header in lean
    for header in codex.INSTRUCTION_SECTIONS["interactive_guidance"] + codex.INSTRUCTION_SECTIONS["skills"]:
        assert ("\n" + header + "\n") not in lean


def test_config_toml_names_the_instructions_file_only_when_a_lean_prompt_exists():
    with_file = codex.config_toml(routing=_DIRECT, model="gpt-5.6-terra", effort="high",
                                  capabilities=["interactive_guidance"],
                                  instructions="lean text")
    assert f'model_instructions_file = "{codex.INSTRUCTIONS_PATH}"' in with_file
    without = codex.config_toml(routing=_DIRECT, model="gpt-5.6-terra", effort="high",
                                capabilities=["interactive_guidance"], instructions=None)
    assert "model_instructions_file" not in without
    assert "interactive_guidance" in codex.CONFIG_CAPABILITIES


def test_codex_trimech_names_no_endpoint():
    """The arm is endpoint-agnostic by construction: nothing in its config names one.

    Stated as a test because the three mechanisms reach the runner through three different
    channels (prompt prefix, config.toml, live stream) and each one could have grown an
    endpoint assumption; the endpoint is the runner's to resolve.
    """
    from te_bench.arms import ARMS
    import dataclasses
    cfg = ARMS["codex-trimech"].cfg()
    flat = repr(dataclasses.asdict(cfg)) if dataclasses.is_dataclass(cfg) else repr(cfg)
    for token in ("openai", "copilot", "http", "base_url", "api_key"):
        assert token not in flat.lower(), f"{token!r} leaked into the arm's config"


def test_rtk_compliance_is_read_off_the_trace(tmp_path):
    """On Codex rtk is an instruction, not a hook, so whether it did anything is whether the
    agent typed `rtk`. Counted from the transcript like answer_key_attempts; None when there
    is no transcript, so a lane of zeros stays distinguishable from a lane of no data."""
    from te_harness import codex
    from fixtures import write_codex_transcript
    p = str(tmp_path / "t.jsonl")
    write_codex_transcript(p, [
        {"type": "command_execution", "command": '/bin/bash -lc "rtk git status"',
         "aggregated_output": "ok", "exit_code": 0},
        {"type": "command_execution", "command": '/bin/bash -lc "pytest -q"',
         "aggregated_output": "1 passed", "exit_code": 0},
        {"type": "command_execution", "command": "/bin/bash -lc '/usr/local/bin/rtk proxy make'",
         "aggregated_output": "", "exit_code": 0},
        {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]},
    ])
    got = codex.rtk_compliance(p)
    assert got == {"rtk_shell_commands": 3, "rtk_prefixed_commands": 2,
                   "rtk_compliance": 0.667}
    assert codex.rtk_compliance(str(tmp_path / "missing.jsonl")) == {
        "rtk_shell_commands": None, "rtk_prefixed_commands": None, "rtk_compliance": None}
    assert codex._unwrap_shell('/bin/bash -lc "rtk ls"') == "rtk ls"
    assert codex._unwrap_shell("sh -c 'rtk ls'") == "rtk ls"
    assert codex._unwrap_shell("rtk ls") == "rtk ls"


# ---- MCP servers reach Codex through config.toml -----------------------------------------
def _spec():
    return {"serena": {"command": "/tmp/agent-home/.local/bin/serena",
                       "args": ["start-mcp-server", "--project", "/testbed", "--context", "codex"],
                       "env": {"PATH": "/tmp/agent-home/.local/bin:/usr/bin"}}}


def test_config_toml_writes_an_mcp_server_table_the_cli_validates():
    """The block is emitted before the array-of-tables skills entries (which must be last),
    with args as a TOML array and env as a sub-table. On 0.153.1 `--strict-config` accepts
    command/args/env/cwd/startup_timeout_sec and refuses an unknown key, so the acceptance
    is checked against the real CLI when one is installed."""
    from te_harness import codex
    t = codex.config_toml(routing=_DIRECT, model="gpt-5.6-terra", effort="high",
                          capabilities=("skills",), mcp_servers=_spec())
    i = t.index("[mcp_servers.serena]")
    assert t.index("[[skills.config]]") > i
    body = t[i:]
    assert 'command = "/tmp/agent-home/.local/bin/serena"' in body
    assert 'args = ["start-mcp-server", "--project", "/testbed", "--context", "codex"]' in body
    assert "startup_timeout_sec = 90" in body
    assert "[mcp_servers.serena.env]" in body and 'PATH = "/tmp/agent-home/.local/bin:/usr/bin"' in body
    # no servers -> no table at all: the baseline config is unchanged by this feature
    assert "mcp_servers" not in codex.config_toml(routing=_DIRECT, model="gpt-5.6-terra",
                                                  effort="high")
    import shutil, os, tempfile
    if shutil.which("codex") or os.environ.get("TE_CODEX_BIN"):
        home = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(__file__)))
        try:
            err = codex.validate_config(t, home=home)
            assert not (err or "").strip(), err
        finally:
            shutil.rmtree(home, ignore_errors=True)


def test_mcp_tool_calls_counts_completed_items_and_keeps_a_sample_of_what_it_cannot_read(tmp_path):
    """A configured server is not a used one. The reader counts `mcp_tool_call` items at
    completion, per server/tool; an item whose tool name sits somewhere unexpected is counted
    as unrecognised and ONE raw sample is kept.

    The shape below is the one codex-cli 0.153.1 emits (`item.completed`, item keys: id,
    type, server, tool, arguments, result, error, status). Codex also makes calls of its OWN,
    such as `list_mcp_resources` on server `codex`, which is why the count is split per
    server: `mcp_tool_calls` includes it, `serena_tool_calls` does not."""
    from te_harness import codex
    import json
    p = str(tmp_path / "t.jsonl")
    live_shape = {"id": "item_2", "type": "mcp_tool_call", "server": "codex",
                  "tool": "list_mcp_resources", "arguments": {},
                  "result": {"content": [{"type": "text", "text": "{\"resources\":[]}"}],
                             "structured_content": None},
                  "error": None, "status": "completed"}
    with open(p, "w") as fh:
        fh.write(json.dumps({"type": "item.started", "item": {"type": "mcp_tool_call", "server": "serena", "tool": "find_symbol"}}) + "\n")
        fh.write(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "serena", "tool": "find_symbol", "status": "completed"}}) + "\n")
        fh.write(json.dumps({"type": "item.completed", "item": live_shape}) + "\n")
        fh.write(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "invocation": {"server": "serena", "tool": "get_symbols_overview"}}}) + "\n")
        fh.write(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "weird": 1}}) + "\n")
        fh.write(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}}) + "\n")
    got = codex.mcp_tool_calls(p, server="serena")
    assert got["mcp_tool_calls"] == 3 and got["serena_tool_calls"] == 2
    assert got["mcp_tools_used"] == {"serena/find_symbol": 1, "serena/get_symbols_overview": 1,
                                     "codex/list_mcp_resources": 1}
    assert got["mcp_unrecognised_items"] == 1 and '"weird"' in got["mcp_item_sample"]
    none = codex.mcp_tool_calls(str(tmp_path / "missing.jsonl"), server="serena")
    assert none["mcp_tool_calls"] is None and none["serena_tool_calls"] is None


def test_the_codex_lane_hosts_every_scaffold_and_wires_an_mcp_one_through_the_config(
        tmp_path, monkeypatch):
    """The Codex lane refuses no scaffold: rtk runs in its own Codex mode, repomap writes
    AGENTS.md, and serena -- an MCP server -- is written into config.toml after setup,
    because `_start` writes the config before the scaffold exists. Both halves pinned: no
    scaffold name is refused at construction, and a scaffold that returns `mcp_servers`
    causes exactly one config rewrite carrying that spec."""
    from te_harness import swebench
    assert not hasattr(swebench, "_CODEX_UNAVAILABLE_SCAFFOLDS")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name in ("rtk", "repomap", "serena"):
        with pytest.raises(RuntimeError, match="nothing to bill"):      # past the scaffold gate
            swebench.CodexSwebenchRunner(agent="codex", scaffold=name)

    r = object.__new__(swebench.CodexSwebenchRunner)
    writes = []
    monkeypatch.setattr(swebench.ClaudeSwebenchRunner, "_apply_scaffold",
                        lambda self, name, task, rd: ({"arm_setup": "serena", "mcp_servers": _spec(),
                                                       "mcp_config": "/tmp/agent-home/scaffold.mcp.json"},
                                                      "/tmp/agent-home/scaffold.mcp.json"))
    r._write_config = lambda name: writes.append((name, r._mcp_servers))
    notes, cfg = r._apply_scaffold("c1", {"instance_id": "x"}, str(tmp_path))
    assert writes == [("c1", _spec())]
    assert "mcp_servers" not in notes                     # a dict never lands in row.json
    assert notes["mcp_wiring"] == "config.toml" and notes["mcp_servers_configured"] == ["serena"]
    assert cfg == "/tmp/agent-home/scaffold.mcp.json"
    # and a scaffold that asks for nothing rewrites nothing
    writes.clear()
    monkeypatch.setattr(swebench.ClaudeSwebenchRunner, "_apply_scaffold",
                        lambda self, name, task, rd: ({"arm_setup": "rtk"}, None))
    r._apply_scaffold("c1", {"instance_id": "x"}, str(tmp_path))
    assert writes == [] and r._mcp_servers is None
