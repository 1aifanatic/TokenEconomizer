from types import SimpleNamespace
import pytest
from tokeneconomizer.plugin import repomap
from tokeneconomizer.plugin.config import LiveConfig
from tokeneconomizer.plugin.store import Store


def context(tmp_path):
    return SimpleNamespace(store=Store(tmp_path / 'host'), repo=SimpleNamespace(root=str(tmp_path)),
                           config=LiveConfig(repomap_tokens=4096, repomap_python='chosen-python'), env={})

@pytest.mark.parametrize('python', [None, 'python'])
def test_failed_refresh_preserves_map_provenance(tmp_path, monkeypatch, python):
    ctx = context(tmp_path)
    prior = dict(map='old map', chars=7, commit='old', tokens_budget=2048, python='old-python')
    repomap._write_state(ctx, prior)
    monkeypatch.setattr(repomap, 'head_commit', lambda root: 'new')
    monkeypatch.setattr(repomap, 'find_python', lambda ctx: python)
    monkeypatch.setattr(repomap, 'generate', lambda *args: ('', 'failed'))
    result = repomap.refresh(ctx)
    for key in prior:
        assert result[key] == prior[key]
    assert result['error']
    assert repomap.stale(ctx)


def test_prompt_does_not_run_cached_interpreter_after_explicit_change(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    repomap._write_state(ctx, dict(map='cached', python='old-python'))
    calls = []
    def generate(ctx, python, *args, **kwargs):
        calls.append(python)
        return '', 'unavailable'
    monkeypatch.setattr(repomap, 'generate', generate)
    block, how = repomap.for_prompt(ctx, 'Fix `parse_config`')
    assert calls == []
    assert how == 'cached'
    assert block.endswith('cached')
