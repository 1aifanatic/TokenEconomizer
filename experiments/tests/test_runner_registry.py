"""Every runner in a RUNNERS registry must accept what its make_runner forwards.

This is the gap a two-lane blocker went through. `make_runner` fans out to a concrete
runner class:

    RUNNERS[agent](agent=agent, scaffold=scaffold, **kwargs)      # te_harness.swebench

and `CopilotSwebenchRunner.__init__` did not take `scaffold`, so every Copilot SWE-bench
cell raised TypeError at construction -- before task 0, for every arm including `baseline`
where the value is None. Two of the sweep's eight lanes could not run at all.

`run_experiment.load_runner` already guards against exactly this, but it inspects the
FACTORY's signature, and a factory declared `def make_runner(agent=None, **kwargs)` accepts
anything by definition -- so the guard passes and the concrete class raises. The check has
to look at the classes the registry actually dispatches to.

Kept to `inspect`: no CLI, no Docker, no network, no credentials, so it runs anywhere the
rest of the suite does. That is the point -- the whole failure was a signature mismatch, and
the offline smoke test could not see it because RUNNER=stub never reaches these classes.
"""
import inspect

import pytest


def _registries():
    """(module_name, RUNNERS, forwarded_kwargs) for each harness that has one."""
    out = []
    for mod_name, forwarded in (("te_harness.swebench", ("agent", "scaffold", "model")),
                                ("te_harness.webbench", ("agent", "model"))):
        try:
            mod = __import__(mod_name, fromlist=["RUNNERS"])
        except Exception as exc:                       # harness extra not installed
            pytest.skip(f"{mod_name} not importable: {exc}")
        out.append((mod_name, mod.RUNNERS, forwarded))
    return out


def _accepts(func, name):
    sig = inspect.signature(func)
    if name in sig.parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def test_every_runner_accepts_what_make_runner_forwards():
    for mod_name, runners, forwarded in _registries():
        assert runners, f"{mod_name}.RUNNERS is empty"
        for agent, cls in sorted(runners.items()):
            for kw in forwarded:
                assert _accepts(cls.__init__, kw), (
                    f"{mod_name}.RUNNERS[{agent!r}] is {cls.__name__}, whose __init__ does "
                    f"not accept {kw!r} -- but make_runner forwards it, so every cell on "
                    f"this agent would raise TypeError at construction, before task 0.")


def test_scaffold_bearing_arms_have_a_runner_that_takes_a_scaffold():
    """An arm that names a scaffold must be constructible on every SWE-bench agent.

    The scaffold arms are SWE-bench-only (SWEEP_ARMS_WEBBENCH_UNAVAILABLE), and a runner
    that silently ignored `scaffold` would run NATIVE while filing rows under the
    scaffold's name -- which is the failure `load_runner` refuses loudly for, and which
    this keeps refusing for the classes rather than the factory.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import run_experiment as rx

    scaffold_arms = [n for n, a in rx.ARMS.items() if getattr(a, "scaffold", None)]
    assert scaffold_arms, "expected at least one scaffold-bearing arm in the registry"

    mod = __import__("te_harness.swebench", fromlist=["RUNNERS"])
    for agent, cls in sorted(mod.RUNNERS.items()):
        assert _accepts(cls.__init__, "scaffold"), (
            f"arms {scaffold_arms} carry a scaffold, but "
            f"te_harness.swebench.RUNNERS[{agent!r}] ({cls.__name__}) cannot take one")


def test_webbench_runners_can_take_a_scaffold_they_are_asked_to_host():
    """Web-Bench hosts `rtk` (see te_harness.webbench.WEBBENCH_SCAFFOLDS), so both of its
    runners must accept `scaffold` and be able to build a profile for it.

    A runner that silently ignored `scaffold` would run NATIVE while filing rows under the
    arm's name -- the same failure the SWE-bench check above exists for.
    """
    from te_harness.webbench import RUNNERS, WEBBENCH_SCAFFOLDS

    assert WEBBENCH_SCAFFOLDS, "expected Web-Bench to host at least one baseline"
    for agent, cls in sorted(RUNNERS.items()):
        assert _accepts(cls.__init__, "scaffold"), (
            f"te_harness.webbench.RUNNERS[{agent!r}] ({cls.__name__}) cannot take a "
            f"scaffold, but {sorted(WEBBENCH_SCAFFOLDS)} are planned there")
        assert hasattr(cls, "scaffold_profile_for"), (
            f"{cls.__name__} takes a scaffold but declares no scaffold_profile_for(home); "
            f"Web-Bench's HOME is per task-run, so the profile cannot be a class attribute")


def test_webbench_refuses_a_scaffold_it_cannot_install():
    """`repomap`/`serena` need a container or a Python symbol map. Asking for one must fail
    LOUDLY at construction, not produce native rows wearing the baseline's name."""
    import pytest as _pytest
    from te_harness import webbench

    for bad in ("repomap", "serena"):
        with _pytest.raises(RuntimeError, match="not wired for Web-Bench"):
            webbench.make_runner(agent="claude", scaffold=bad)


def test_each_runner_declares_a_scaffold_profile():
    """A SWE-bench runner that takes a scaffold needs the profile the scaffold installs against.

    `scaffold_profile` is what makes an install agent-correct -- the instructions file,
    the native navigation tool names, and the patch_excludes subtracted from the graded
    diff. A runner accepting `scaffold=` without one would install a Claude-shaped scaffold
    into a Copilot container.
    """
    mod = __import__("te_harness.swebench", fromlist=["RUNNERS"])
    for agent, cls in sorted(mod.RUNNERS.items()):
        profile = getattr(cls, "scaffold_profile", None)
        assert profile is not None, (
            f"{cls.__name__} accepts a scaffold but declares no scaffold_profile")
        assert getattr(profile, "instructions_file", None), (
            f"{cls.__name__}.scaffold_profile has no instructions_file")
        assert getattr(profile, "patch_excludes", None), (
            f"{cls.__name__}.scaffold_profile declares no patch_excludes, so scaffold "
            f"artifacts would be graded as part of the agent's fix")


def test_the_registries_carry_exactly_the_three_agent_labels():
    """`claude`, `copilot`, `codex` -- one label per agent, each billing its own vendor's
    API natively, and the same three on both domains. Codex is a first-class Web-Bench
    agent too."""
    from te_harness import swebench, webbench
    assert sorted(swebench.RUNNERS) == sorted(webbench.RUNNERS) == ["claude", "codex",
                                                                    "copilot"]
    assert swebench.RUNNERS["codex"] is swebench.CodexSwebenchRunner
    assert webbench.RUNNERS["codex"] is webbench.CodexWebBenchRunner
    assert issubclass(webbench.CodexWebBenchRunner, webbench.WebBenchRunner)


@pytest.mark.parametrize("label", ["claude-code", "copilot-cli", "openai", "nope"])
def test_an_unregistered_label_is_refused_not_mapped(label, monkeypatch):
    """A label with no entry is an error rather than a fallback: rows filed under the wrong
    agent are wrong numbers, not a crash. Refused before any runner is built."""
    from te_harness import swebench, webbench
    monkeypatch.delenv("TE_SCAFFOLD", raising=False)
    with pytest.raises(RuntimeError, match="no SWE-bench runner"):
        swebench.make_runner(agent=label)
    with pytest.raises(RuntimeError, match="no Web-Bench runner"):
        webbench.make_runner(agent=label)


def test_the_webbench_adapter_carries_a_node_vocabulary():
    """The core's Web-Bench adapter describes a Node project, whichever agent runs it."""
    from te_bench.adapters.webbench import WebBenchAdapter
    assert WebBenchAdapter.env_vocabulary(None).name == "node-web"
