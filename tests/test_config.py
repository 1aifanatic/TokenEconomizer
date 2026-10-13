"""Config: from_args maps the kept flags onto the three mechanism switches."""
from types import SimpleNamespace

from tokeneconomizer import EconomizerConfig
from tokeneconomizer.config import InjectionConfig, ToolingConfig, SpiralConfig


def test_defaults_all_off():
    cfg = EconomizerConfig()
    assert cfg.injection.enabled is False
    assert cfg.tooling.enabled is False
    assert cfg.spiral.enabled is False


def test_from_args_generic_trimech():
    args = SimpleNamespace(
        inject=True, propose_every=5,
        untool_evidence=3, untool_evidence_reaudit=12, untool_judge=True,
        spiral_guard=True, spiral_judge=True, spiral_nudge=True)
    cfg = EconomizerConfig.from_args(args)
    assert cfg.injection.enabled and cfg.injection.propose_every == 5
    assert cfg.tooling.evidence == 3 and cfg.tooling.judge is True
    assert cfg.tooling.enabled is True
    assert cfg.spiral.guard and cfg.spiral.judge and cfg.spiral.nudge
    assert cfg.spiral.enabled is True


def test_from_args_codex():
    args = SimpleNamespace(codex_untool=True, codex_spiral=True)
    cfg = EconomizerConfig.from_args(args)
    assert cfg.tooling.codex_untool is True and cfg.tooling.enabled is True
    assert cfg.spiral.codex_spiral is True and cfg.spiral.enabled is True
    assert cfg.injection.enabled is False


def test_propose_every_defaults_when_zero():
    args = SimpleNamespace(inject=True, propose_every=0)
    cfg = EconomizerConfig.from_args(args)
    assert cfg.injection.propose_every == InjectionConfig.propose_every


def test_partial_args_default_off():
    # A sparse args object must not crash: unknown attrs default off.
    cfg = EconomizerConfig.from_args(SimpleNamespace())
    assert not cfg.injection.enabled and not cfg.tooling.enabled and not cfg.spiral.enabled
