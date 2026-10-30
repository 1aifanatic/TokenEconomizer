import pytest
from tokeneconomizer.plugin.config import LiveConfig

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
