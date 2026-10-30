"""Generated per-user settings must actually be ignored by Git."""
import os
import subprocess

import pytest

from tokeneconomizer.plugin import denylist
from tokeneconomizer.plugin.store import Store


@pytest.mark.parametrize("host", ["claude", "copilot"])
@pytest.mark.parametrize("comment", ["# {entry}\n", "# Keep {entry} local\n"])
def test_commented_exclude_does_not_count_as_an_ignore_rule(tmp_path, host, comment):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    rel = denylist._SETTINGS[host].replace("\\", "/")
    exclude = repo / ".git/info/exclude"
    original = comment.format(entry="/" + rel)
    exclude.write_text(original)
    store = Store(str(tmp_path / "state"))
    assert denylist.apply(str(repo), ["Example"], store, host=host) == ["Example"]
    result = subprocess.run(["git", "-c", "core.excludesFile=" + os.devnull, "check-ignore", "--", rel], cwd=repo,
                            capture_output=True, text=True)
    assert result.returncode == 0, "generated settings remain visible to git add"
    assert exclude.read_text().startswith(original)
    before = exclude.read_bytes()
    assert denylist._exclude_from_git(str(repo), denylist._SETTINGS[host])
    assert exclude.read_bytes() == before
