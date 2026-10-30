"""Raw-token reports must not depend on the model pricing snapshot."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("raw", [True, False])
def test_unknown_model_raw_summary(tmp_path, raw):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps({
        "instance": "task", "position": 1, "model": "unlisted-test-model",
        "tokens_total": 120, "input_tokens": 100, "output_tokens": 20,
        "optimizer_tokens": 10, "resolved": True,
    }) + "\n")
    script = Path(__file__).resolve().parents[1] / "scripts/summarize_stream.py"
    command = [sys.executable, str(script), str(stream), "--json"]
    if raw:
        command.append("--raw")
    result = subprocess.run(command, capture_output=True, text=True)
    if not raw:
        assert result.returncode != 0
        assert "unlisted-test-model" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    summary, = json.loads(result.stdout)
    assert summary["cost_basis"] == "tokens_total (raw)"
    assert summary["cum_exec_tokens"] == 120
    assert summary["cum_optimizer_tokens"] == 10
    assert summary["cum_total_tokens"] == 130
    assert summary["curve"][0]["task_cost"] == 130
