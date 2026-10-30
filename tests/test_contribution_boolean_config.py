import pytest
from tokeneconomizer.plugin.config import LiveConfig

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
