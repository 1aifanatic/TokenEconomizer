import os
import pytest
from tokeneconomizer.plugin import cli, paths

@pytest.mark.parametrize('disappears', ['entry', 'directory'])
def test_recent_host_tolerates_state_disappearing_during_scan(tmp_path, monkeypatch, disappears):
    monkeypatch.setenv('TOKENECONOMIZER_HOME', str(tmp_path / 'state'))
    repo = tmp_path / 'repo'
    repo.mkdir()
    identity = paths.repo_identity(str(repo))
    base = paths.repo_state_dir(identity, 'claude')
    fallback = paths.repo_state_dir(identity, 'copilot')
    os.makedirs(base)
    os.makedirs(fallback)
    file = os.path.join(base, 'recent.json')
    open(file, 'w').close()
    os.utime(base, (100, 100))
    os.utime(file, (200, 200))
    os.utime(fallback, (50, 50))
    original_listdir = os.listdir
    original_mtime = os.path.getmtime
    if disappears == 'entry':
        monkeypatch.setattr(os, 'listdir', lambda p: original_listdir(p) + ['removed.tmp'] if p == base else original_listdir(p))
    else:
        def mtime(p):
            if p == base:
                raise FileNotFoundError(p)
            return original_mtime(p)
        monkeypatch.setattr(os.path, 'getmtime', mtime)
    assert cli._recent_host(str(repo)) == ('claude' if disappears == 'entry' else 'copilot')
