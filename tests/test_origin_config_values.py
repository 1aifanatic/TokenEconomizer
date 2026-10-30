import subprocess
import pytest
from tokeneconomizer.plugin.paths import origin_url, repo_identity

@pytest.mark.parametrize('url', ['/tmp/local repo.git', '/tmp/repo#one.git', '/tmp/repo;one.git', 'https://example.com/owner/repo.git'])
def test_origin_url_matches_git_written_values(tmp_path, url):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'config', 'remote.origin.url', url], check=True)
    assert origin_url(str(tmp_path)) == url
    assert repo_identity(str(tmp_path), env={}).how == 'remote'

@pytest.mark.parametrize('value', ['https://example.com/repo.git # comment', '"https://example.com/repo.git" ; comment'])
def test_origin_url_ignores_unquoted_comments(tmp_path, value):
    dot = tmp_path / '.git'
    dot.mkdir()
    (dot / 'HEAD').write_text('ref: refs/heads/main\n')
    (dot / 'config').write_text('[remote "origin"]\n url = ' + value + '\n')
    assert origin_url(str(tmp_path)) == 'https://example.com/repo.git'


def test_quoted_git_escapes_are_decoded(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    url = 'C:\\Users\\name\\local repo.git'
    subprocess.run(['git', '-C', str(tmp_path), 'config', 'remote.origin.url', url], check=True)
    assert origin_url(str(tmp_path)) == url
