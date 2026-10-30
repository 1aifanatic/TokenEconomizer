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
