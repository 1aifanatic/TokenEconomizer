# workloads/ — the exact evaluated task sets (committed)

One JSON manifest per domain naming **which** tasks the published results ran. Unlike the
results tree (regenerated per run, git-ignored), these are **committed**: they are ids only —
no issue text, no transcripts — so all four together are about 16 KB.

| File | Tasks | Groups | Seeds |
|---|---|---|---|
| `swebench.json` | 229 | 6 repos | `0 1 2` |
| `webbench.json` | 102 | 5 projects | `A B C` |
| `creddata.json` | 100 | 1 | `0 1 2` |
| `reposvul.json` | 100 | 1 | `0 1 2` |

## Shape

```json
{
  "domain": "swebench",
  "group_field": "repo",
  "seeds": [0, 1, 2],
  "total_tasks": 229,
  "task_counts": {"django/django": 40, "...": 0},
  "groups": {"django/django": ["django__django-7530", "..."]},
  "full_split_only": ["pydata__xarray-6386", "..."]
}
```

`groups` preserves order. For Web-Bench that order is **load-bearing** — each task builds on
the previous one's edits, so a stream must run front to back.

`swebench.json` additionally carries `full_split_only`: the 30 instances that are **not** in
SWE-bench_Verified and must come from the full `princeton-nlp/SWE-bench` test split.

## Who reads them

- **`scripts/launch/*.sh`** default `WORKLOAD` to `workloads/$DOMAIN.json` and pass it to the
  driver, so a launch is pinned to the evaluated set even if your `TASKS_FILE` holds more.
  `WORKLOAD=none` opts out.
- **`scripts/run_experiment.py --workload`** filters `--tasks-file` to the manifest (and to
  `--repo`'s group). Unlike the legacy `--repo` prefix match, an unknown group is an error
  rather than a silent fall back to the whole task list.
- **Any non-Python harness** that needs the list without importing the package.

## Regenerating

`experiments/te_bench/workloads.py` is the authority; these files are generated from it:

```bash
python scripts/export_workloads.py           # rewrite
python scripts/export_workloads.py --check    # verify (exit 1 if stale)
```

`tests/test_workloads.py` runs the same comparison, so drift fails the test suite.
