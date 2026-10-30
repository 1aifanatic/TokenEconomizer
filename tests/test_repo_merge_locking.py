"""A busy host must not leave earlier hosts partially merged."""
from pathlib import Path

import pytest

from tokeneconomizer.plugin import repos
from tokeneconomizer.plugin.store import Store


@pytest.mark.parametrize("busy_key", ["source", "destination"])
def test_whole_merge_takes_all_host_locks_before_changing_state(tmp_path, busy_key):
    root = str(tmp_path)
    source = Store(str(tmp_path / "repos/source/claude"))
    destination = Store(str(tmp_path / "repos/destination/claude"))
    source.write("sessions/s1.json", {"session_id": "s1"})
    source.write("envelope.json", {"resolved_turns": [5], "resolved_snaps": []})
    source.append("events.jsonl", {"kind": "nudge", "session_id": "s1", "at": 1})
    destination.write("envelope.json", {"resolved_turns": [10], "resolved_snaps": []})
    Store(str(tmp_path / "repos/source/codex")).ensure()
    busy = Store(str(tmp_path / "repos" / busy_key / "codex"))
    before = Path(destination.path("envelope.json")).read_bytes()
    with busy.locked("fold", timeout=0) as held:
        assert held
        assert repos.merge(root, "source", "destination", timeout=0) is None
    assert Path(destination.path("envelope.json")).read_bytes() == before
    assert destination.read_lines("events.jsonl") == []
    assert (tmp_path / "repos/source").is_dir()

    assert repos.merge(root, "source", "destination", timeout=0) is not None
    assert destination.read("envelope.json")["resolved_turns"] == [5, 10]
    assert len(destination.read_lines("events.jsonl")) == 1
    assert not (tmp_path / "repos/source").exists()
    assert list((tmp_path / "merged").glob("source.*"))
