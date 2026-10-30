"""The canonical index must describe the whole tree, or refuse to be written.

`aggregate.py --arms` limits the REWRITE, not just the reading: both CSVs are written
whole on every invocation. Naming a subset while writing to `<root>/index` would therefore
replace an index describing the tree with one describing a subset, at the path
`compare_arms.py` reads, under a name that says nothing about being partial -- with every
cell still on disk. The Codex lane runs one arm per invocation, which makes an `--arms`
an easy thing to pass, so the subset write to the canonical path is refused.

These tests are offline: they build a tiny tree of synthetic `row.json` files and run
`main()` against it.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGGREGATE = os.path.join(_REPO, "scripts", "aggregate.py")


def _agg():
    spec = importlib.util.spec_from_file_location("_aggregate", _AGGREGATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(root, arm, cell, stamp, instance, **kw):
    d = os.path.join(root, arm, cell, f"{stamp}__{instance}__{arm}__standalone")
    os.makedirs(d, exist_ok=True)
    row = {"instance": instance, "repo": "sympy/sympy", "seed": 0, "arm": arm,
           "resolved": True, "num_turns": 10, "tokens_total": 1000,
           "input_tokens": 10, "output_tokens": 100,
           "cache_read_input_tokens": 800, "cache_creation_input_tokens": 90,
           "model": "gpt-5.6-terra", "effort": "high", "agent": "codex",
           "run_dir": d}
    row.update(kw)
    with open(os.path.join(d, "row.json"), "w") as f:
        json.dump(row, f)
    return d


@pytest.fixture
def tree(tmp_path):
    """Two arms, one cell each -- the shape the refusal is about."""
    root = str(tmp_path / "codex_swebench_gpt56terra")
    _row(root, "baseline", "sympy_s0", "20260916T000000Z", "sympy-11618")
    _row(root, "baseline", "sympy_s0", "20260916T000100Z", "sympy-12096")
    _row(root, "repomap", "sympy_s0", "20260916T010000Z", "sympy-11618")
    return root


def _run(argv):
    mod = _agg()
    old = sys.argv
    sys.argv = ["aggregate.py"] + argv
    try:
        mod.main()
    finally:
        sys.argv = old


def _cells(index_dir):
    import csv
    with open(os.path.join(index_dir, "summary_by_cell.csv")) as f:
        return [(r["arm"], r["repo"], r["n_tasks"]) for r in csv.DictReader(f)]


def test_whole_tree_is_the_default(tree):
    _run([tree])
    assert _cells(os.path.join(tree, "index")) == [
        ("baseline", "sympy", "2"), ("repomap", "sympy", "1")]


def test_subset_rewrite_of_the_canonical_index_is_refused(tree):
    """`--arms repomap` must not leave the canonical index holding only repomap."""
    _run([tree])                                     # a good index exists
    before = _cells(os.path.join(tree, "index"))

    with pytest.raises(SystemExit) as e:
        _run([tree, "--arms", "repomap"])
    assert e.value.code != 0

    # and, the point of refusing rather than warning: the good index is untouched.
    assert _cells(os.path.join(tree, "index")) == before


def test_subset_is_allowed_somewhere_of_its_own(tree, tmp_path):
    _run([tree])
    before = _cells(os.path.join(tree, "index"))

    out = str(tmp_path / "repomap_only")
    _run([tree, "--arms", "repomap", "--out", out])

    assert _cells(out) == [("repomap", "sympy", "1")]
    assert _cells(os.path.join(tree, "index")) == before


def test_naming_every_arm_is_not_a_subset(tree):
    """`--arms` that happens to list the whole tree is a no-op, not an error."""
    _run([tree, "--arms", "baseline", "repomap"])
    assert _cells(os.path.join(tree, "index")) == [
        ("baseline", "sympy", "2"), ("repomap", "sympy", "1")]


def test_superseded_run_dirs_are_not_double_counted(tree):
    """The re-run case the pilot cells are in: newest attempt wins, older is dropped.

    Already the behaviour of `collect()`; pinned here because the launcher passes
    `--fresh`, which starts a new stream in the SAME cell directory, so a re-run cell
    always holds both attempts on disk.
    """
    _row(tree, "repomap", "sympy_s0", "20260916T020000Z", "sympy-11618", num_turns=99)
    _run([tree])
    assert _cells(os.path.join(tree, "index")) == [
        ("baseline", "sympy", "2"), ("repomap", "sympy", "1")]

    import csv
    with open(os.path.join(tree, "index", "all_rows.csv")) as f:
        kept = [r for r in csv.DictReader(f) if r["arm"] == "repomap"]
    assert [r["num_turns"] for r in kept] == ["99"]


@pytest.mark.parametrize("alias", ["explicit", "relative", "symlink"])
def test_explicit_canonical_output_preserves_existing_index(tree, tmp_path, monkeypatch, alias):
    from pathlib import Path

    _run([tree])
    index = Path(tree) / "index"
    before = {p.name: p.read_bytes() for p in index.iterdir()}
    if alias == "relative":
        monkeypatch.chdir(tree)
        out = "./index"
    elif alias == "symlink":
        link = tmp_path / "index_alias"
        try:
            link.symlink_to(index, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks unavailable")
        out = str(link)
    else:
        out = str(index)
    with pytest.raises(SystemExit) as exc:
        _run([tree, "--arms", "repomap", "--out", out])
    assert exc.value.code != 0
    assert {p.name: p.read_bytes() for p in index.iterdir()} == before


def test_explicit_canonical_output_allows_all_arms(tree):
    _run([tree, "--arms", "baseline", "repomap", "--out", os.path.join(tree, "index")])
    assert _cells(os.path.join(tree, "index")) == [
        ("baseline", "sympy", "2"), ("repomap", "sympy", "1")]
def test_unreached_webbench_tasks_are_not_graded_misses(tmp_path):
    import csv

    root = str(tmp_path / "webbench")
    _row(root, "baseline", "project_s0", "20260916T000000Z", "task-1",
         domain="webbench", resolved=True, resolved_p1=True, resolved_p2=True,
         webbench_attempts=1)
    _row(root, "baseline", "project_s0", "20260916T000100Z", "task-2",
         domain="webbench", resolved=False, resolved_p1=False, resolved_p2=False,
         webbench_attempts=2)
    # This is the shape WebBenchRunner emits after its sequential stream stops.
    _row(root, "baseline", "project_s0", "20260916T000200Z", "task-3",
         domain="webbench", resolved=False, stream_broken=True, unreached=True,
         resolved_p1=None, resolved_p2=None, webbench_attempts=0,
         webbench_attempt_results=[], tokens_total=0, input_tokens=0,
         output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    _run([root])
    with open(os.path.join(root, "index", "summary_by_cell.csv")) as f:
        summary, = csv.DictReader(f)
    assert summary["n_tasks"] == "3"
    assert summary["n_graded"] == "2"
    assert summary["n_not_graded"] == "1"
    assert summary["resolve_rate"] == "0.5"
    assert summary["n_attempted"] == "2"
    with open(os.path.join(root, "index", "all_rows.csv")) as f:
        rows = {r["instance"]: r for r in csv.DictReader(f)}
    assert rows["task-2"]["not_graded"] == "False"
    assert rows["task-3"]["not_graded"] == "True"
    assert rows["task-3"]["not_graded_reason"] == "unreached"


@pytest.mark.parametrize("baseline,arm", [([0], [10]), ([0, 0, 100], [10, 20, 50]), ([100], [50])])
def test_comparison_handles_zero_baselines(tmp_path, baseline, arm):
    for name, costs in (("baseline", baseline), ("arm", arm)):
        for index, cost in enumerate(costs):
            run = tmp_path / name / f"20261030-{index}"
            run.mkdir(parents=True)
            (run / "row.json").write_text(json.dumps({"instance": str(index), "tokens_total": cost}))
            (run / "run_config.json").write_text(json.dumps({"injection": {"present": index == 0, "chars": 4}}))
    output = tmp_path / "comparison.json"
    script = Path(__file__).resolve().parents[1] / "scripts/compare_arms.py"
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
