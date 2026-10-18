# scripts/ — reproduction pipeline

The scripts here plan, drive, aggregate and compare the experiment arms. The results trees
they write are **outside the repository and regenerated per run**. Dataset, Docker-image,
and results paths below are **placeholders**: set the environment variables to point at
your own runs. The run book is [`../docs/RUNNING.md`](../docs/RUNNING.md).

## The end-to-end chain

```
 build a task stream            run each arm                  aggregate                 compare
 ─────────────────────         ───────────────────────       ─────────────────         ──────────────
 <your stream builder>   ──▶   run_experiment.py       ──▶   aggregate.py        ──▶   compare_arms.py
 (chronological task list)     (facade + AgentRunner;         row.json  ─▶              paired per-instance
                               Docker/CLI harness: te_harness/) all_rows.csv +          cost and resolve
                                     │                        summary_by_cell.csv
                                     ▼
                               _streams/*.jsonl  ──▶  summarize_stream.py  (amortized cost curve)
```

### 0. Launchers — `launch/` (reproduce the main experiments)

`run_experiment.py` is the one-arm driver that maps each **proven arm** to an exact
`EconomizerConfig` and drives the facade per task (see [`launch/README.md`](launch/README.md)
for the full arm→flags table). The `launch/*.sh` scripts wrap it into lanes and A/Bs:

```bash
# Claude/Copilot tri-mechanism A/B on a domain (baseline vs trimech), then aggregate:
AGENT=claude DOMAIN=swebench ARM=trimech REPOS="pydata/xarray" SEEDS="0 1 2" \
  RUNNER=te_harness.swebench:make_runner bash scripts/launch/trimech_ab.sh
# Codex-native A/B (codex-untool | codex-spiral | codex-inject | codex-trimech | ...):
ARM=codex-untool RUNNER=te_harness.swebench:make_runner bash scripts/launch/codex_ab.sh
# Offline wiring check (StubRunner; writes only to a temp dir, cleans up):
bash scripts/launch/smoke_test.sh
```

The task-execution harness is selected with `RUNNER=module:factory`: this repo's own is
[`te_harness/`](../te_harness), and `RUNNER=stub` runs a no-agent stand-in for wiring checks
only.

### 1. Run each arm

Task execution — launching a sealed agent (Claude Code / Copilot CLI / Codex) inside its
Docker image, applying the economizer's per-task decisions, and grading against the gold
patch — lives **below the `te_bench.runner.AgentRunner` interface**, in
[`te_harness/`](../te_harness) (details: [`../docs/HARNESS.md`](../docs/HARNESS.md)). It is a
sibling package, never imported from the core and not shipped in the wheel, so the core stays
free of Docker and CLI dependencies.

Bringing your own is a supported path, not a fallback: pass `--runner module:factory` for a
zero-arg factory returning an `AgentRunner` that:

- builds an `Economizer` per repo stream (`te_bench.facade.Economizer`);
- per task calls `injection_prefix` → `build_tools` → `run_kwargs`, runs the agent with
  those decisions, then `after_task`;
- writes one `row.json` per task-run under `<RESULTS>/<arm>/<repo>_s<seed>/<run>/` and a
  per-task `transcript.jsonl` the mechanisms read;
- optionally appends each task's row to `<RESULTS>/<arm>/_streams/<repo>__<mode>__<ts>.jsonl`.

Placeholders to set for your harness (see [`../docs/BENCHMARKS.md`](../docs/BENCHMARKS.md)
for the full recipe and the driver-task-list vs. dataset-root distinction):

| Placeholder | What it is |
|---|---|
| `TOKENECONOMIZER_SWEBENCH_TASKS` | JSON list of `{instance_id, problem_statement, repo}` (SWE-bench; driver task list **and** adapter source) |
| `TOKENECONOMIZER_WEBBENCH_TASKS` / `_CREDDATA_TASKS` / `_REPOSVUL_TASKS` | driver task-list JSON **file** for those domains (not a dir) |
| `TOKENECONOMIZER_WEBBENCH_PROJECTS` | Web-Bench `projects/` tree (adapter dataset root) |
| `TOKENECONOMIZER_CREDDATA_ROOT` / `_REPOSVUL_ROOT` / `_CYBERGYM_SUITE` | dataset roots for those adapters |
| `<DOCKER_IMAGE>` | era-pinned benchmark image (`te_harness.swebench.image_for`, or your runner) |
| `TOKENECONOMIZER_RESULTS` | root of the results trees the aggregator reads |

The arm catalog, per agent and domain, is in [`../docs/RUNNING.md`](../docs/RUNNING.md).

### 2. Aggregate — `aggregate.py`

Run dirs are timestamped (`<UTC>__<index>__…`), so re-running a cell adds a run dir rather
than overwriting one. The aggregator therefore keeps **one row per (arm, cell, instance),
from the newest attempt**, and prints `superseded runs skipped: N` when older attempts were
passed over — they stay on disk. Move a run under `provenance/` to retire it entirely; only
the arm dirs are walked.

```
python scripts/aggregate.py "$TOKENECONOMIZER_RESULTS" [--arms baseline trimech ...]
```

Walks `<root>/<arm>/<repo>_s<seed>/**/row.json` → writes `<root>/index/all_rows.csv`
(one row per task-run) and `summary_by_cell.csv` (one row per arm×repo×seed). Cost is the
honest cache-aware basis `pricing.effective_tokens(row) + ledger.overhead(row)` (vendored).

### 3. Amortized curve — `summarize_stream.py`

```
python scripts/summarize_stream.py "$TOKENECONOMIZER_RESULTS"/<arm>/_streams/*.jsonl
python scripts/summarize_stream.py --curve run.jsonl      # per-position amortized curve
```

Folds each task's execution cost + mechanism overhead into one amortized-per-task number,
cache-aware by default (`--raw` for legacy full-weight `tokens_total`).

### 4. Compare arms — `compare_arms.py`

```
python scripts/compare_arms.py --baseline "$TOKENECONOMIZER_RESULTS"/<tree>/baseline/<cell> \
                               --arm      "$TOKENECONOMIZER_RESULTS"/<tree>/<arm>/<cell>
```

Pairs the two cells on instance id (a task missing from either side is reported and
excluded) and reports the pooled mean, the median, the paired per-task delta and resolve
counts, on the same cache-aware basis with each arm's mechanism spend charged to it.
