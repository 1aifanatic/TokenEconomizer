"""Exercise the comparison CLI with synthetic stored benchmark cells."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("baseline,arm", [([0], [10]), ([0, 0, 100], [10, 20, 50]), ([100], [50])])
def test_comparison_handles_zero_baselines(tmp_path, baseline, arm):
    for name, costs in (("baseline", baseline), ("arm", arm)):
        for index, cost in enumerate(costs):
            run = tmp_path / name / f"20261030-{index}"
            run.mkdir(parents=True)
            (run / "row.json").write_text(json.dumps({"instance": str(index), "tokens_total": cost}))
            (run / "run_config.json").write_text(json.dumps({"injection": {"present": index == 0, "chars": 4}}))
    output = tmp_path / "comparison.json"
    script = Path(__file__).resolve().parents[1] / "experiments/scripts/compare_arms.py"
    result = subprocess.run([sys.executable, str(script), "--baseline", str(tmp_path / "baseline"),
                             "--arm", str(tmp_path / "arm"), "--json", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text())
    assert data["pairs"] == len(baseline)
    assert data["paired_delta_tasks"] == sum(b != 0 for b in baseline)
    expected = -0.5 if any(baseline) else None
    assert data["paired_delta_mean"] == expected
    assert data["paired_delta_median"] == expected
    if 0 in baseline:
        assert "n/a" in result.stdout
        assert "zero baseline" in result.stdout
    else:
        assert "-50.0%" in result.stdout
