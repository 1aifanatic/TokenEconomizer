"""Reject invalid host settings without changing either the file or ownership state."""
import json
from pathlib import Path

import pytest

from tokeneconomizer.plugin import denylist
from tokeneconomizer.plugin.store import Store


@pytest.mark.parametrize("host,settings", [
    ("claude", {"permissions": "Workflow"}),
    ("claude", {"permissions": {"deny": "Workflow"}}),
    ("claude", {"permissions": {"deny": None}}),
    ("claude", {"permissions": {"deny": ["Workflow", {"tool": "Read"}]}}),
    ("copilot", {"disabledMcpServers": "Workflow"}),
    ("copilot", {"disabledMcpServers": ["Workflow", 42]}),
])
@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_invalid_deny_settings_are_preserved(tmp_path, host, settings, operation):
    repo = str(tmp_path / "repo")
    path = Path(denylist.settings_path(repo, host))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings))
    original = path.read_bytes()
    store = Store(str(tmp_path / "state"))
    denylist.write_applied(store, {str(path): ["Workflow"]})
    state = Path(store.path(denylist.APPLIED)).read_bytes()
    with pytest.raises(RuntimeError, match="refusing to edit"):
        if operation == "apply":
            denylist.apply(repo, ["Other"], store, host=host)
        else:
            denylist.restore(repo, store, host=host)
    assert path.read_bytes() == original
    assert Path(store.path(denylist.APPLIED)).read_bytes() == state
