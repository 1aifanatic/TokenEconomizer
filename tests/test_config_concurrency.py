import json
import subprocess
import sys
import time
import pytest

FIRST = '''
import sys, time
from pathlib import Path
from tokeneconomizer.plugin import config
path, ready, release = map(Path, sys.argv[1:])
original = config._read
def read(*args, **kwargs):
    result = original(*args, **kwargs)
    ready.touch()
    deadline = time.monotonic() + 10
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test release timed out")
        time.sleep(0.01)
    return result
config._read = read
config.write_config(str(path), {"microskills": "on"})
'''

@pytest.mark.parametrize('operation', ['set', 'unset'])
def test_concurrent_config_edits_preserve_both_updates(tmp_path, operation):
    path, ready, release = [tmp_path / name for name in ('config.json', 'ready', 'release')]
    path.write_text(json.dumps({'loopguard': 'off'}))
    first = subprocess.Popen([sys.executable, '-c', FIRST, str(path), str(ready), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert first.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        edit = 'config.write_config(sys.argv[1], {"toolprune": "off"})' if operation == 'set' else 'config.unset_config(sys.argv[1], ["loopguard"])'
        second = subprocess.Popen([sys.executable, '-c', 'import sys; from tokeneconomizer.plugin import config; ' + edit, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            second.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        release.touch()
        _, error = first.communicate(timeout=10)
        assert first.returncode == 0, error
        _, error = second.communicate(timeout=10)
        assert second.returncode == 0, error
        result = json.loads(path.read_text())
        assert result['microskills'] == 'envfacts+distiller'
        if operation == 'set':
            assert result['toolprune'] == 'off'
        else:
            assert 'loopguard' not in result
    finally:
        release.touch()
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate()


def test_config_lock_timeout_preserves_file(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from tokeneconomizer.plugin import config
    from tokeneconomizer.plugin.store import Store
    path = tmp_path / 'config.json'
    path.write_text('{"loopguard":"off"}')
    @contextmanager
    def unavailable(*args, **kwargs):
        yield False
    monkeypatch.setattr(Store, 'locked', unavailable)
    with pytest.raises(ValueError, match='busy'):
        config.write_config(str(path), {'toolprune': 'off'})
    assert path.read_text() == '{"loopguard":"off"}'
