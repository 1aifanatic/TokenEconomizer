import json
import pytest
from tokeneconomizer.plugin.config import load_config, unset_config, write_config

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
