"""Codex trust detection accepts comments and both TOML string quote styles."""
import pytest

from tokeneconomizer.plugin.denylist import codex_project_trusted


@pytest.mark.parametrize("header_comment,value", [
    (" # my project", '"trusted"'),
    ("", "'trusted'"),
    (" # my project", "'trusted' # reviewed locally"),
    ("", '"trusted" # reviewed locally'),
])
def test_trusted_project_with_toml_comments_and_literal_values(tmp_path, header_comment, value):
    repo = tmp_path / "project"
    repo.mkdir()
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text(
        f"[projects.'{repo}']{header_comment}\ntrust_level = {value}\n")
    assert codex_project_trusted(str(repo), {"CODEX_HOME": str(home)}) is True


@pytest.mark.parametrize("value", ['"untrusted"', "'untrusted'", '"trusted-extra"'])
def test_other_trust_values_are_not_accepted(tmp_path, value):
    home = tmp_path / "codex"
    home.mkdir()
    repo = tmp_path / "project"
    (home / "config.toml").write_text(
        f"[projects.'{repo}'] # project\ntrust_level = {value}\n")
    assert codex_project_trusted(str(repo), {"CODEX_HOME": str(home)}) is False
