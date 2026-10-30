"""Applied capabilities must correspond to configuration entries we actually added."""
from pathlib import Path

import pytest

from tokeneconomizer.plugin import denylist
from tokeneconomizer.plugin.store import Store


@pytest.mark.parametrize("user", [
    '[features]\ngoals = true\n',
    'features.goals = false\n',
    'features = { hooks = true }\n',
])
def test_user_owned_codex_settings_are_not_recorded_as_added(tmp_path, monkeypatch, user):
    repo = tmp_path / "repo"
    path = Path(denylist.settings_path(str(repo), "codex"))
    path.parent.mkdir(parents=True)
    path.write_text(user)
    store = Store(str(tmp_path / "state"))
    monkeypatch.setattr(denylist, "_git_tracked", lambda *args: False)
    def unexpected(*args, **kwargs):
        pytest.fail("a no-op must not rewrite or validate the user's file")
    monkeypatch.setattr(denylist, "_write_text", unexpected)
    monkeypatch.setattr(denylist, "codex_config_error", unexpected)
    assert denylist.apply(str(repo), ["goals"], store, host="codex") == []
    assert denylist.applied_for(store, str(repo), "codex") == []
    assert path.read_text() == user


def test_mixed_codex_apply_records_only_changed_capabilities(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    path = Path(denylist.settings_path(str(repo), "codex"))
    path.parent.mkdir(parents=True)
    user = '[features]\ngoals = true\n'
    path.write_text(user)
    store = Store(str(tmp_path / "state"))
    monkeypatch.setattr(denylist, "_git_tracked", lambda *args: False)
    monkeypatch.setattr(denylist, "codex_config_error", lambda *args: None)
    assert denylist.apply(str(repo), ["goals", "image_media"], store, host="codex") == ["image_media"]
    assert denylist.applied_for(store, str(repo), "codex") == ["image_media"]
    assert denylist.restore(str(repo), store, host="codex") == ["image_media"]
    assert path.read_text() == user
