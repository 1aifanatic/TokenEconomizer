"""The shared environment-fact layer: vocabulary plug-in, composition with the paid
proposer, and the arms that carry each profile."""
import os

from tokeneconomizer.mechanisms import envfacts as ef
from tokeneconomizer.mechanisms import codex_injection as ci
from tokeneconomizer.mechanisms.injection import PrefixStage, ComposedDistiller
from tokeneconomizer.config import InjectionConfig, needs_optimizer
from tokeneconomizer.adapters.swebench import SwebenchAdapter
from fixtures import write_codex_transcript

BASE = "/opt/env/base/bin/python"
PROJ = "/opt/env/proj/bin/python"


def _cmd(command, output, code):
    return {"type": "command_execution", "command": f'/bin/bash -lc "{command}"',
            "aggregated_output": output, "exit_code": code}


def _edit(path):
    return {"type": "file_change", "changes": [{"path": path, "kind": "update"}]}


def _broken(tmp_path, i):
    p = str(tmp_path / f"t{i}.jsonl")
    write_codex_transcript(p, [
        _edit(f"pkg/mod{i}.py"),
        _cmd("python -m pytest tests/test_a.py -q", f"{BASE}: No module named pytest", 1),
        _cmd(f"{PROJ} -m pytest tests/test_a.py -q", "1 passed", 0)])
    return p


# ---- the Codex module is a shim over the shared layer ------------------------------
def test_codex_module_reexports_the_shared_layer():
    assert ci.EnvFactsDistiller is ef.EnvFactsDistiller
    assert ci.observe_task is ef.observe_task
    assert ci.CODEX_DEFAULT_PROFILE == "envfacts+distiller"   # since 2026-09-12
    assert ci.CODEX_FREE_PROFILE == "envfacts"


# ---- vocabulary plug-in ----------------------------------------------------------------
def test_node_vocabulary_reads_a_node_repository():
    obs = ef.classify_command("npx jest tests/ --ci", vocab=ef.NODE_WEB)
    assert [(o.intent, o.form) for o in obs] == [("run_tests", "npx jest")]
    obs = ef.classify_command("node scripts/build.js && npm test", vocab=ef.NODE_WEB)
    assert [(o.intent, o.form) for o in obs] == [("run_interpreter", "node"),
                                                 ("run_tests", "npm test")]
    obs = ef.classify_command("npm install left-pad", vocab=ef.NODE_WEB)
    assert obs[0].intent == "fetch_remote"
    # the Python vocabulary does not know jest; the Node one does not know pytest
    assert ef.classify_command("npx jest", vocab=ef.PYTHON_GIT) == [] or \
        ef.classify_command("npx jest", vocab=ef.PYTHON_GIT)[0].intent != "run_tests"
    assert ef.classify_command("pytest -q", vocab=ef.NODE_WEB) == []
    assert ef.error_class("Error: Cannot find module 'left-pad'") == "module_missing"
    assert ef.error_class("connect ECONNREFUSED 127.0.0.1:443") == "connection_refused"


def test_adapters_supply_a_vocabulary():
    assert SwebenchAdapter().env_vocabulary().name == "python-git"
    from tokeneconomizer.adapters.webbench import WebBenchAdapter
    assert WebBenchAdapter.env_vocabulary(None).name == "node-web"


def test_distiller_records_its_vocabulary(tmp_path):
    d = ef.EnvFactsDistiller("acme/widget", vocab=ef.NODE_WEB)
    d.update(_broken(tmp_path, 0), iid="w-0")
    d.save(str(tmp_path))
    import json
    saved = json.load(open(os.path.join(str(tmp_path), ef.STATE_SUBDIR, "acme__widget.json")))
    assert saved["vocab"] == "node-web"
    assert d.debug_row()["_vocab"] == "node-web"


# ---- composition with the paid proposer -----------------------------------------------
class _RecordingRunner:
    """A proposer stand-in: records every prompt, authors nothing, bills 7 eff tokens."""
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, model="agent"):
        self.prompts.append(prompt)
        return '{"extractors": [], "facts": []}', 7.0


def test_composed_profile_renders_both_layers_and_briefs_the_proposer(tmp_path):
    runner = _RecordingRunner()
    cfg = InjectionConfig(enabled=True, profile="envfacts+distiller", propose_every=1)
    stage = PrefixStage(cfg, "acme/widget", str(tmp_path), fresh=True, runner=runner,
                        agent="codex", adapter=SwebenchAdapter())
    assert isinstance(stage.distiller, ComposedDistiller)
    total_opt = 0
    for i in range(6):
        block = stage.render()
        rd = str(tmp_path / f"run{i}"); os.makedirs(rd)
        os.replace(_broken(tmp_path, i), os.path.join(rd, "transcript.jsonl"))
        opt, reason = stage.after_task({"instance_id": f"w-{i}"}, {}, rd, {"resolved": True})
        total_opt += opt
        assert reason == "envfacts+manual-distill"
    # the free layer formed and renders first
    assert block.startswith(ef.HEADER) and PROJ in block
    # the proposer was called every task (cadence 1) and billed through the ledger seam
    assert len(runner.prompts) >= 5 and total_opt == 7.0 * len(runner.prompts)
    # once the environment fact was injectable, the proposer was told not to restate it
    assert any("ALREADY injected" in p and "fails here" in p for p in runner.prompts[3:])
    # telemetry names the profile and carries both layers' facts
    srow = {"resolved": True}
    rd = str(tmp_path / "run_last"); os.makedirs(rd)
    os.replace(_broken(tmp_path, 9), os.path.join(rd, "transcript.jsonl"))
    stage.after_task({"instance_id": "w-9"}, {}, rd, srow)
    assert srow["manual_facts"]["_profile"] == "envfacts+distiller"
    assert any(k.startswith("form_repair:") for k in srow["manual_facts"])


def test_envfacts_alone_still_needs_no_runner(tmp_path):
    cfg = InjectionConfig(enabled=True, profile="envfacts")
    stage = PrefixStage(cfg, "acme/widget", str(tmp_path), fresh=True, runner=None,
                        agent="codex", adapter=SwebenchAdapter())
    assert stage.render() == "" and stage.reason == "envfacts"


# ---- arms ------------------------------------------------------------------------------
def test_codex_arms_carry_the_intended_profiles():
    """Since 2026-09-12 the Codex injection DEFAULT is the composed profile.

    The free layer's value on this benchmark came from the inactive-project-environment
    defect; with that fixed it abstains, so `codex-inject` carries the paid proposer and
    the LLM-free variant is the named ablation `codex-inject-envfacts`.
    """
    from tokeneconomizer.arms import ARMS, ARM_ORDER
    from tokeneconomizer.mechanisms import codex_injection as ci
    default = ARMS["codex-inject"].cfg().injection
    assert default.profile == ci.CODEX_DEFAULT_PROFILE == "envfacts+distiller"
    assert default.paid and default.propose_every == 10
    assert needs_optimizer(ARMS["codex-inject"].cfg()) is True
    # the untool stack follows the default; the ablation does not
    assert ARMS["codex-inject-untool"].cfg().injection.profile == "envfacts+distiller"
    assert ARMS["codex-inject-untool"].cfg().tooling.codex_untool is True
    free = ARMS["codex-inject-envfacts"].cfg().injection
    assert free.profile == ci.CODEX_FREE_PROFILE == "envfacts" and not free.paid
    assert needs_optimizer(ARMS["codex-inject-envfacts"].cfg()) is False
    # the pre-2026-09-12 name of the default still resolves to the same configuration
    assert ARMS["codex-inject-distiller"].cfg().injection == default
    for name in ("codex-inject", "codex-inject-untool", "codex-inject-envfacts",
                 "codex-inject-distiller"):
        assert ARMS[name].mode == "manual4" and name in ARM_ORDER
