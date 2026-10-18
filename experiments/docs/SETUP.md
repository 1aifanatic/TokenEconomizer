# Setup — from `git clone` to a real run

A sequential walkthrough of the four things a fresh clone needs: a **venv**, a
**data directory**, **credentials**, and a **runner**. Every step ends with a
gate that tells you immediately whether it worked, so a failure lands on the step that
caused it instead of surfacing hours later as an impossible benchmark result.

Reference detail lives elsewhere and is linked rather than repeated:
[`ENVIRONMENT.md`](ENVIRONMENT.md) for requirements and Docker install,
[`BENCHMARKS.md`](BENCHMARKS.md) for per-benchmark dataset provisioning,
[`../scripts/launch/README.md`](../scripts/launch/README.md) for launcher flags.

```
Step 1  venv                    → pytest + smoke test pass          (no Docker, no keys)
Step 2  data directory          → $DATA exists, canonical
Step 3  credentials             → each agent's key answers, free
Step 4  runner                  → one real task produces a graded row
Step 5  per-benchmark data      → the benchmark can grade its own answer key
Step 6  first A/B               → index CSVs compare_arms.py reads
```

---

## Step 1 — the venv (Layer 1)

```bash
git clone <repo-url> TokenEconomizer && cd TokenEconomizer
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e '.[test]'
cd experiments                                         # every command below runs from here
```

The core is stdlib-only; the `test` extra adds `pytest`. Other install shapes
(`requirements.txt`) are in [`ENVIRONMENT.md`](ENVIRONMENT.md).

**Running the full arm catalog needs two more extras.** The run book lists 12 SWE-bench arms
per agent, and three of them carry an external tool baseline with a host dependency:

```bash
pip install -e '..[test,harness,baselines]'
```

| extra | what it adds | arms that need it |
|---|---|---|
| `harness` | nothing on PyPI — it names the intent to run `te_harness/` (Docker, an agent CLI, a key, benchmark images) | every arm |
| `baselines` | `aider-chat`, for the repo map generated on the HOST | `repomap`, `trimech-repomap`, `codex-trimech-repomap` |

`rtk` and `serena` need no host package — rtk ships a static binary (staged into
`TE_SCAFFOLD_CACHE`, else `$DATA/scaffolds`, from Step 2) and Serena installs itself inside
the task container. Skip `baselines` only if you will never run a repo-map arm: without it
those arms raise `ScaffoldError` at runner construction, naming this command, so a lane fails
at launch rather than 200 tasks in.

**Gate:**

```bash
python -c "import tokeneconomizer as te; print(te.Economizer)"   # self-contained import
pytest                                                            # offline suite
bash scripts/launch/smoke_test.sh                                 # every arm wired
```

Expect `pytest` all-pass and `SMOKE PASS: all arms wired; no artifacts left.` This works with
no Docker, no datasets and no API keys — if it passes, the repo layer is healthy and
everything after this point is external infrastructure.

> Use **this** venv for the driver later, and install the SWE-bench grader into it (Step 4).
> A venv that can run `pytest` but cannot import the grader will fail only once a real run
> reaches grading.

---

## Step 2 — the one directory outside the repo

The `AgentRunner` implementation is **in** the repo — [`te_harness/`](../te_harness) — so
there is no harness directory to create and no
`PYTHONPATH` to export: `scripts/run_experiment.py` puts `experiments/` and the repository
root (the shared core) on `sys.path` itself,
so `--runner te_harness.swebench:make_runner` resolves from any working directory. (Writing
your own remains supported; put it anywhere importable and pass
`--runner your_pkg:make_runner`.)

What does **not** belong in the repo is data — what a benchmark ships and what runs produce.
One directory, created wherever you like:

| Variable | Holds | Contents |
|---|---|---|
| `$DATA` | **what a benchmark ships + what runs produce** | task lists, grading keys, benchmark checkouts, results |

Two things deliberately live in neither: **Docker images** (they belong to Docker's own
store — `docker images` is the inventory, never copy them into `$DATA`) and the **SWE-bench
grader checkout**, which is a pip-installable library and can sit anywhere you point
`TE_SWEBENCH_REPO`.

```bash
export DATA="$(readlink -f ~/te_data)"
mkdir -p "$DATA/results"
```

`readlink -f` is not cosmetic. If your home is a symlink (`/home/you` → `/srv/home/you`),
Web-Bench's Vite-based project resolves its entry module against a root that disagrees with
node's already-resolved `process.cwd()`, and **every** test fails with an app that never
rendered — indistinguishable from an agent that wrote nothing. Canonicalize once, here.

Keep the exports in one sourceable file so every shell, launcher and hand-run command agrees:

```bash
cat > "$DATA/env.sh" <<'EOF'
# TokenEconomizer data root — source me
export DATA="$(readlink -f ~/te_data)"
export TOKENECONOMIZER_RESULTS="$DATA/results"
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"
EOF
. "$DATA/env.sh"
```

### Why a benchmark checkout goes in `$DATA`, not the repo

Some benchmarks ship their grader *with* their data, inseparably. Web-Bench is one monorepo
holding both its Node/Playwright evaluator **and** its task data
(`projects/<project>/tasks.jsonl`, `src-init/`, `test/`), and its evaluator resolves
`projects/` relative to its own location — so the tree cannot be split. Of its ~1.9 GB,
~1.7 GB is regenerable `node_modules`/pnpm store and ~158 MB is the actual dataset. Moving
it into the repo would commit a real dataset in order to evict build artifacts from the data
directory, and would leave the dataset variable pointing inside a git tree. So: **a benchmark
checkout is a benchmark payload — it lives in `$DATA`**, and the repo stays code only.

Results are also `$DATA` (they are what runs produce) and are git-ignored by the repo.
Old run archives are worth parking under `$DATA/archive/` rather than at the top level.

---

## Step 3 — credentials

```bash
cp .env.example .env        # .env is git-ignored
```

`.env` is plain `KEY=value`, no interpolation, and **a variable already exported in your
shell always wins** — so a one-off `ANTHROPIC_API_KEY=... bash scripts/launch/...` overrides
the file. Each agent runs on its own native authentication; fill in the ones you will run:

| Key | For | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude Code | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` are ignored and unset by the launchers |
| `OPENAI_API_KEY` | Codex | |
| `OPENAI_BASE_URL` | Codex | **only** for a region-scoped key (`https://<region>.api.openai.com/v1`) — such a key 401s on the global endpoint; any non-OpenAI host is refused |
| `COPILOT_GITHUB_TOKEN` / `GH_TOKEN`, or `COPILOT_PROVIDER_*` | Copilot CLI | the GitHub subscription, or Copilot's own BYOK — see [`COPILOT.md`](COPILOT.md) |
| `ANTHROPIC_MODEL`, `CODEX_MODEL` | cost basis | must exist in `pricing.MODEL_WEIGHTS`, which hard-fails rather than mispricing an unknown id |
| `TE_EFFORT_LEVEL` | cost basis | `low\|medium\|high\|xhigh\|max`. Keep **both arms of an A/B identical**: results at different levels are not directly comparable |

**Gate — prove the keys without spending a token.** The model-list endpoints are free:

```bash
set -a; . ./.env; set +a
curl -s -o /dev/null -w 'anthropic %{http_code}\n' https://api.anthropic.com/v1/models \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01"
curl -s -o /dev/null -w 'openai    %{http_code}\n' "${OPENAI_BASE_URL:-https://api.openai.com/v1}/models" \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

Each key you set should give `200`. Fetch the listings in full once and check that your
configured `ANTHROPIC_MODEL` / `CODEX_MODEL` ids actually appear — a typo here fails much
later, inside a container, after you have paid for the containers that came before it. The
Copilot gate is in [`COPILOT.md`](COPILOT.md) Step 1.

---

## Step 4 — the runner

The heavy machinery — launching a sealed agent, applying the economizer's per-task
decisions, grading against gold — sits **below `AgentRunner`**
([`te_bench/runner.py`](../te_bench/runner.py), which is the authoritative
contract). The repo ships a reference implementation, [`te_harness/`](../te_harness) — see
[`HARNESS.md`](HARNESS.md) — so this step is a check
rather than something to write. Every launch selects a runner with `RUNNER=module:factory`
(a zero-arg callable returning the runner), which is also how you'd point it at your own.

What a runner owes the economizer:

| Member | Contract |
|---|---|
| `optimizer_call(prompt, model)` | one small paid LLM request → `(text, effective_tokens)`; used by the between-task proposer / judge / nudge-writer. Must never raise — return `("", 0.0)` on a transport failure so the mechanisms fall back to their LLM-free path |
| `run_task(task, *, arm, injected, exclude_tools, run_dir_path, **kw)` | run one task with those decisions applied → `(row, run_dir)` |
| `begin_stream(tasks, **info)` / `end_stream()` | **optional.** For a harness that owns a whole stream rather than one task (Web-Bench's evaluator drives its project's 20 sequentially dependent tasks itself). The driver calls them `hasattr`-guarded, so runners without them are unaffected |

`run_task` must write `transcript.jsonl` under `run_dir_path` (the schema
`tokeneconomizer.trace` re-reads) and return a row carrying the four-field token breakdown
`pricing.effective_tokens` reads, plus `resolved` and `num_turns`. Recording a
`run_config.json` beside it is strongly advised: agent CLIs record neither the reasoning
effort nor the tool mask in their own transcripts, so without it a finished run cannot answer
"what settings produced this?".

Pick it per launch. No `PYTHONPATH` is needed for the in-repo harness — the driver puts
`experiments/` and the repository root on `sys.path` itself, so `te_harness.*` resolves from any working directory; a
harness of your own just has to be importable by the same interpreter.

`.env` ships `RUNNER=stub`, the built-in offline `StubRunner`. It runs **no agent and grades
nothing** — its `resolved` is a fixed deterministic pattern — so set `RUNNER` explicitly on
every real launch. With more than one domain you will have more than one runner, which is
why this is a per-launch flag rather than a value you set once:

```bash
RUNNER=te_harness.swebench:make_runner   # SWE-bench
RUNNER=te_harness.webbench:make_runner   # Web-Bench
```

> The factory is handed the driver's `--agent` label (`factory(agent=...)`, falling back to
> a zero-arg call for older harnesses, with the label also exported as `TE_AGENT`). A factory
> that ignores it will run whatever agent it hardcodes while the driver files the rows under
> `--agent`'s name — plausible numbers attributed to an agent that never ran. If your harness
> supports one agent only, raise on an unexpected label rather than ignoring it. Running the
> **Copilot CLI** instead of Claude Code: [`COPILOT.md`](COPILOT.md).

**Gate:** launch with `RUNNER=stub` and confirm the banner
`[env] RUNNER=stub -- SYNTHETIC run: no agent, no grading, numbers are NOT a benchmark`.
That line is your standing check that you are pointed at a real harness; if you ever see it
on a run you meant to be real, stop.

### Installing the SWE-bench grader

The official grader is a library the runner imports, so it must be importable from the **same
interpreter that runs the driver**:

```bash
git clone https://github.com/princeton-nlp/SWE-bench.git "$DATA/SWE-bench"
pip install -e "$DATA/SWE-bench"        # into the Step-1 venv
echo 'export TE_SWEBENCH_REPO="$DATA/SWE-bench"' >> "$DATA/env.sh"
```

**Gate:** `python -c "from swebench.harness.grading import get_eval_report; print('ok')"`.
Its import chain pulls `bs4`/`datasets`, so a venv that skipped `pip install -e` on the
checkout raises `ModuleNotFoundError` — and only at grading time, after the agent has run and
billed.

---

## Step 5 — per-benchmark data

Full fetch-and-layout instructions per benchmark are in
[`BENCHMARKS.md`](BENCHMARKS.md); provision only what the workload names
(`workloads/*.json`), which is a small fraction of each full benchmark. What belongs here is
the discipline that saves money:

> **Verify a benchmark can grade its own answer key before you spend anything on an agent.**
> A benchmark that cannot pass its own reference solution cannot grade a model, and the
> failure looks identical to a model that produced nothing.

For **SWE-bench** that means, in order: the grading file covers every workload id you intend
to run; every one of those ids has a local image (`docker images`); then one task at
`--arm baseline --limit 1` whose `row.json` carries a `resolved` from the official report and
a full token breakdown.

For **Web-Bench** that means installing *and building* the monorepo (an installed-but-unbuilt
checkout has no `dist/` to run — they are separate Rush commands), installing the Playwright
browser build the projects **pin** (a mismatch surfaces as `Executable doesn't exist`, not as
a version warning), and then running each project's own `src` through its own suite and
recording the expected pass counts. Some projects need their build step first to generate a
client or seed a database — exactly what the evaluator does per task — so a bare test run
under-reports.

Whatever your harness offers as an offline wiring check, run it before the first paid task:
it should exercise the real grader with the agent call replaced, so **every task comes back
resolved** and anything else is a harness fault rather than a model result.

---

## Step 6 — the first A/B

```bash
. "$DATA/env.sh"
AGENT=claude DOMAIN=swebench ARM=trimech SEEDS="0" \
  REPOS="pydata/xarray django/django sympy/sympy" \
  RUNNER=te_harness.swebench:make_runner bash scripts/launch/trimech_ab.sh
```

Each launcher runs `baseline` then the economized arm over your repos and seeds, then
aggregates into `index/all_rows.csv` + `index/summary_by_cell.csv`; compare the two arms
cell by cell with `scripts/compare_arms.py`. Launches **detach by
default** and print the log path plus the `kill -TERM -<pid>` line that stops the whole
process group (`DETACH=0` to stay in the foreground).

Read [`RUNNING.md`](RUNNING.md) for the arm catalog and one command per lane,
[`../scripts/launch/README.md`](../scripts/launch/README.md) for every env var, and
[`../scripts/README.md`](../scripts/README.md) for the chain from rows to comparisons.

### Where the traces land

```
$TOKENECONOMIZER_RESULTS/<agent>_<domain>_<model>/  e.g. claude_swebench_opus48
├── <arm>/<repo-or-project>_s<seed>/                the CELL: one arm x one stream x one seed
│   ├── 20260826T204556Z__000__claude__<instance>__<arm>__s<seed>__<mode>/
│   │   ├── transcript.jsonl     the raw agent stream — the primary artifact
│   │   ├── row.json             the normalized row (tokens, resolved, telemetry)
│   │   ├── run_config.json      everything that shaped the run (effort, tool mask, …)
│   │   └── …                    domain extras: model_patch.diff + eval.log (SWE-bench),
│   │                            agent_request_<n>.json (Web-Bench)
│   ├── _streams/<stream>__<mode>__stream.jsonl     the per-cell amortized cost curve
│   └── _webbench_<project>/     Web-Bench only: the evaluator's log, config and report
└── index/                       aggregate.py output: all_rows.csv, summary_by_cell.csv
```

The run-dir name is `<UTC>__<index>__<agent>__<instance>__<arm>__s<seed>__<mode>`:

* **the UTC stamp comes first**, so every run dir is unique and the tree sorts
  chronologically. This is the part that matters operationally — **re-running a cell
  archives the previous attempt beside the new one instead of overwriting its transcript.**
  A trace cannot be reproduced after the fact, so losing one to a re-run loses it for good.
* **the stream index** (`000`, `001`, …) keeps stream order explicit — which is essential
  for a sequentially dependent Web-Bench stream — and breaks ties when two tasks land in
  the same second (an unreached Web-Bench task costs no wall-clock at all).
* **`<mode>`** stays in the name because `aggregate.py` recovers it from there.

Because re-runs accumulate, `aggregate.py` reports **one row per (arm, cell, instance),
taken from the newest attempt**, and prints `superseded runs skipped: N` when it passed over
older ones. So the CSVs always describe the latest run of each task while the earlier traces
stay on disk. To retire a bad run rather than supersede it, move its dir under
`<tree>/provenance/` — the aggregator only walks the arm dirs.

**One interpretation rule before you read any Web-Bench numbers.** Its streams are
sequentially dependent, so the evaluator stops a project at the first task that fails every
attempt; the driver still asks for the tasks after it, and a correct harness returns those as
unreached with zero tokens. They are **not** cheap failures — exclude them from any per-task
cost mean.

---

## Troubleshooting — symptom → cause

| Symptom | Cause |
|---|---|
| `ModuleNotFoundError: No module named 'bs4'` at grading | SWE-bench grader not installed into the driver's interpreter (Step 4) |
| Rows look plausible but resolve follows a fixed pattern | `RUNNER=stub`; check the launch banner (Step 4) |
| Every Web-Bench test fails, app never rendered | non-canonical path — `readlink -f` your roots (Step 2) |
| `Executable doesn't exist at .../chromium_headless_shell-<n>` | Playwright browser does not match the version the projects pin (Step 5) |
| "the evaluator is not built" / no `dist/` | `rush install` ran but `rush build` did not (Step 5) |
| A whole project's tests fail that passed before | a build/seed step was skipped, or a stale database file was left behind (Step 5) |
| Codex 401s despite a valid key | a regional OpenAI key is hitting the global endpoint; set `OPENAI_BASE_URL` (Step 3) |
| `pricing: no weight entry for billed model …` | the billed model id is not in `pricing.MODEL_WEIGHTS`; add it rather than letting it be mispriced |
| Two runs differ in cost at equal resolve | `TE_EFFORT_LEVEL` differs between them (Step 3) |
