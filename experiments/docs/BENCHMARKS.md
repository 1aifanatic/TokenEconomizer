# Benchmark provisioning — images, datasets, and env vars

> Per-benchmark reference detail. If you are setting up from a fresh
> clone, start with [`SETUP.md`](SETUP.md) and come here at its data step.

This is the operational companion to [`../scripts/launch/README.md`](../scripts/launch/README.md).
It documents, per benchmark, **exactly what you must pull/fetch/build and where it must land**
before an economized A/B can run.

> **Set up your environment first.** [`ENVIRONMENT.md`](ENVIRONMENT.md) covers the two layers
> (the core package vs. the harness and what it drives), the `requirements.txt`, and Docker
> install/update for beginners. The per-benchmark provisioning below is the harness layer.

## What lives where (and why this doc exists)

TokenEconomizer is middleware: it observes traces, maintains per-repo state, and decides
per-task interventions. It **never pulls an image or downloads a dataset itself** — that is
the job of the task-execution harness below the `AgentRunner` seam
(`RUNNER=te_harness.swebench:make_runner`, or your own; see [`HARNESS.md`](HARNESS.md)).
Nothing provisioned below is committed: the harness is code, and these are payloads.

### The two kinds of path you configure (do not confuse them)

Every domain has **two** independent inputs, read by **two** different components:

| # | What | Read by | Value | Env var pattern |
|---|---|---|---|---|
| A | **Task list** | `run_experiment.py` (the driver) — `json.load()`s it | a **JSON file** (a list of `{instance_id, problem_statement, repo}`) | `TOKENECONOMIZER_<DOMAIN>_TASKS` |
| B | **Dataset root** | the domain **adapter** + your harness | a **directory / suite file** with the raw data | `TOKENECONOMIZER_<DOMAIN>_ROOT` (or `_PROJECTS` / `_SUITE`) |

- **A (task list)** is what the driver iterates and what the per-task tool judge reads. It is
  **always a JSON file path** — pointing it at a directory will crash with `IsADirectoryError`.
- **B (dataset root)** is where the actual source/labels live, used by the adapter to enumerate
  instances and by your harness to materialize each task's workspace.

For **SWE-bench only**, A and B are the *same file*: `TOKENECONOMIZER_SWEBENCH_TASKS` is both
the driver's task list and the adapter's instance source. For every other domain they are
**different** — you export a small task-list JSON (A) *and* set the dataset root (B).

The commands below are the reference provisioning recipe; adapt paths to your infra.
**All fetched data is git-ignored** — none of it is committed to this repo.

### Which tasks to actually run — `tokeneconomizer.workloads`

The experiments do **not** run a whole benchmark. Each domain uses a fixed subset, and that subset is data in the core package — importable on a bare clone, with
no results tree, no datasets, and no Docker:

```python
from tokeneconomizer import workloads

workloads.task_counts("swebench")     # {'django/django': 40, ..., 'pydata/xarray': 39}
workloads.total_tasks("swebench")     # 229 tasks per seed
workloads.instances("swebench", "pydata/xarray")        # the exact 39 instance ids
workloads.swebench_image_tag("django__django-11532")    # the image to pull for one task
```

| Domain | Workload | Seeds |
|---|---|---|
| SWE-bench | **229** recorded over 6 repos (django 40, sphinx 44, sympy 40, xarray 39, matplotlib 34, sklearn 32); a NEW run executes **277** — `workloads.SWEBENCH_EXCLUDED` drops one sklearn instance and `workloads.SWEBENCH_ADDED` extends the two saturated streams, so `runnable_instances()` reports **sklearn 60** and **sympy 60** | `0 1 2` |
| Web-Bench | **102** tasks over 5 projects (calculator 21, table 21, expressjs/react/zustand 20 each) | `A B C` |
| CredData | **100** of the 333 repo ids | `0 1 2` |
| ReposVul | **100** instances, all from the **Python** split (`py-*`) | `0 1 2` |

Provision only what the workload names — it is a small fraction of each full benchmark, and
for SWE-bench it is the difference between ~277 and 2,294 multi-GB images.

The same lists ship as committed JSON manifests under [`../workloads/`](../workloads/) —
ids only, ~16 KB for all four, readable without importing the package. The launchers default
`WORKLOAD` to `workloads/$DOMAIN.json` and pass it to the driver, so a launch is pinned to the
evaluated set even when your `TASKS_FILE` holds a superset (`WORKLOAD=none` opts out):

```bash
# uses workloads/swebench.json automatically:
AGENT=claude DOMAIN=swebench ARM=trimech REPOS="pydata/xarray" SEEDS="0 1 2" \
  RUNNER=te_harness.swebench:make_runner bash scripts/launch/trimech_ab.sh

# or explicitly, straight to the driver:
python scripts/run_experiment.py --workload workloads/swebench.json --repo pydata/xarray ...
```

To pin a run to exactly the evaluated set from Python instead, filter the task list yourself:

```python
tasks = json.load(open(os.environ["TOKENECONOMIZER_SWEBENCH_TASKS"]))
tasks = workloads.filter_tasks(tasks, "swebench", "pydata/xarray")   # exactly 39
```

> Prefer `filter_tasks` over the driver's `--repo` flag. That flag prefix-matches the repo's
> short name against `instance_id` and **falls back to the entire task list when nothing
> matches** (`run_experiment.py:188`) — which is what happens for any repo whose org differs
> from its name, `pydata/xarray` included.

### Set a base directory first

Every command below writes under `$DATA`. Pick any writable path and export it once
(`$TOKENECONOMIZER_RESULTS` is where runs land — also your choice):

```bash
export DATA="$HOME/te_data"                       # anywhere you like; holds datasets + task lists
export TOKENECONOMIZER_RESULTS="$DATA/results"     # git-ignored results tree
mkdir -p "$DATA" "$TOKENECONOMIZER_RESULTS"
```

### Quick map of every placeholder

| Env var | Kind | Consumed by | Points at |
|---|---|---|---|
| `TOKENECONOMIZER_SWEBENCH_TASKS` | A + B | driver + swebench adapter | SWE-bench instances JSON (`{instance_id, problem_statement, repo}`) |
| `TOKENECONOMIZER_WEBBENCH_TASKS` | A | driver | Web-Bench task-list JSON you export (below) |
| `TOKENECONOMIZER_WEBBENCH_PROJECTS` | B | webbench adapter | Web-Bench `projects/` tree (each has `tasks.jsonl`) |
| `TOKENECONOMIZER_CREDDATA_TASKS` | A | driver | CredData task-list JSON you export (below) |
| `TOKENECONOMIZER_CREDDATA_ROOT` | B | creddata adapter | CredData checkout (`data/`, `meta/`) |
| `TOKENECONOMIZER_REPOSVUL_TASKS` | A | driver | ReposVul task-list JSON you export (below) |
| `TOKENECONOMIZER_REPOSVUL_ROOT` | B | reposvul adapter | prepared ReposVul (`instances/*.json`) |
| `TOKENECONOMIZER_CYBERGYM_SUITE` | B | cybergym adapter | CyberGym suite JSON |
| `TOKENECONOMIZER_RESULTS` | — | aggregate + harness | results trees the harness writes and the aggregator reads |
| `<DOCKER_IMAGE>` | — | your harness (not this repo) | the era-pinned benchmark image |

---

## 1. SWE-bench (primary — Claude / Copilot `trimech`, Codex arms)

**Dataset.** SWE-bench Verified (500 instances) from Hugging Face. The economizer only needs
the issue text; the grading gold patch stays inside your harness. Here the task list (A) and
the adapter's instance source (B) are the **same file**.

> **Verified alone is not enough.** A new run executes 277 instances and **79 of them are
> not in Verified**: the recent `pydata/xarray` tail (30, `workloads.SWEBENCH_FULL_SPLIT_ONLY`)
> plus all 49 de-saturation additions (`workloads.SWEBENCH_ADDED_FULL_SPLIT_ONLY` — both
> repos' Verified rows were already spent). Build the task file from both splits and take
> the union named by `workloads.runnable_instances("swebench")`; those two frozensets list
> exactly which instances force the second fetch.

```bash
mkdir -p "$DATA/swebench"
python - <<'PY'
import json, os
from datasets import load_dataset          # pip install datasets  (provisioning only)
from tokeneconomizer import workloads

wanted = {i for ids in workloads.runnable_instances("swebench").values() for i in ids}  # 277
rows, seen = [], set()
for name in ("princeton-nlp/SWE-bench_Verified", "princeton-nlp/SWE-bench"):
    for r in load_dataset(name, split="test"):                # Verified first, then the
        iid = r["instance_id"]                                # full split for the 30 extras
        if iid in wanted and iid not in seen:
            seen.add(iid)
            rows.append({"instance_id": iid, "repo": r["repo"],
                         "problem_statement": r["problem_statement"]})
    if seen >= wanted:
        break
missing = wanted - seen
out = os.path.join(os.environ["DATA"], "swebench", "swebench_tasks.json")
json.dump(rows, open(out, "w"))
print(len(rows), "->", out, "| missing:", sorted(missing) or "none")
PY
export TOKENECONOMIZER_SWEBENCH_TASKS="$DATA/swebench/swebench_tasks.json"
```

**Per-instance execution images.** The official SWE-bench harness runs each instance in a
pinned Docker image (repo + deps + test bed frozen at the issue's era). Your harness pulls
these; they are large, so pre-pull the repos you'll run:

Note the tag convention: the `__` in an instance id becomes `_1776_` in the image name, so
`django__django-11532` lives at `swebench/sweb.eval.x86_64.django_1776_django-11532:latest`.
`workloads.swebench_image_tag()` does that conversion, so the whole set is one loop:

```bash
python - > "$DATA/swebench/image_tags.txt" <<'PY'
from tokeneconomizer import workloads
for ids in workloads.runnable_instances("swebench").values():
    for iid in ids:
        print(workloads.swebench_image_tag(iid))
PY
wc -l "$DATA/swebench/image_tags.txt"                      # 277
xargs -a "$DATA/swebench/image_tags.txt" -P 4 -n 1 docker pull -q
# ...or let the SWE-bench harness build/pull them:  python -m swebench.harness.run_evaluation ...
```

Budget roughly **150–200 GB** for the 277 images: they share a ~1.4 GB base plus a
~1–3 GB environment layer per repo/era, with each instance adding only ~0.1–0.5 GB on top.
Unauthenticated Docker Hub pulls are rate-limited to 100 per 6 h, so `docker login` first
or expect the pull to span several windows.

**Task-spec schema** (what both the driver and the adapter read):
```json
[{"instance_id": "pydata__xarray-1234", "repo": "pydata/xarray",
  "problem_statement": "…issue text…"}]
```
The adapter redacts gold-solution URLs (PR/commit/blob links) from `problem_statement`
automatically.

**Run** (`$TOKENECONOMIZER_RESULTS` was set in the preamble):
```bash
AGENT=claude DOMAIN=swebench ARM=trimech \
  REPOS="pydata/xarray django/django sympy/sympy scikit-learn/scikit-learn sphinx-doc/sphinx" \
  SEEDS="0 1 2" RUNNER=te_harness.swebench:make_runner \
  bash scripts/launch/trimech_ab.sh
# Codex arms use the same task-spec, a different launcher, and one arm per command,
# billed to OPENAI_API_KEY. Both models are evaluated, each in its own tree:
# gpt-5.6-terra and gpt-5.6-sol.
MODEL=gpt-5.6-terra \
  ARM=codex-untool RUNNER=te_harness.swebench:make_runner bash scripts/launch/codex_ab.sh
# Twelve arms, one per mechanism plus the naive spine, the all-three arm (and its rtk and
# repomap variants) and all three scaffolds: baseline, baseline-medium, baseline-low,
# repomap, rtk, serena, codex-inject, codex-untool, codex-spiral, codex-trimech,
# codex-trimech-rtk, codex-trimech-repomap.
# rtk on Codex is INSTRUCTION-based (read rtk_compliance); serena is wired through
# config.toml (read serena_tool_calls). A configured scaffold is not a used one.
# Full catalog, one command per lane: docs/RUNNING.md section 5.
```

---

## 2. Web-Bench (primary — Copilot / Claude `trimech-web`)

> **All three agents run here** (Codex through `CodexWebBenchRunner`). Because Web-Bench runs
> the agent on the host with no per-task container, the Codex lane cannot use the PreToolUse
> hook the other two are guarded by — it ignores hooks — so it carries the SWE-bench
> `/etc/hosts` block inside an unprivileged user+mount namespace instead. Its rows report
> `net_guard_installed: false` and `answer_key_hosts_blocked: true`; filter on the second.
>
> ```bash
> DOMAIN=webbench MODEL=gpt-5.6-terra \
>   ARM=codex-untool RUNNER=te_harness.webbench:make_runner bash scripts/launch/codex_ab.sh
> ```

**Dataset + evaluator.** Web-Bench (ByteDance, `bytedance/web-bench`) is 50 Node/TypeScript
projects, each a stream of 20 sequentially dependent tasks. Its **Node/Playwright evaluator
owns the task loop and grading** and lives in your harness; the adapter only reads each
project's `tasks.jsonl`.

```bash
git clone https://github.com/bytedance/web-bench.git "$DATA/web-bench"
cd "$DATA/web-bench"
# It is a RUSH monorepo, not an npm project: `npm install` at the root does nothing useful.
npm i -g @microsoft/rush          # or use ./common/scripts/install-run-rush.js
rush install                      # installs every workspace
rush build --to @web-bench/eval   # the Node evaluator YOUR harness drives
rush build --to @web-bench/calculator --to @web-bench/expressjs --to @web-bench/react \
           --to @web-bench/zustand --to @web-bench/table
# Playwright browsers must match the version the projects PIN (@playwright/test 1.57.0
# wants browser build 1200); install from inside a project so the pinned version resolves:
cd projects/calculator && npx playwright install chromium chromium-headless-shell && cd -
# (B) dataset root the adapter reads:
export TOKENECONOMIZER_WEBBENCH_PROJECTS="$DATA/web-bench/projects"  # <project>/tasks.jsonl
```

> Use a **canonical** path here. The evaluator hands this root to each project's dev server
> while node reports `process.cwd()` already symlink-resolved; reached through a symlinked
> home (`/home/x` → `/srv/home/x`) the two spellings disagree and Vite cannot resolve the
> `react` project's entry module — every test fails with an app that never rendered, which
> looks exactly like an agent that wrote nothing. `readlink -f` the path before exporting it.

**Check the checkout before spending anything.** Run a project's own reference solution
through its own suite; a project that cannot pass its tests with the answer key cannot grade
an agent:

```bash
R="$TOKENECONOMIZER_WEBBENCH_PROJECTS"
cd "$R/calculator" && MAX_TEST_WORKERS=4 EVAL_PROJECT_ROOT="$R/calculator/src" \
  IS_EVAL_PRODUCTION=true EVAL=true \
  EVAL_PROJECT_PORT=3931 npm run test -- -a      # expect: 68 passed
```
`MAX_TEST_WORKERS=4` matches the runner's own `TE_WEBBENCH_MAX_WORKERS` (default 4). Leave
it off and Playwright uses half the CPU count of workers against one dev server; on a
many-core host the reference solutions then fail their own suites (observed: expressjs
88/92, react 51/59, prisma 90/91 — prisma has since been retired from the sweep) with
nothing actually wrong.

Expected totals: calculator 68 · react 59 · expressjs 92 · zustand ? · table ? (the two new
projects are unmeasured; run the gate command above once to record them)
(calculator measured 2026-09-07; react and expressjs carried over from the 2026-08
provisioning). No project in the current five needs a pre-build — `react`/`zustand` are a plain `vite build`
and `table` and `calculator`/`expressjs` build nothing. `expressjs` still fails one test if a stale `src/test.sqlite` is left behind.

**Expected dataset layout (B)** — `TOKENECONOMIZER_WEBBENCH_PROJECTS/<project>/tasks.jsonl`,
one object per line:
```json
{"id": "init",   "date": "…", "level": "easy", "description": "…"}
{"id": "task-1", "date": "…", "level": "easy", "description": "…"}
```

**Export the driver task list (A).** The driver needs its own JSON list of the tasks to
stream (one entry per Web-Bench task, `repo` = the project name). It is a projection of
`workloads.WEBBENCH_TASKS`, so a script builds it rather than a copy-pasted snippet:

```bash
export TOKENECONOMIZER_WEBBENCH_TASKS="$DATA/webbench_tasks.json"     # (A) driver task list
python scripts/export_webbench_tasks.py            # write it
python scripts/export_webbench_tasks.py --check    # verify only; exit 1 if stale
```

> **Three registries have to agree, and only one of them is the authority.**
> `tokeneconomizer/workloads.py` decides which projects and tasks are in the sweep; (A) the
> driver task list and (C) `workloads/webbench.json` are both projections of it, and (B) the
> projects tree has to contain a `tasks.jsonl` for every project named. Changing the project
> list is therefore three commands, in this order:
>
> ```bash
> python scripts/webbench_tasks_from_yaml.py --all   # B: projects that ship only tasks.yml
> python scripts/export_workloads.py                 # C
> python scripts/export_webbench_tasks.py            # A
> ```
>
> A stale (A) does not fail loudly at launch — the driver streams the *old* project's tasks
> into the *new* project's evaluator session and the harness raises
> `session is running 'X' but the task belongs to 'Y'` partway into the run.
>
> Some Web-Bench projects (`zustand` among the evaluated five) ship only `tasks.yml`, while
> `te_harness.webbench` and `WebBenchAdapter` both read `tasks.jsonl` and nothing else.
> `webbench_tasks_from_yaml.py` derives the jsonl and **leaves the yml in place on purpose**:
> Web-Bench's own evaluator prefers the yml when both exist, so deleting or editing one
> without the other silently desyncs the graded task order from the driver's.


**Run** (short streams → `trimech-web`, which adds the LLM nudge + frozen envelope). Each
value in `REPOS` names a Web-Bench **project**; the driver selects that project's adapter and
filters the task list to it:
```bash
AGENT=claude DOMAIN=webbench ARM=trimech-web SEEDS="0" \
  REPOS="calculator expressjs react zustand table" \
  RUNNER=te_harness.webbench:make_runner bash scripts/launch/trimech_ab.sh
```

Two things a Web-Bench harness has to get right, both because the streams are sequentially
dependent:

* **The evaluator owns the loop.** It walks a project's whole stream itself, so it must be
  started once per stream rather than per task. The driver exposes the optional
  `begin_stream(tasks, ...)` / `end_stream()` hooks on `AgentRunner` for exactly this; a
  harness that does not define them is called the usual way.
* **A failed task ends the stream.** In the evaluator's `sequential` mode the project stops
  at the first task that fails every attempt. The driver still asks for the tasks after it,
  and a harness should mark those rows unreached (`stream_broken` / `unreached`) with zero
  tokens rather than letting them read as cheap failures in the cost aggregation.

**Two resolve metrics, and they differ.** Each task gets up to `TE_WEBBENCH_RETRY`
attempts (default 2), so a row carries `resolved_p1` (first attempt — Web-Bench's own
headline metric) and `resolved_p2` (any attempt), alongside the per-attempt verdict list
`webbench_attempt_results`. `resolved` stays pass@N because that is what the evaluator
acts on when it decides to end a stream — and because a row's cost is the sum of every
attempt, pass@N is the metric the cost columns actually match. The retry can rescue several tasks
per arm, so never quote one metric as the other. Unreached tasks carry `None` for both —
never attempted is not a miss.

---

## 3. CredData (secondary — security / secret detection)

**Dataset.** Samsung CredData: 333 repos of obfuscated source, each `data/<repoid>/` with a
per-repo label CSV `meta/<repoid>.csv`.

```bash
git clone https://github.com/Samsung/CredData.git "$DATA/CredData"
cd "$DATA/CredData" && python download_data.py --data_dir data && cd -   # per upstream README
export TOKENECONOMIZER_CREDDATA_ROOT="$DATA/CredData"                    # (B) dir with data/ + meta/
```

**Expected dataset layout (B):**
```
$TOKENECONOMIZER_CREDDATA_ROOT/
  data/<repoid>/…            # obfuscated source to audit (harness copies a disposable clone)
  meta/<repoid>.csv          # line-level secret labels → graded to F1 (the quality guardrail)
```
Every repo gets the **same** read-only audit prompt; grading is line-level detection **F1**
(the CredData analog of `resolved`). No Docker image — the harness runs the agent over a
disposable copy of each repo directory.

**Export the driver task list (A).** The audit prompt is fixed, so build one entry per repoid
with that prompt as `problem_statement` (the exact prompt lives in
`tokeneconomizer/adapters/creddata.py:CREDDATA_TASK_PROMPT`):
```bash
python - <<'PY'
import glob, json, os
from tokeneconomizer.adapters.creddata import CREDDATA_TASK_PROMPT
from tokeneconomizer import workloads
root = os.environ["TOKENECONOMIZER_CREDDATA_ROOT"]
have = {os.path.basename(p)[:-4] for p in glob.glob(os.path.join(root, "meta", "*.csv"))}
ids = [rid for rid in workloads.CREDDATA_INSTANCES if rid in have]   # 100 of the 333 repos
print("missing from checkout:", sorted(set(workloads.CREDDATA_INSTANCES) - have) or "none")
rows = [{"instance_id": rid, "problem_statement": CREDDATA_TASK_PROMPT, "repo": rid}
        for rid in ids]
out = os.path.join(os.environ["DATA"], "creddata_tasks.json")
json.dump(rows, open(out, "w"))
print(len(rows), "->", out)
PY
export TOKENECONOMIZER_CREDDATA_TASKS="$DATA/creddata_tasks.json"        # (A) JSON list, NOT a dir
```

**Run:**
```bash
AGENT=claude DOMAIN=creddata ARM=trimech SEEDS="0 1 2" \
  RUNNER=your_pkg.harness:make_runner bash scripts/launch/trimech_ab.sh
```

---

## 4. ReposVul (secondary — vulnerability detection)

**Dataset.** ReposVul (ICSE'24, `Eshe0922/ReposVul`): each instance is a CVE with a fixing
commit; you reconstruct the **pre-fix (vulnerable)** files at their real repo paths and
grade line-level F1 against the fix-removed lines.

```bash
git clone https://github.com/Eshe0922/ReposVul.git "$DATA/ReposVul-src"
# Prepare per-instance vulnerable-file snapshots into instances/*.json with YOUR prep step
# (checkout parent commit of each fixing commit, capture the removed lines as labels):
python your_prep/reposvul_prepare.py --src "$DATA/ReposVul-src" --out "$DATA/ReposVul/instances"
export TOKENECONOMIZER_REPOSVUL_ROOT="$DATA/ReposVul"                    # (B) dir containing instances/
```

**Expected dataset layout (B):** `$TOKENECONOMIZER_REPOSVUL_ROOT/instances/<iid>.json` (one
prepared instance each). Every instance gets the **same** vulnerability-audit prompt; grading
is line-level **F1**. No Docker image.

**Export the driver task list (A)** (prompt from
`tokeneconomizer/adapters/reposvul.py:REPOSVUL_TASK_PROMPT`):
```bash
python - <<'PY'
import glob, json, os
from tokeneconomizer.adapters.reposvul import REPOSVUL_TASK_PROMPT
from tokeneconomizer import workloads
root = os.environ["TOKENECONOMIZER_REPOSVUL_ROOT"]
have = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(root, "instances", "*.json"))}
ids = [iid for iid in workloads.REPOSVUL_INSTANCES if iid in have]   # 100 Python-split CVEs
print("missing from prep:", sorted(set(workloads.REPOSVUL_INSTANCES) - have) or "none")
rows = [{"instance_id": iid, "problem_statement": REPOSVUL_TASK_PROMPT, "repo": iid}
        for iid in ids]
out = os.path.join(os.environ["DATA"], "reposvul_tasks.json")
json.dump(rows, open(out, "w"))
print(len(rows), "->", out)
PY
export TOKENECONOMIZER_REPOSVUL_TASKS="$DATA/reposvul_tasks.json"        # (A) JSON list, NOT a dir
```

**Run:**
```bash
AGENT=claude DOMAIN=reposvul ARM=trimech SEEDS="0 1 2" \
  RUNNER=your_pkg.harness:make_runner bash scripts/launch/trimech_ab.sh
```

---

## 5. CyberGym (exploratory — PoC authoring)

**Dataset.** CyberGym tasks give a vulnerable workspace and ask the agent to author a
proof-of-concept input that triggers the vulnerability. Provisioning is via a single **suite
JSON** listing tasks; per-task workspaces are materialized by your harness.

```bash
# Fetch/prepare the suite per your CyberGym source, then:
export TOKENECONOMIZER_CYBERGYM_SUITE="$DATA/cybergym/suite.json"        # (B) adapter reads this
```

CyberGym has **no `trimech_ab.sh` domain case** — the launchers cover swebench / webbench /
creddata / reposvul only. Run it by calling the driver directly, supplying the driver task
list (A) yourself and `--adapter cybergym`:

```bash
# (A) a JSON list of {instance_id, problem_statement, repo}; problem_statement can be the
#     fixed CYBERGYM_TASK_PROMPT (see tokeneconomizer/adapters/cybergym.py).
python scripts/run_experiment.py \
  --arm trimech --agent claude --adapter cybergym \
  --tasks-file "$DATA/cybergym_tasks.json" --repo "$PROJECT" --seed 0 \
  --results-root "$TOKENECONOMIZER_RESULTS/claude_cybergym" \
  --runner your_pkg.harness:make_runner
python scripts/aggregate.py "$TOKENECONOMIZER_RESULTS/claude_cybergym" --arms baseline trimech
```
The adapter reads the suite JSON to key the per-task judge; PoC building + triggering live in
the harness.

---

## 6. WebArena — not implemented

`WebArenaAdapter` is a **stub**: every method raises `NotImplementedError` naming the exact
surface a WebArena integration must supply (episode→trajectory parser, per-action usage
counter, action-space catalog, waste detector, protected/inapplicable sets). It documents the
contract; there is nothing to provision until it is implemented.

---

## Offline check (no images, no datasets)

Before wiring any real harness, confirm the whole pipeline runs end-to-end with the built-in
`StubRunner` — it launches no agent, needs no image or dataset, and cleans up after itself:

```bash
bash scripts/launch/smoke_test.sh          # exercises every arm; asserts CSVs; deletes its temp dir
```

Its numbers are synthetic (a wiring check, not a benchmark). Real results require your
`AgentRunner`.
