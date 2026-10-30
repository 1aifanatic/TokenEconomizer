"""Settings a person controls: each mechanism on or off, and its hyperparameters by hand.

Every setting is read on each hook call, so these drive real hook calls (`tests.test_live_hooks`)
with the setting in the environment or the repository's config file, and check that the
mechanism does what the setting says.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.plugin.config import LiveConfig, load_config, unset_config, write_config
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
from tests.test_live_hooks import (GREEN, _first, _turn, env, repo, run_session,  # noqa: F401
                                   stuck_calls, trap_session)


# ---- parsing ------------------------------------------------------------------------------
@pytest.mark.parametrize("key,on", [("microskills", "envfacts+distiller"), ("toolprune", "auto"),
                                    ("loopguard", "nudge")])
def test_each_mechanism_takes_a_plain_on_and_off(key, on):
    cfg = LiveConfig()
    assert cfg.set(key, "off") and getattr(cfg, key) == "off"
    assert cfg.set(key, "on") and getattr(cfg, key) == on
    assert cfg.set(key, False) and getattr(cfg, key) == "off"
    assert not cfg.set(key, "sometimes") and getattr(cfg, key) == "off"


def test_a_guard_threshold_is_typed_and_range_checked():
    cfg = LiveConfig()
    assert cfg.set("loopguard.min_turns", "12") and cfg.loopguard_thresholds["min_turns"] == 12
    assert (cfg.set("loopguard.backstop_mult", "2")
            and cfg.loopguard_thresholds["backstop_mult"] == 2.0)
    assert not cfg.set("loopguard.min_turns", "12.5")        # a count is a whole number
    assert not cfg.set("loopguard.stagnation", "1.5")        # a share is between 0 and 1
    assert not cfg.set("loopguard.min_turns", "0")
    assert not cfg.set("loopguard.no_such_threshold", "3")
    assert cfg.loopguard_thresholds == {"min_turns": 12, "backstop_mult": 2.0}
    assert len(cfg.warnings) == 4


def test_set_and_unset_keep_thresholds_in_their_own_object(tmp_path):
    path = str(tmp_path / "config.json")
    write_config(path, {"loopguard": "off", "loopguard.min_turns": "12",
                        "loopguard.edit_churn": "6"})
    assert json.load(open(path)) == {"loopguard": "off",
                                     "loopguard_thresholds": {"edit_churn": 6, "min_turns": 12}}
    with pytest.raises(ValueError):
        write_config(path, {"loopguard.min_turns": "-1"})     # nothing written on a rejection
    unset_config(path, ["loopguard.min_turns", "loopguard"])
    assert json.load(open(path)) == {"loopguard_thresholds": {"edit_churn": 6}}
    unset_config(path, ["loopguard.edit_churn"])
    assert json.load(open(path)) == {}
    with pytest.raises(ValueError):
        unset_config(path, ["loopguard.nope"])


def test_the_repository_file_beats_the_global_one_and_the_environment_beats_both(tmp_path):
    root, repo_dir = tmp_path / "root", tmp_path / "repo"
    write_config(str(root / "config.json"), {"loopguard_nudges": "3",
                                             "loopguard.min_turns": "30"})
    write_config(str(repo_dir / "config.json"), {"loopguard.min_turns": "10"})
    cfg = load_config(str(root), str(repo_dir), env={})
    assert cfg.loopguard_nudges == 3 and cfg.loopguard_thresholds == {"min_turns": 10}
    cfg = load_config(str(root), str(repo_dir), env={
        "TE_LOOPGUARD_NUDGES": "2",
        "TE_LOOPGUARD_THRESHOLDS": '{"min_turns": 8, "stagnation": 0.5}',
        "TE_MICROSKILLS_MAX_FACTS": "2", "TE_LOOPGUARD": "off"})
    assert cfg.loopguard_nudges == 2 and cfg.microskills_max_facts == 2
    assert cfg.loopguard == "off"
    assert cfg.loopguard_thresholds == {"min_turns": 8, "stagnation": 0.5}
    assert not cfg.warnings


def test_the_names_before_the_rename_still_work(tmp_path):
    """`inject`, `untool`, `spiral` and their settings and variables are read as the new names;
    a file holding both spellings takes the new one; writing a setting stores its new name and
    drops the old spelling; unsetting by either name removes both."""
    root, repo_dir = tmp_path / "root", tmp_path / "repo"
    os.makedirs(root)
    with open(root / "config.json", "w") as fh:
        json.dump({"loopguard": "off", "spiral": "guard", "inject_max_facts": 3,
                   "untool": "auto", "propose_every": 7, "spiral_aggr": "aggressive",
                   "spiral_thresholds": {"min_turns": 9}}, fh)
    cfg = load_config(str(root), str(repo_dir), env={
        "TE_SPIRAL_NUDGES": "2", "TE_UNTOOL_EVIDENCE": "4", "TE_INJECT": "off"})
    assert cfg.loopguard == "off"                       # the new name wins in one file
    assert (cfg.microskills_max_facts, cfg.toolprune, cfg.microskills_propose_every,
            cfg.loopguard_aggr) == (3, "auto", 7, "aggressive")
    assert cfg.loopguard_thresholds == {"min_turns": 9}
    assert (cfg.loopguard_nudges, cfg.toolprune_evidence, cfg.microskills) == (2, 4, "off")
    assert not cfg.warnings
    # a new variable beats its old spelling
    assert load_config(str(root), env={"TE_SPIRAL": "nudge",
                                       "TE_LOOPGUARD": "stop"}).loopguard == "stop"
    assert LiveConfig().set("spiral", "guard") and LiveConfig().set("loopguard", "guard")
    path = str(root / "config.json")
    write_config(path, {"spiral": "nudge", "spiral.edit_churn": "5"})
    written = json.load(open(path))
    assert written["loopguard"] == "nudge" and "spiral" not in written
    assert written["loopguard_thresholds"] == {"min_turns": 9, "edit_churn": 5}
    assert "spiral_thresholds" not in written
    unset_config(path, ["untool", "inject_max_facts", "spiral.min_turns"])
    written = json.load(open(path))
    assert "untool" not in written and "toolprune" not in written
    assert "inject_max_facts" not in written
    assert written["loopguard_thresholds"] == {"edit_churn": 5}


def test_the_fact_caps_default_to_the_cores_own():
    from tokeneconomizer.microskills import envfacts
    cfg = LiveConfig()
    assert (cfg.microskills_max_facts, cfg.microskills_max_tokens) == (envfacts.MAX_FACTS,
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
    off = run_session(dict(env, TE_MICROSKILLS="off"), repo, "s4",
                      trap_session(str(tmp_path / "s4.jsonl"), repo, 4))
    assert off == {}                                     # `envfacts` injects here
    tight = dict(env, TE_INJECT_MAX_TOKENS="5")          # smaller than the block's header
    run_session(tight, repo, "s5", trap_session(str(tmp_path / "s5.jsonl"), repo, 5))
    after = run_session(tight, repo, "s6", trap_session(str(tmp_path / "s6.jsonl"), repo, 6))
    assert "hookSpecificOutput" not in after


def test_loopguard_stop_is_still_read_and_acts_as_nudge_with_a_warning(tmp_path):
    """`stop` once stopped a headless turn on Claude Code and Pi; the plugin no longer stops a
    turn on any agent, so an existing `stop` (or the old `spiral=guard`) keeps working as the
    redirect-then-notice ladder, and says so."""
    for env in ({"TE_LOOPGUARD": "stop"}, {"TE_SPIRAL": "guard"}):
        cfg = load_config(str(tmp_path), env=env)
        assert cfg.loopguard == "stop"
        assert any("loopguard=stop acts as nudge" in w for w in cfg.warnings)
    assert not load_config(str(tmp_path), env={"TE_LOOPGUARD": "nudge"}).warnings


def test_the_quiet_window_before_a_deny_list_is_written_may_be_zero():
    cfg = LiveConfig()
    assert cfg.toolprune_quiet_minutes == 60
    assert cfg.set("toolprune_quiet_minutes", "0") and cfg.toolprune_quiet_minutes == 0
    assert not cfg.set("toolprune_quiet_minutes", "-5")
    assert not cfg.set("toolprune_evidence", "0")             # other numbers still start at 1
    assert load_config("/nonexistent", env={"TE_TOOLPRUNE_QUIET_MINUTES": "15"}
                       ).toolprune_quiet_minutes == 15


@pytest.mark.parametrize("value", ["typo", "", None, 2, []])
@pytest.mark.parametrize("key", ["enabled", "shadow"])
def test_invalid_boolean_preserves_setting(key, value):
    cfg = LiveConfig(shadow=True)
    assert cfg.set(key, value) is False
    assert getattr(cfg, key) is True
    assert cfg.warnings

@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (" on ", True), ("off", False), (1, True), (0, False)])
def test_valid_booleans(value, expected):
    cfg = LiveConfig()
    assert cfg.set("enabled", value)
    assert cfg.enabled is expected


@pytest.mark.parametrize('spelling', ['loopguard.min_turns', 'spiral.min_turns'])
@pytest.mark.parametrize('operation', ['one', 'all', 'set'])
def test_threshold_mutations_remove_flat_aliases(tmp_path, spelling, operation):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({spelling: 99, 'loopguard.error_outputs': 7, 'microskills': 'off'}))
    if operation == 'one':
        result = unset_config(str(path), ['loopguard.min_turns'])
    elif operation == 'all':
        result = unset_config(str(path), ['loopguard_thresholds'])
    else:
        result = write_config(str(path), {'loopguard.min_turns': 23})
    cfg = load_config(str(tmp_path), env={})
    assert spelling not in result
    assert cfg.loopguard_thresholds.get('min_turns') == (23 if operation == 'set' else None)
    assert cfg.loopguard_thresholds.get('error_outputs') == (None if operation == 'all' else 7)
    assert result['microskills'] == 'off'
    if operation == 'set':
        unset_config(str(path), ['loopguard.min_turns'])
        assert 'min_turns' not in load_config(str(tmp_path), env={}).loopguard_thresholds


def test_table_update_removes_only_overridden_flat_keys(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'spiral.min_turns': 99, 'loopguard.error_outputs': 7}))
    result = write_config(str(path), {'loopguard_thresholds': {'min_turns': 23}})
    assert 'spiral.min_turns' not in result
    assert load_config(str(tmp_path), env={}).loopguard_thresholds == {'min_turns': 23, 'error_outputs': 7}


@pytest.mark.parametrize('raw', [b'{"microskills":"on",', b'[]', b'null', b'\xff'])
@pytest.mark.parametrize('operation', ['set', 'unset'])
def test_invalid_existing_config_is_not_overwritten(tmp_path, raw, operation):
    path = tmp_path / 'config.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        if operation == 'set':
            write_config(str(path), {'loopguard': 'off'})
        else:
            unset_config(str(path), ['loopguard'])
    assert path.read_bytes() == raw
    # Hooks still fail open when reading a malformed configuration.
    assert load_config(str(tmp_path), env={}).loopguard == 'nudge'


def test_missing_config_can_be_created(tmp_path):
    assert write_config(str(tmp_path / 'config.json'), {'loopguard': 'off'}) == {'loopguard': 'off'}


@pytest.mark.parametrize('key', ['toolprune_evidence', 'microskills_propose_every', 'loopguard.min_turns'])
@pytest.mark.parametrize('value', [1.5, 2.9, True])
def test_integer_settings_reject_lossy_values(key, value):
    cfg = LiveConfig()
    before = cfg.as_dict()
    assert cfg.set(key, value) is False
    assert cfg.as_dict() == before
    assert cfg.warnings

@pytest.mark.parametrize('key', ['toolprune_evidence', 'loopguard.min_turns'])
@pytest.mark.parametrize('value', [3, 3.0, '3'])
def test_whole_numbers_still_work(key, value):
    cfg = LiveConfig()
    assert cfg.set(key, value)
    assert (cfg.loopguard_thresholds['min_turns'] if '.' in key else cfg.toolprune_evidence) == 3


_CONFIG_FIRST_WRITER = '''
import sys, time
from pathlib import Path
from tokeneconomizer.plugin import config
path, ready, release = map(Path, sys.argv[1:])
original = config._read
def read(*args, **kwargs):
    result = original(*args, **kwargs)
    ready.touch()
    deadline = time.monotonic() + 10
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test release timed out")
        time.sleep(0.01)
    return result
config._read = read
config.write_config(str(path), {"microskills": "on"})
'''

@pytest.mark.parametrize('operation', ['set', 'unset'])
def test_concurrent_config_edits_preserve_both_updates(tmp_path, operation):
    path, ready, release = [tmp_path / name for name in ('config.json', 'ready', 'release')]
    path.write_text(json.dumps({'loopguard': 'off'}))
    first = subprocess.Popen([sys.executable, '-c', _CONFIG_FIRST_WRITER, str(path), str(ready), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert first.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        edit = 'config.write_config(sys.argv[1], {"toolprune": "off"})' if operation == 'set' else 'config.unset_config(sys.argv[1], ["loopguard"])'
        second = subprocess.Popen([sys.executable, '-c', 'import sys; from tokeneconomizer.plugin import config; ' + edit, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            second.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        release.touch()
        _, error = first.communicate(timeout=10)
        assert first.returncode == 0, error
        _, error = second.communicate(timeout=10)
        assert second.returncode == 0, error
        result = json.loads(path.read_text())
        assert result['microskills'] == 'envfacts+distiller'
        if operation == 'set':
            assert result['toolprune'] == 'off'
        else:
            assert 'loopguard' not in result
    finally:
        release.touch()
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate()


def test_config_lock_timeout_preserves_file(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from tokeneconomizer.plugin import config
    from tokeneconomizer.plugin.store import Store
    path = tmp_path / 'config.json'
    path.write_text('{"loopguard":"off"}')
    @contextmanager
    def unavailable(*args, **kwargs):
        yield False
    monkeypatch.setattr(Store, 'locked', unavailable)
    with pytest.raises(ValueError, match='busy'):
        config.write_config(str(path), {'toolprune': 'off'})
    assert path.read_text() == '{"loopguard":"off"}'


@pytest.mark.parametrize("table", ["loopguard_thresholds", "spiral_thresholds"])
@pytest.mark.parametrize("value", [None, [], [1], "invalid"])
@pytest.mark.parametrize("operation", ["set", "unset"])
def test_malformed_threshold_table_is_refused_without_rewriting(tmp_path, table, value, operation):
    path = tmp_path / "config.json"
    original = json.dumps({table: value, "shadow": True})
    path.write_text(original)
    with pytest.raises(ValueError, match=table):
        if operation == "set":
            write_config(str(path), {"loopguard.min_turns": 12})
        else:
            unset_config(str(path), ["loopguard.min_turns"])
    assert path.read_text() == original
    # Removing the whole setting remains a way to repair the invalid table.
    unset_config(str(path), [table])
    assert json.loads(path.read_text()) == {"shadow": True}
    write_config(str(path), {"loopguard.min_turns": 12})
    assert load_config(str(tmp_path), env={}).loopguard_thresholds == {"min_turns": 12}
