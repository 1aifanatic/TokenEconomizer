from types import SimpleNamespace
import pytest
from tokeneconomizer.plugin import fold, hosts
from tokeneconomizer.plugin.config import LiveConfig
from tokeneconomizer.plugin.store import Store

@pytest.mark.parametrize('host,items,catalog,pattern,expected', [
    ('claude', ['Unused', 'KeepMe'], ['Unused', 'KeepMe'], 'Keep*', ['Unused']),
    ('claude', ['KeepMe'], ['KeepMe'], 'KeepMe', []),
    ('copilot', ['docs', 'other'], ['docs-search', 'docs-list', 'other-search'], 'docs-*', ['other']),
    ('codex', ['collaboration'], [], '*', ['collaboration']),
])
def test_pending_list_rechecks_current_tool_protection(tmp_path, monkeypatch, host, items, catalog, pattern, expected):
    ctx = SimpleNamespace(store=Store(tmp_path), repo=SimpleNamespace(root=str(tmp_path)),
                          config=LiveConfig(toolprune_protect=[pattern], toolprune_quiet_minutes=0),
                          host=hosts.HOSTS[host](), env={})
    ctx.store.write('untool.json', {'proposal': items, 'catalog_tools': catalog,
                                  'auto_pending': {'items': items, 'since': 1}})
    applied = []
    monkeypatch.setattr(fold.denylist, 'applied_for', lambda *args: [])
    monkeypatch.setattr(fold.denylist, 'kept_for', lambda *args: [])
    def apply(root, items, store, **kwargs):
        applied.extend(items)
        return items
    monkeypatch.setattr(fold.denylist, 'apply', apply)
    assert fold.apply_pending(ctx, now=100) is None
    assert applied == expected
    state = ctx.store.read('untool.json')
    assert not state.get('auto_pending')
    assert not state['proposal']
