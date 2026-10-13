"""Settings a person controls: each mechanism on or off, and its hyperparameters by hand.

Every setting is read on each hook call, so these drive real hook calls (`tests.test_live_hooks`)
with the setting in the environment or the repository's config file, and check that the
mechanism does what the setting says.
"""
import json
import os

import pytest

from te_live import guard
from te_live.config import LiveConfig, load_config, unset_config, write_config
from te_live.paths import repo_identity, repo_state_dir
from tests.test_live_hooks import (GREEN, _first, _turn, env, repo, run_session,  # noqa: F401
                                   stuck_calls, trap_session)


# ---- parsing ------------------------------------------------------------------------------
@pytest.mark.parametrize("key,on", [("inject", "envfacts"), ("untool", "propose"),
                                    ("spiral", "nudge")])
def test_each_mechanism_takes_a_plain_on_and_off(key, on):
    cfg = LiveConfig()
    assert cfg.set(key, "off") and getattr(cfg, key) == "off"
    assert cfg.set(key, "on") and getattr(cfg, key) == on
    assert cfg.set(key, False) and getattr(cfg, key) == "off"
    assert not cfg.set(key, "sometimes") and getattr(cfg, key) == "off"


def test_a_guard_threshold_is_typed_and_range_checked():
    cfg = LiveConfig()
    assert cfg.set("spiral.min_turns", "12") and cfg.spiral_thresholds["min_turns"] == 12
    assert cfg.set("spiral.backstop_mult", "2") and cfg.spiral_thresholds["backstop_mult"] == 2.0
    assert not cfg.set("spiral.min_turns", "12.5")           # a count is a whole number
    assert not cfg.set("spiral.stagnation", "1.5")           # a share is between 0 and 1
    assert not cfg.set("spiral.min_turns", "0")
    assert not cfg.set("spiral.no_such_threshold", "3")
    assert cfg.spiral_thresholds == {"min_turns": 12, "backstop_mult": 2.0}
    assert len(cfg.warnings) == 4


def test_set_and_unset_keep_thresholds_in_their_own_object(tmp_path):
    path = str(tmp_path / "config.json")
    write_config(path, {"spiral": "off", "spiral.min_turns": "12", "spiral.edit_churn": "6"})
    assert json.load(open(path)) == {"spiral": "off",
                                     "spiral_thresholds": {"edit_churn": 6, "min_turns": 12}}
    with pytest.raises(ValueError):
        write_config(path, {"spiral.min_turns": "-1"})        # nothing written on a rejection
    unset_config(path, ["spiral.min_turns", "spiral"])
    assert json.load(open(path)) == {"spiral_thresholds": {"edit_churn": 6}}
    unset_config(path, ["spiral.edit_churn"])
    assert json.load(open(path)) == {}
    with pytest.raises(ValueError):
        unset_config(path, ["spiral.nope"])


def test_the_repository_file_beats_the_global_one_and_the_environment_beats_both(tmp_path):
    root, repo_dir = tmp_path / "root", tmp_path / "repo"
    write_config(str(root / "config.json"), {"spiral_nudges": "3", "spiral.min_turns": "30"})
    write_config(str(repo_dir / "config.json"), {"spiral.min_turns": "10"})
    cfg = load_config(str(root), str(repo_dir), env={})
    assert cfg.spiral_nudges == 3 and cfg.spiral_thresholds == {"min_turns": 10}
    cfg = load_config(str(root), str(repo_dir), env={
        "TE_SPIRAL_NUDGES": "2", "TE_SPIRAL_THRESHOLDS": '{"min_turns": 8, "stagnation": 0.5}',
        "TE_INJECT_MAX_FACTS": "2", "TE_SPIRAL": "off"})
    assert cfg.spiral_nudges == 2 and cfg.inject_max_facts == 2 and cfg.spiral == "off"
    assert cfg.spiral_thresholds == {"min_turns": 8, "stagnation": 0.5}
    assert not cfg.warnings


def test_the_fact_caps_default_to_the_cores_own():
    from tokeneconomizer.mechanisms import envfacts
    cfg = LiveConfig()
    assert (cfg.inject_max_facts, cfg.inject_max_tokens) == (envfacts.MAX_FACTS,
                                                             envfacts.MAX_TOKENS)


# ---- thresholds: preset, then learned, then pinned ----------------------------------------
class _Store:
    def __init__(self, d):
        self.d = d

    def read(self, name):
        return self.d.get(name)


def test_a_pinned_threshold_wins_over_the_preset_and_over_what_was_learned():
    store = _Store({"envelope.json": {"resolved_turns": [6, 7, 8, 8, 9]}})
    learned = guard.thresholds(guard.load_envelope(store))
    assert learned["min_turns"] == 8                     # learned below the preset's 20
    tracker = guard.load_envelope(store, overrides={"min_turns": 30, "backstop_mult": 4.0})
    thr = guard.thresholds(tracker, {"min_turns": 30, "backstop_mult": 4.0})
    assert thr["min_turns"] == 30
    assert thr["backstop_turns"] == pytest.approx(tracker.envelope * 4.0)


def test_the_envelope_settings_reach_the_tracker():
    store = _Store({"envelope.json": {"resolved_turns": [6, 8]}})
    assert guard.load_envelope(store).envelope is None   # two samples: not trusted by default
    tracker = guard.load_envelope(store, overrides={"envelope_min_samples": 2,
                                                    "envelope_margin": 2.0, "envelope_pct": 50})
    assert tracker.envelope == pytest.approx(7 * 2.0)


def test_base_thresholds_show_the_preset_with_the_pinned_values_on_top():
    thr = guard.base_thresholds("aggressive", {"min_turns": 9})
    assert thr["min_turns"] == 9 and thr["backstop_mult"] == 1.8
    assert "restart_budget" not in thr                   # not used by the live guard


# ---- end to end, through the hooks --------------------------------------------------------
def _warm(env, repo):
    for k in range(5):
        _turn(env, repo, "s", f"g{k}", GREEN)


def _messages(outs):
    return [o.get("systemMessage") or "" for o in outs]


def test_a_guard_turned_off_in_the_repository_file_never_intervenes(env, repo):
    _warm(env, repo)
    path = os.path.join(repo_state_dir(repo_identity(repo, env), "claude", env), "..",
                        "config.json")
    write_config(os.path.normpath(path), {"spiral": "off"})
    assert all(o == {} for o in _turn(env, repo, "s", "stuck", stuck_calls(40)))


def test_more_redirects_are_given_before_the_notice_when_asked_for(env, repo):
    _warm(env, repo)
    outs = _turn(dict(env, TE_SPIRAL_NUDGES="2"), repo, "s", "stuck", stuck_calls(60))
    msgs = _messages(outs)
    nudges = [i for i, m in enumerate(msgs) if "nudged" in m]
    notice = _first(msgs, lambda m: "still looks stuck" in m)
    assert len(nudges) == 2 and notice is not None and notice > nudges[-1]


def test_a_higher_min_turns_holds_the_guard_back(env, repo):
    _warm(env, repo)
    outs = _turn(dict(env, TE_SPIRAL_THRESHOLDS='{"min_turns": 80}'), repo, "s", "stuck",
                 stuck_calls(40))
    assert all(o == {} for o in outs)                    # default thresholds nudge this turn


def test_injection_off_and_a_tight_fact_cap_keep_the_context_clean(env, repo, tmp_path):
    for i in range(4):
        run_session(env, repo, f"s{i}", trap_session(str(tmp_path / f"s{i}.jsonl"), repo, i))
    off = run_session(dict(env, TE_INJECT="off"), repo, "s4",
                      trap_session(str(tmp_path / "s4.jsonl"), repo, 4))
    assert off == {}                                     # the default injects here
    tight = dict(env, TE_INJECT_MAX_TOKENS="5")          # smaller than the block's header
    run_session(tight, repo, "s5", trap_session(str(tmp_path / "s5.jsonl"), repo, 5))
    after = run_session(tight, repo, "s6", trap_session(str(tmp_path / "s6.jsonl"), repo, 6))
    assert "hookSpecificOutput" not in after
