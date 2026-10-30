"""A benchmark result and its metadata must come from the same run directory."""
import importlib.util
import json
from pathlib import Path

import pytest


def load_comparison():
    path = Path(__file__).resolve().parents[1] / "scripts/compare_arms.py"
    spec = importlib.util.spec_from_file_location("comparison_reruns", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("new_config", [None, "{incomplete", '{}'])
def test_latest_row_does_not_inherit_previous_run_configuration(tmp_path, new_config):
    old = tmp_path / "20261029T000000Z__task"
    new = tmp_path / "20261030T000000Z__task"
    old.mkdir()
    new.mkdir()
    (old / "row.json").write_text(json.dumps({"instance": "task", "tokens_total": 100}))
    (old / "run_config.json").write_text(json.dumps({
        "routing": {"base_url": "https://old.invalid"},
        "injection": {"present": True, "chars": 400},
    }))
    (new / "row.json").write_text(json.dumps({"instance": "task", "tokens_total": 200}))
    if new_config is not None:
        (new / "run_config.json").write_text(new_config)
    rows, configs = load_comparison().load_cell(str(tmp_path))
    assert rows["task"]["tokens_total"] == 200
    assert configs.get("task", {}) == {}


def test_latest_valid_configuration_stays_paired_with_latest_row(tmp_path):
    for day in (29, 30):
        run = tmp_path / f"202610{day}T000000Z__task"
        run.mkdir()
        (run / "row.json").write_text(json.dumps({"instance": "task", "tokens_total": day}))
        (run / "run_config.json").write_text(json.dumps({"injection": {"present": day == 29}}))
    rows, configs = load_comparison().load_cell(str(tmp_path))
    assert rows["task"]["tokens_total"] == 30
    assert configs["task"] == {"injection": {"present": False}}
