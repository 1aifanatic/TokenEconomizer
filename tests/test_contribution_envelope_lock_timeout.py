from contextlib import contextmanager
from tokeneconomizer.plugin.store import Store
from tokeneconomizer.loopguard.envelope import record_turn


def test_record_turn_does_not_write_without_lock(tmp_path, monkeypatch):
    store = Store(tmp_path)
    original = {"resolved_turns": [12], "resolved_snaps": []}
    store.write("envelope.json", original)
    @contextmanager
    def unavailable(*args, **kwargs):
        yield False
    monkeypatch.setattr(store, "locked", unavailable)
    tracker = record_turn(store, object())
    assert tracker.resolved_turns == [12]
    assert store.read("envelope.json") == original
