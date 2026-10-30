import pytest
from tokeneconomizer.plugin.config import write_config, unset_config, load_config

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
