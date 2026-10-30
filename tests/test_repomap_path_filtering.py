import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('use_git', [False, True])
def test_map_filters_directory_components_relative_to_repo(tmp_path, monkeypatch, capsys, use_git):
    # An excluded name outside the repository must not exclude the whole tree.
    repo = tmp_path / 'docs' / 'checkout'
    repo.mkdir(parents=True)
    for name in ['core.py', 'builders.py', 'distribution.py', 'docs_api/module.py', 'build/generated.py', 'docs/manual.py']:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def example(): pass\n')
    if use_git:
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    class RepoMap:
        def __init__(self, **kwargs):
            pass
        def get_repo_map(self, chat, files, **kwargs):
            return '\n'.join(sorted(os.path.relpath(p, repo) for p in files))
    monkeypatch.setitem(sys.modules, 'aider', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'aider.io', SimpleNamespace(InputOutput=lambda: None))
    monkeypatch.setitem(sys.modules, 'aider.models', SimpleNamespace(Model=lambda *args: None))
    monkeypatch.setitem(sys.modules, 'aider.repomap', SimpleNamespace(RepoMap=RepoMap))
    script = Path(__file__).resolve().parents[1] / 'tokeneconomizer/plugin/repomap_gen.py'
    monkeypatch.setattr(sys, 'argv', [str(script), str(repo), '100'] + (['--git'] if use_git else []))
    runpy.run_path(str(script), run_name='__main__')
    assert set(capsys.readouterr().out.splitlines()) == {'core.py', 'builders.py', 'distribution.py', os.path.join('docs_api', 'module.py')}
