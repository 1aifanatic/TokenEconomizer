import pytest
from tokeneconomizer.plugin.config import LiveConfig

@pytest.mark.parametrize("key,value", [("loopguard.judge_confidence", "nan"), ("loopguard.envelope_margin", "inf"), ("loopguard.min_turns", float("inf")), ("repomap_tokens", float("inf"))])
def test_nonfinite_config_is_rejected(key, value):
    cfg = LiveConfig()
    before = cfg.as_dict()
    assert cfg.set(key, value) is False
    assert cfg.as_dict() == before
    assert cfg.warnings
