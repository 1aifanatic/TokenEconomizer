import pytest
from scripts import build_plugin

@pytest.mark.parametrize("suffix", ["", "plugin", "tokeneconomizer"])
def test_build_rejects_source_overlap_before_mutation(tmp_path, monkeypatch, suffix):
    repo = tmp_path / "repo"
    for name in ("plugin", "tokeneconomizer", ".claude-plugin"):
        (repo / name).mkdir(parents=True)
    sentinel = repo / "tokeneconomizer" / "keep.py"
    sentinel.write_text("keep")
    monkeypatch.setattr(build_plugin, "REPO", str(repo))
    with pytest.raises(ValueError, match="overlap"):
        build_plugin.build(str(repo / suffix))
    assert sentinel.read_text() == "keep"
