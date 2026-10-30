import pytest
from tokeneconomizer.plugin import store

@pytest.mark.parametrize('wall_step', [-100, 100])
def test_lock_timeout_tracks_elapsed_time_despite_wall_clock_adjustments(tmp_path, monkeypatch, wall_step):
    elapsed = [0.0]
    monkeypatch.setattr(store.time, 'time', lambda: 1000 + wall_step * elapsed[0])
    monkeypatch.setattr(store.time, 'monotonic', lambda: elapsed[0])
    def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] > 1:
            raise AssertionError('timeout did not expire after one second')
    monkeypatch.setattr(store.time, 'sleep', sleep)
    def busy(fh):
        raise OSError('lock is held')
    monkeypatch.setattr(store, '_try_lock', busy)
    with store.Store(tmp_path).locked(timeout=0.2) as acquired:
        assert acquired is False
    assert 0.2 <= elapsed[0] <= 0.25
