# scripts/launch/ — reproducible experiment launchers

These scripts run the **main experiments**: a whole lane (`main_sweep.sh`, `full_sweep.sh`)
or an A/B (baseline vs. an economized arm, `trimech_ab.sh`, `codex_ab.sh`) over a set of repos
and seeds via [`../run_experiment.py`](../run_experiment.py), then aggregate the results into
the index CSVs `compare_arms.py` reads. `run_experiment.py` is the single source of truth
mapping each arm to an exact `EconomizerConfig`. The run book, with the arm catalog and one
command per lane, is [`../../docs/RUNNING.md`](../../docs/RUNNING.md).

## The harness seam (why you must supply a runner)

Task execution — launching a sealed agent (Claude Code / Copilot CLI / Codex) inside its
Docker image, applying the economizer's per-task decisions, and grading against the gold
patch — lives **below `te_bench.runner.AgentRunner`**, in
[`../../te_harness`](../../te_harness) (details: [`../../docs/HARNESS.md`](../../docs/HARNESS.md)).
Select it per launch; no `PYTHONPATH` is needed, since the driver puts `experiments/` and
the repository root (the shared core) on `sys.path` itself:

```
RUNNER=te_harness.swebench:make_runner       # SWE-bench
RUNNER=te_harness.webbench:make_runner       # Web-Bench
RUNNER=your_package.harness:make_runner      # or your own: any zero-arg callable -> AgentRunner
```

`RUNNER=stub` (the default) selects the built-in offline `StubRunner`: it runs **no agent**,
writes a synthetic transcript, and returns a fixed cache-heavy row. It exists only to
exercise the wiring end-to-end (facade → aggregate) with no Docker, no datasets,
and no LLM — its numbers are **not** a benchmark. `smoke_test.sh` uses it.

## Arm → exact configuration

| Arm | Original flags | Applies to |
|---|---|---|
| `baseline` | *(none)* | all — native cost basis |
| `trimech` | `--mode manual4 --untool-evidence 3 --untool-judge --spiral-guard --spiral-judge --spiral-nudge` | Claude / Copilot |
| `trimech-web` | `trimech` + `--spiral-nudge-llm --spiral-frozen-envelope 30` | Claude / Copilot on short Web-Bench streams |
| `codex-untool` | `--codex-recent-adaptive-untool-v7` | Codex |
| `codex-spiral` | `--codex-spiral-resume`, the **defensive** profile | Codex |
| `codex-inject` | `--mode manual4` on the composed `envfacts+distiller` profile (**paid**) | Codex |
| `codex-trimech` | `codex-inject` + `codex-untool` + `codex-spiral` — all three (**paid**) | Codex |
| `codex-trimech-rtk` | `codex-trimech` + rtk (instruction mode; row carries `rtk_compliance`) | Codex |
| `codex-trimech-repomap` | `codex-trimech` + aider's repo map as the project `AGENTS.md` (SWE-bench only; row carries `map_ok`) | Codex |

`rtk`, `repomap` and `serena` all run on the Codex lane: rtk in its instruction mode, serena
as a `[mcp_servers.serena]` block in the per-task `config.toml` (rows carry `serena_tool_calls`).

The Codex arms run on **both** domains: `codex_ab.sh` takes `DOMAIN=swebench` (default) or
`DOMAIN=webbench`, and refuses a `RUNNER` that does not match it.

## Detached by default

Both launchers **run in the background by default**. A real sweep is hours of Docker and
paid agent time; a closed laptop or a dropped ssh session should not cost you the run. The
launcher validates its config and credentials first, then re-execs itself under `setsid`
and returns your terminal:

```
[detach] trimech_ab__claude_swebench_trimech started in the background (DETACH=0 to run in the foreground)
[detach] log:    /data/te_results/logs/trimech_ab__claude_swebench_trimech__20260814-144811.log
[detach] follow: tail -f /data/te_results/logs/trimech_ab__claude_swebench_trimech__20260814-144811.log
[detach] stop:   kill -TERM -$(cat /data/te_results/logs/trimech_ab__claude_swebench_trimech__20260814-144811.pid)
```

Because the run gets its own session, it has no controlling terminal and survives SIGHUP.
Everything it spawns — driver, `docker exec`, the agent — shares one process group, so the
`kill -TERM -<pid>` above (note the leading `-`: that is the *group*) stops the whole tree.
The harness also traps TERM/INT/HUP and removes the task container it is holding, since
containers live outside that process group and would otherwise keep running — and keep
billing. Logs and pidfiles land under `$TOKENECONOMIZER_RESULTS/logs/`; override with
`TE_LOG=/path/run.log`.

`DETACH=0` runs in the foreground (streaming to your terminal) — the right choice for a
quick `LIMIT=1` check or when driving the launcher from another script. `smoke_test.sh` is
always foreground; it takes seconds and asserts its own result.

## Credentials (`.env`)

Real (non-stub) runs drive each agent CLI on its own native authentication. Copy the
tracked template and fill it in — `.env` is git-ignored:

```bash
cp .env.example .env      # then set the key for the agent you run
```

Both launchers source [`_env.sh`](_env.sh) and `run_experiment.py` calls
[`../_env.py`](../_env.py)`.load_env()`, so the file is picked up automatically. Format is
plain `KEY=value` (`#` comments, optional `export`, no `$VAR` interpolation); **variables
already exported in your shell win over the file**, and `KEY=` counts as unset. Point at a
different file with `TOKENECONOMIZER_ENV_FILE=/path/to/other.env`.

| Agent | Credential |
|---|---|
| Claude Code | `ANTHROPIC_API_KEY`. `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` are ignored: the launcher says so once and unsets them. |
| Codex | `OPENAI_API_KEY`. `OPENAI_BASE_URL` only for a region-scoped key (e.g. `https://us.api.openai.com/v1`); any non-OpenAI host is refused. |
| Copilot CLI | the GitHub subscription (`COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`, or `copilot` then `/login`), or Copilot's own BYOK (`COPILOT_PROVIDER_*`, see [`../../docs/COPILOT.md`](../../docs/COPILOT.md)). |

Before each real run the launcher checks the credential for its agent and fails fast when it
is missing.

## Placeholders (env vars)

> **Provisioning each benchmark** — what image to pull, dataset to fetch, and the exact
> commands — is in [`../../docs/BENCHMARKS.md`](../../docs/BENCHMARKS.md). It explains the two
> path families every non-SWE-bench domain needs: a **`_TASKS` JSON file** (the driver's task
> list) *and* a **`_ROOT`/`_PROJECTS`/`_SUITE` dataset root** (the adapter's data). For
> SWE-bench those are the same file.

The launchers themselves read only these:

| Env var | What it is |
|---|---|
| `TOKENECONOMIZER_SWEBENCH_TASKS` | JSON list of `{instance_id, problem_statement, repo}` (SWE-bench; also the adapter source) |
| `TOKENECONOMIZER_WEBBENCH_TASKS` / `_CREDDATA_TASKS` / `_REPOSVUL_TASKS` | driver task-list JSON for those domains (a **file**, not a dir) |
| `TOKENECONOMIZER_RESULTS` | root of the results trees (outside the repository) the aggregator reads |
| `RUNNER` | `stub`, `te_harness.swebench:make_runner` / `.webbench:make_runner`, or any `module:factory` |
| `WORKLOAD` | workload manifest (default `workloads/$DOMAIN.json`); `none` disables filtering |
| `PYTHON` | interpreter to use (default `python3`) |

**Workload pinning.** Both launchers default `WORKLOAD` to the committed manifest in
[`../../workloads/`](../../workloads/) and pass it through as `run_experiment.py --workload`,
which filters `TASKS_FILE` to exactly the evaluated instances for the `--repo` group. Your
task file may therefore hold a superset (e.g. all of SWE-bench_Verified). This also avoids the
legacy `--repo` prefix match, which silently falls back to the *entire* task list whenever the
repo's org differs from its name — `pydata/xarray` included. Set `WORKLOAD=none` to opt out.

The **dataset roots** (`_WEBBENCH_PROJECTS`, `_CREDDATA_ROOT`, `_REPOSVUL_ROOT`,
`_CYBERGYM_SUITE`) are read by the adapters + your harness, not by these scripts — export them
per [`../../docs/BENCHMARKS.md`](../../docs/BENCHMARKS.md) before launching.

## Examples

**Claude tri-mechanism on SWE-bench, three seeds:**
```bash
export TOKENECONOMIZER_SWEBENCH_TASKS=/data/swebench_verified.json
export TOKENECONOMIZER_RESULTS=/data/te_results
AGENT=claude DOMAIN=swebench ARM=trimech \
  REPOS="pydata/xarray django/django sympy/sympy scikit-learn/scikit-learn" \
  SEEDS="0 1 2" RUNNER=te_harness.swebench:make_runner \
  bash scripts/launch/trimech_ab.sh
```

**Tri-mechanism on Web-Bench (short streams → `trimech-web`):**
```bash
AGENT=claude DOMAIN=webbench ARM=trimech-web SEEDS="0" \
  REPOS="calculator expressjs react zustand table" \
  RUNNER=te_harness.webbench:make_runner bash scripts/launch/trimech_ab.sh
```

Each `REPOS` value is a Web-Bench **project** — one stream of 20–21 sequentially dependent
tasks (`init`, `task-1` … `task-20`). Two Web-Bench-specific notes:

* Web-Bench's evaluator owns the task loop, so a Web-Bench runner is started once per stream
  through the optional `begin_stream(tasks, ...)` / `end_stream()` hooks on `AgentRunner`
  (see `experiments/te_bench/runner.py`). Runners that do not define them are called as usual.
* Because the tasks are sequentially dependent and graded cumulatively, the evaluator stops
  the project at the first task that fails every attempt. The driver still asks for the rest;
  a runner should return those as unreached (`stream_broken` / `unreached`, zero tokens) so
  the cost aggregation does not read them as cheap failures.

**Codex untooling A/B:**
```bash
ARM=codex-untool REPOS="pydata/xarray sympy/sympy" SEEDS="0" \
  RUNNER=te_harness.swebench:make_runner bash scripts/launch/codex_ab.sh
```

**Codex spiral A/B:**
```bash
ARM=codex-spiral REPOS="pydata/xarray" SEEDS="0" \
  RUNNER=te_harness.swebench:make_runner bash scripts/launch/codex_ab.sh
```

## Offline smoke test

`bash scripts/launch/smoke_test.sh` runs **every** arm with the StubRunner on a tiny
synthetic task set, aggregates, asserts the CSVs are produced, and **deletes everything it
created** (it writes only under a temp dir). Use it to confirm the driver + facade +
aggregator wiring after any change. See `../README.md` for the full reproduction chain.
