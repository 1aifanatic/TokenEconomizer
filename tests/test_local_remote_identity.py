from pathlib import Path
from tokeneconomizer.plugin.paths import repo_identity


def checkout(path, remote):
    dot = path / '.git'
    dot.mkdir(parents=True)
    (dot / 'HEAD').write_text('ref: refs/heads/main\n')
    (dot / 'config').write_text('[remote "origin"]\nurl = ' + remote + '\n')
    return repo_identity(str(path), env={})


def test_same_relative_spelling_different_targets_have_different_keys(tmp_path):
    first = checkout(tmp_path / 'one' / 'clone', '../origin.git')
    second = checkout(tmp_path / 'two' / 'clone', '../origin.git')
    assert first.key != second.key


def test_relative_and_absolute_local_remote_share_key(tmp_path):
    origin = tmp_path / 'origin.git'
    origin.mkdir()
    relative = checkout(tmp_path / 'clone1', '../origin.git')
    absolute = checkout(tmp_path / 'clone2', str(origin))
    assert relative.key == absolute.key


def test_dot_git_suffix_is_part_of_local_path(tmp_path):
    plain = checkout(tmp_path / 'clone1', str(tmp_path / 'origin'))
    suffixed = checkout(tmp_path / 'clone2', str(tmp_path / 'origin.git'))
    assert plain.key != suffixed.key


def test_network_remote_normalization_is_unchanged(tmp_path):
    ssh = checkout(tmp_path / 'clone1', 'git@github.com:Owner/Repo.git')
    https = checkout(tmp_path / 'clone2', 'https://github.com/owner/repo')
    assert ssh.key == https.key
