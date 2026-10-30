import pytest
from tokeneconomizer.plugin.store import Store

@pytest.mark.parametrize("failure", ["serialize", "replace"])
def test_failed_write_keeps_original_without_temporary_files(tmp_path, monkeypatch, failure):
    store = Store(tmp_path)
    store.write("state.json", {"old": True})
    value = {"bad": object()}
    error = TypeError
    if failure == "replace":
        def fail(*args):
            raise OSError("replace failed")
        monkeypatch.setattr("tokeneconomizer.plugin.store.os.replace", fail)
        value, error = {"new": True}, OSError
    with pytest.raises(error):
        store.write("state.json", value)
    assert store.read("state.json") == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))
