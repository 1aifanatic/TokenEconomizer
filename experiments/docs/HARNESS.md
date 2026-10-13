# te_harness — Layer-2 task-execution harness for TokenEconomizer

Everything that touches Docker, an agent CLI, or a credential lives **below**
`tokeneconomizer.runner.AgentRunner`. [`te_harness/`](../te_harness) is this repo's
implementation of that seam. Supplying your own instead is a supported path, not a
fallback: pass `--runner your_pkg.harness:make_runner` and nothing above the seam changes.

**It is a sibling package of `tokeneconomizer`, not a submodule of it**, and `pyproject.toml`
excludes it from the distributed wheel. That boundary is the point: `import tokeneconomizer`
cannot reach Docker or an agent CLI because the code that shells out to them is not
underneath it, so the core's stdlib-only guarantee is structural rather than a convention.
It costs nothing at install time either — this package is stdlib-only at import time too
(the official SWE-bench grader is imported lazily inside `swebench._import_swebench`, from
the checkout `TE_SWEBENCH_REPO` names), which is why the `harness` extra installs no
packages. What it needs cannot come from pip: Docker, an agent CLI on `PATH`, an API key,
the benchmark images, and for Web-Bench a built Rush monorepo.

Because the harness is versioned alongside the mechanisms, a result is attributable to a
harness commit and not merely to "whatever was on disk that day". Anything that changes
recorded cost, `resolved`, or turn counts is a reviewable diff — which matters more than it
sounds, since the accounting these rows are built from lives here, not above the seam.

It holds one runner per domain, each executing tasks with a **real sealed coding agent** and
grading with **that benchmark's own official grader**, returning the row + transcript the
economizer's mechanisms consume:

| Runner | Domain | Agent runs | Graded by |
|---|---|---|---|
| `te_harness.swebench:make_runner` | SWE-bench | inside the instance's official Docker image | the official SWE-bench harness (`get_eval_report`) |
| `te_harness.webbench:make_runner` | Web-Bench | on the host, over the task's file set | Web-Bench's own Node evaluator + Playwright suites |
| `te_harness.webbench_smoke:make_runner` | Web-Bench | **not at all** — the reference solution stands in | same as above (offline wiring check) |

## Agents

Both real factories take the driver's `--agent` label and return that agent's runner. Each
agent runs on its own native authentication: Claude Code on `ANTHROPIC_API_KEY`, Codex on
`OPENAI_API_KEY`, the Copilot CLI on its GitHub subscription or its own BYOK.

| `--agent` | SWE-bench | Web-Bench |
|---|---|---|
| `claude` (default) | `ClaudeSwebenchRunner` | `WebBenchRunner` |
| `copilot` | `CopilotSwebenchRunner` | `CopilotWebBenchRunner` |
| `codex` | `CodexSwebenchRunner` | `CodexWebBenchRunner` |

An unknown label **raises**. It must never fall back: a factory that quietly returned the
Claude runner for `--agent copilot` would produce a full tree of Claude rows filed under
Copilot's name — plausible numbers for an agent that never ran, which is worse than a crash.

Everything Copilot-specific — GitHub auth instead of an API key, its own model catalog,
token recovery from the debug log, and the absence of a turn cap — lives in
[`te_harness/copilot.py`](../te_harness/copilot.py); setup is in [`COPILOT.md`](COPILOT.md).
The Codex seam — the capability→config mapping and its own accounting — is
[`te_harness/codex.py`](../te_harness/codex.py). The two Copilot runners
subclass the Claude ones and override only construction, launch, and accounting; the task
lifecycle (container, spiral segments, grading, evaluator round-trip) is shared.

## Use

```bash
cd /path/to/TokenEconomizer
set -a; . ./.env; set +a            # ANTHROPIC_API_KEY etc.

python3 scripts/run_experiment.py \
  --arm baseline --agent claude --repo pydata/xarray --seed 0 --limit 1 \
  --adapter swebench --tasks-file "$TOKENECONOMIZER_SWEBENCH_TASKS" \
  --workload workloads/swebench.json \
  --results-root "$TOKENECONOMIZER_RESULTS/claude_swebench" \
  --runner te_harness.swebench:make_runner --fresh
```

The launchers take the same seam: `RUNNER=te_harness.swebench:make_runner bash
scripts/launch/trimech_ab.sh`.

No `PYTHONPATH` is needed for either form. `scripts/run_experiment.py` puts `experiments/`
and the repository root (the shared core) on `sys.path` itself, so `--runner te_harness.…` resolves from any working directory. (While the
harness lived outside the repo this required exporting `PYTHONPATH`, and forgetting it failed
a launch at import time — or, worse, appeared to work by namespace-package accident whenever
the shell happened to be sitting in the harness's parent directory.)

## What one task does

1. **Start** `swebench/sweb.eval.x86_64.<instance>:latest` detached, with the host's
   Claude Code binary bind-mounted in. It is a static ELF, so it runs in the image
   unmodified — no image rebuild, no Node, no network install.
2. **Run the agent** at `/testbed` via `claude -p --output-format stream-json`, applying the
   economizer's per-task decisions: the injected manual prefix goes in the prompt, the
   untooling mask becomes `--disallowed-tools`, and the spiral guard folds the live stream
   through `NudgeGuard.monitor`. Every raw stream line is written to `transcript.jsonl` —
   the exact schema `tokeneconomizer.trace` re-reads afterwards.
3. **Spiral rung 1**: on a guard nudge, the agent is re-invoked in the *same* container on
   top of its own on-disk edits, with the leakage-safe redirect appended. On an abort, the
   agent is killed inside the container (killing the `docker exec` client alone would leave
   it running and billing).
4. **Grade** with `swebench.harness.test_spec` + `.grading` — the same `get_eval_report`
   the official `run_evaluation` calls — running the instance's real eval script in the
   same container. The eval script resets test files and applies the instance's own
   `test_patch`, so agent edits to tests are discarded exactly as upstream discards them.
5. **Return** `(row, run_dir)` with the cache-aware token breakdown from the CLI's final
   `result` event, `resolved`, `num_turns`, and the spiral/untool telemetry the mechanisms
   read. When the agent was killed before that event, the breakdown is recovered from the
   transcript and the row is flagged — see **Accounting** below.

### Accounting: rows that are a lower bound

Claude Code reports its token breakdown **only** in the stream's terminal
`{"type":"result"}` event, and both of `_segment`'s break paths — a spiral guard stop and
`TE_TASK_TIMEOUT` — kill the CLI before it arrives. The segment used to return `{}`, so the
row recorded **zero** for every token field and for `num_turns`, describing a rollout that
had really run for tens of turns and hundreds of thousands of tokens as free.

Only economized arms install a guard, so the understatement was **one-directional**: it
flattered exactly the arms under test and could never touch a baseline. Grading was
unaffected (it reads the patch), but cost and turn-based claims were not.

`_UsageRecovery` now folds usage in as the stream arrives and rebuilds a `result`-shaped
event when none comes. What that can and cannot recover:

| Field | Recoverable? | How |
|---|---|---|
| `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` | **exactly** | per-message `usage` snapshots deduplicated on `message.id` (they are cumulative and repeat per content block, so summing every event double-counts). Validated against 60 healthy rows that *do* have a `result` event: 60/60 exact on all three |
| `num_turns` | approximately | deduplicated assistant-message count — exact on 107 of 120 healthy rows, within 1 on 12 more |
| `output_tokens` | **no** | the per-message figure is the `message_start` placeholder, understating the truth 7.5×–134.7× (median 28×). Reported as 0 and flagged, never estimated |

So a recovered row is honest but incomplete and says so: `usage_incomplete` with
`output_tokens_missing`, `num_turns_recovered`, and `usage_incomplete_reason`. The flags are
present-and-`False` on a healthy row, so an absent flag means "written before this check
existed", not "verified complete". They OR across spiral segments, because degradation is a
property of the sum — the usual shape is a first segment that completes and a second the
guard kills.

**A flagged row's cost is a floor, not a measurement.** `pricing.effective_tokens` is
deliberately left alone: this reports the defect rather than papering over it.
`scripts/aggregate.py` surfaces `usage_incomplete` in `all_rows.csv`, counts it per cell as
`n_usage_incomplete` in `summary_by_cell.csv`, and prints a warning naming the affected arms
— so a distorted mean cannot be read without the distortion beside it. It also recognises
rows stored *before* the fix by their signature (no tokens, yet the task resolved or left a
patch) and flags them `legacy_zero_usage`.

Copilot was never affected: it mines its debug log after the kill (`cat {logdir}/*.log`),
which is why the audit found zero affected rows in 2,571. Its own degraded case is a log it
could not parse, which `copilot.row_usage` already reports as
`tokens_source="stream_output_only"` with `None` — not 0 — in the unaccounted fields. Both
Copilot runners map that onto the same `usage_incomplete` flag, so one filter covers every
row this harness writes.

Each run dir therefore holds:

| File | What it is |
|---|---|
| `transcript.jsonl` | raw `stream-json` events, the schema `tokeneconomizer.trace` re-reads |
| `run_config.json` | **everything that shaped the run** — effort, model, turn cap, CLI build, routing mode, the tool mask actually applied, spiral params, injected-manual hash |
| `model_patch.diff` | the agent's working-tree diff, i.e. what was graded |
| `eval.log` | the official eval script's output, i.e. why it graded that way |
| `row.json` | the normalized row (written by the driver, not the harness) |

`run_config.json` exists because the CLI records **neither** the effort level nor the tool
mask in its own transcript — without it, a finished run cannot answer "what settings
produced this?", and an effort default that silently changed would be undetectable after
the fact.

Root note: SWE-bench images run as root, where the CLI refuses
`--dangerously-skip-permissions` unless `IS_SANDBOX=1` says it is containerized. These
disposable per-task containers are exactly that, so the harness sets it.

## Web-Bench

```bash
cd /path/to/TokenEconomizer
set -a; . ./.env; set +a

python3 scripts/run_experiment.py \
  --arm baseline --agent claude --repo calculator --seed 0 \
  --adapter webbench --tasks-file "$TOKENECONOMIZER_WEBBENCH_TASKS" \
  --workload workloads/webbench.json \
  --results-root "$TOKENECONOMIZER_RESULTS/claude_webbench" \
  --runner te_harness.webbench:make_runner --fresh
```

Or a full A/B over the five evaluated projects (`trimech-web` is the arm tuned for
Web-Bench's short streams):

```bash
AGENT=claude DOMAIN=webbench ARM=trimech-web \
  REPOS="calculator expressjs react zustand table" SEEDS="0" \
  RUNNER=te_harness.webbench:make_runner \
  bash scripts/launch/trimech_ab.sh
```

`--repo` names the Web-Bench **project**, which is one stream of 20–21 sequentially
dependent tasks (`init`, `task-1` … `task-20`). One driver invocation runs one project.

### Why this one is different

Web-Bench inverts the control flow. Its Node evaluator owns the task loop — copy the
previous task's workspace forward, collect the file set, ask the agent for files, write them
back, run the project's build, screenshot, then grade with the project's Playwright suite
**cumulatively** (`init` + `task-1..task-N`, so a task passes only if every earlier task
still passes). The economizer's driver owns *its* loop and expects one `run_task` per task.

The runner joins the two at the evaluator's own HTTP agent seam, so neither side is
reimplemented. Per project it starts a serialized HTTP server speaking the evaluator's
agent contract and `@web-bench/eval` restricted to that project, pointed at that server.
`run_task` then blocks on a rendezvous: take the evaluator's request for this task, run
Claude Code over the request's files in a scratch workspace, answer with the files that
changed, and wait for the evaluator's verdict line for this task before returning the row.
`resolved` is Web-Bench's own graded verdict.

The driver reaches this through two optional `AgentRunner` hooks — `begin_stream(tasks)` /
`end_stream()` — because the evaluator has to be started once for the whole stream, not
per task. Harnesses that do not define them are unaffected.

Two consequences of Web-Bench's contract shape what the mechanisms can observe, and neither
is a harness limitation:

* **The agent gets files, not an installed project.** Web-Bench's own reference agent is a
  single files-in/files-out LLM call; the request carries no `node_modules` and no test
  suite (the tests *are* the grader). Claude Code here is strictly more capable — it reads,
  greps and writes across the whole file set — but it cannot build or run the project, so
  trajectories are far shorter than SWE-bench's edit–test–fail loops. That is why the
  `trimech-web` arm exists.
* **A failed task ends the stream.** In the evaluator's `sequential` mode the project stops
  at the first task that fails every attempt. The driver still asks for the tasks after it;
  those rows come back `resolved: false` with `stream_broken: true`, `unreached: true` and
  zero tokens — unreached, not cheap. Aggregations must not read them as low-cost failures.

**Two resolve metrics, and they differ.** Each task gets up to `TE_WEBBENCH_RETRY`
attempts (default 2), so a row carries `resolved_p1` (first attempt — Web-Bench's own
headline metric) and `resolved_p2` (any attempt), alongside the per-attempt verdict list
`webbench_attempt_results`. `resolved` stays pass@N because that is what the evaluator
acts on when it decides to end a stream — and because a row's cost is the sum of every
attempt, pass@N is the metric the cost columns actually match. The retry can rescue several tasks
per arm, so never quote one metric as the other. Unreached tasks carry `None` for both —
never attempted is not a miss.

Each run dir holds the same artifacts as a SWE-bench run (`transcript.jsonl`,
`run_config.json`, `row.json`) plus `agent_request_<n>.json` — what the evaluator actually
asked for on each attempt. The evaluator's own stdout is teed to
`<results>/<arm>/<cell>/_webbench_<project>/evaluator.log`, and its report tree lands beside
it.

### Wiring check (no agent, no spend)

```bash
python3 scripts/run_experiment.py --arm baseline --agent claude --repo calculator --seed 0 \
  --adapter webbench --tasks-file "$TOKENECONOMIZER_WEBBENCH_TASKS" \
  --workload workloads/webbench.json --results-root /tmp/wb_smoke \
  --runner te_harness.webbench_smoke:make_runner --fresh
```

This is the production runner with `_segment` replaced by "copy the project's own reference
solution into the workspace". It exercises the real evaluator, the real HTTP contract and
the real Playwright grading, so **every task should come back resolved** — anything else is
a harness fault, not a model result. Run it after touching this file or upgrading the
Web-Bench checkout.

### Provisioning the checkout

The evaluator is a Rush monorepo and must be installed *and built*; the projects' Playwright
browsers must match the version they pin (`@playwright/test` 1.57.0 → browser build 1200).

```bash
cd "$(dirname "$TOKENECONOMIZER_WEBBENCH_PROJECTS")"
rush install                                        # once
rush build --to @web-bench/eval                     # the evaluator + its deps
rush build --to @web-bench/calculator --to @web-bench/expressjs --to @web-bench/react \
           --to @web-bench/zustand --to @web-bench/table
cd projects/calculator && npx playwright install chromium chromium-headless-shell
```

Verify the checkout independently of any agent by running a project's reference solution
through its own suite — this is what the wiring check automates, and what to reach for when
a real run's grades look impossible:

```bash
R="$TOKENECONOMIZER_WEBBENCH_PROJECTS"
cd "$R/calculator" && MAX_TEST_WORKERS=4 EVAL_PROJECT_ROOT="$R/calculator/src" \
  IS_EVAL_PRODUCTION=true EVAL=true \
  EVAL_PROJECT_PORT=3931 npm run test -- -a          # expect: 68 passed
```

`MAX_TEST_WORKERS=4` matches what the runner passes per grade
(`TE_WEBBENCH_MAX_WORKERS`, default 4, recorded in the row as `playwright_workers`).
Without it Playwright picks its own default — half the CPU count — and on a many-core host
the **reference solution** fails its own suite on the larger projects, which reads like a
broken checkout and is not one.

No project in the current five needs a special pre-build — `react` and `zustand`
are plain `vite build`, and `calculator`/`expressjs` build nothing. (The retired `prisma`
did: its `npm run build` generated the client and seeded `test.sqlite`, and skipping it
failed ~55 tests.) `expressjs` still fails one test if a stale `src/test.sqlite` is left
lying around.

> **Paths must be canonical.** Every path the evaluator derives comes from its entry
> script's `__dirname`, and it passes the project root to each project's dev server through
> `EVAL_PROJECT_ROOT` — while node reports `process.cwd()` already symlink-resolved.
> Reached through a symlinked home (`/home/x` → `/srv/home/x`), the two spellings disagree
> and Vite fails to resolve the `react` project's entry module against a root that is not
> the root it thinks it is: **every** test fails with an app that never rendered, which
> reads exactly like an agent that wrote nothing. `webbench_repo()` therefore
> `realpath`-resolves the checkout, and anything invoking these commands by hand should too.

## Config

Everything comes from the repo's `.env` (see its `.env.example`) plus:

| Var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude Code's key, and the mechanisms' calls on a Claude lane. `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` are reported once and ignored: the agent and the mechanisms bill `api.anthropic.com` directly |
| `OPENAI_API_KEY` | — | Codex's key. `OPENAI_BASE_URL` only for a region-scoped key on an OpenAI host |
| `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`, `COPILOT_PROVIDER_*` | — | Copilot's GitHub auth, or its own BYOK; see [`COPILOT.md`](COPILOT.md) |
| `TE_ENV_ACTIVATION` | `1` | `0` disables [project-environment activation](#project-environment-activation) and reproduces the pre-2026-09-11 (inactive `testbed` env) behaviour. Recorded per row |
| `ANTHROPIC_MODEL` | `claude-opus-5` | agent model (was `claude-opus-4-8`; still accepted) |
| `TE_EFFORT_LEVEL` | `high` | reasoning effort (`low`/`medium`/`high`/`xhigh`/`max`), passed as `--effort`. Validated at construction — a typo raises instead of silently using the CLI default |
| ~~`TE_OPTIMIZER_MODEL`~~ | **refused** | the between-task proposer / judge / nudge-writer runs on the **agent's own model**, on the agent's own authentication, in every harness (`routing.optimizer_transport` records the wire). Not a knob: charging mechanism overhead to a cheaper model would understate what the arm spends, against a baseline that made no such calls. Setting this variable now raises at construction rather than being half-honoured |
| `TE_CLAUDE_BIN` | from `which claude` | host Claude Code binary to mount |
| `TE_SWEBENCH_GRADING` | — (`$DATA/swebench/swebench_grading.json` if `DATA` is set) | `instance_id` → grading fields. **Required**: no default — unset raises at construction |
| `TE_SWEBENCH_REPO` | — | checkout supplying the official grader. **Required**: no default — unset raises at construction, rather than failing as an `ImportError` deep in grading after a task has been paid for |
| `TE_MAX_TURNS` | `80` | agent turn cap |
| `TE_TASK_TIMEOUT` / `TE_EVAL_TIMEOUT` | `1800` | wall-clock caps, seconds |
| `TE_KEEP_CONTAINERS` | unset | `1` leaves containers up for post-mortem |
| `TE_ALLOW_NETWORK_TOOLS` | unset | `1` lifts EVERY answer-key layer at once — the PreToolUse guard, the `/etc/hosts` block, and Codex's `web_search = "disabled"`. For a run that genuinely wants a browsing agent; a graded benchmark run must leave it unset (the answers are a fetch away) |
| `TE_CODEX_VENDOR` | resolved from the Node shim's install tree | **Codex lanes only.** The vendor directory bind-mounted read-only into the task container (the static-PIE ELF plus its siblings). Resolved and checked at runner construction, because Docker invents an *empty* directory for a missing bind-mount source rather than erroring — every task would fail `codex: not found` behind a green launch banner |
| `TE_SWEBENCH_MAX_CPUS` | `16` | CPU cap for ONE task container, plus the matching `OMP`/`MKL`/`OPENBLAS`/`NUMEXPR`/`LOKY` thread caps — which are the half that actually bounds a joblib pool, since `--cpus` does not change what `nproc` reports inside the container. `0` = unlimited. Recorded per row in `run_config.json → cpu_limit`. The launchers set it to `4` |
| `TE_FLEET_CPUS` | `16` | CPU budget for **every lane's containers together**, written by `scripts/launch/fleet_budget.sh` onto the cgroup all rootless-docker container scopes share. The variable above is per container, so *N* lanes are an *N*×cap ceiling; this is the one that bounds the host. It binds lanes that are already running, needs no restart, and is work-conserving inside the budget. `0`/`none` = do not enforce. An un-delegated or read-only cgroup warns and never fails a launch |
| `TE_OPTIMIZER_MAX_TOKENS` | `16384` | output cap for optimizer calls, the same on every runner (both agents, both domains). Too low truncates a judge's per-tool JSON into an unparseable answer, which reads downstream as "no verdict" while still billing — truncation is now logged, not silent. Applies on the API transport only: `copilot -p` has no output-cap flag, so on the CLI transport the value is read by nothing |
| `TE_OPTIMIZER_PREFLIGHT` | set by the driver | `0` skips the mechanism-endpoint probe at runner construction. `run_experiment.py` sets it from `config.needs_optimizer`, so arms that make no paid between-task call neither bill the probe nor depend on the endpoint. Unset means "assume it might", so a direct caller of `make_runner` keeps the check |
| `TE_OPTIMIZER_LOG` | `$TOKENECONOMIZER_RESULTS/logs/optimizer_calls.jsonl` | where between-task optimizer calls (prompt + response + usage) are recorded; not recorded at all if unset and no results root |

### Project-environment activation

The official SWE-bench images install the instance's dependencies into the `testbed` conda
env and activate it only from `/root/.bashrc`. No lane runs with `HOME=/root` and no CLI's
shell sources that file, so until 2026-09-11 a bare `python` / `pytest` in the agent's shell
hit the **base** interpreter (Python 3.11, no project deps) while `TASK_TEMPLATE` told the
agent the env was active. A bare `python` then failed to import the project's own
package, and each failure cost a multi-step detour before the agent found
`/opt/miniconda3/envs/testbed/bin/python` — or gave up running tests. Copilot's
`--bash-env=on` was inert because no `BASH_ENV` was set.

Every SWE-bench runner (Claude, Copilot, Codex) now activates the env for the agent
**process**: at container start it lists `/opt/miniconda3/envs/*/bin`, picks `testbed`
(or the only env), reads the image's `PATH`, and hands the agent `PATH=<env>/bin:<image
PATH>`, `CONDA_PREFIX` and `CONDA_DEFAULT_ENV` via `docker exec -e`. That reaches every
shell shape the CLIs spawn (`bash -lc`, `bash -c`, any `HOME` — measured), which a
`BASH_ENV` hook does not for Claude Code. `python` is resolved once under the new `PATH`
and the result is recorded in `run_config.json → env_activation`
(`mode`, `env_bin`, `python`, `verified`) and on the row as `env_activation`.

`TE_ENV_ACTIVATION=0` restores the old behaviour. **Rows taken with and without activation
are not comparable on cost** and must not be pooled; the field exists so an aggregator can tell them apart.

### The tool catalog

`reflect.parse_tool_catalog_claude` needs `{tool_name: description}` for the tools a run
loaded. The Copilot CLI dumps tool schemas into its debug log; Claude Code does not — its
transcript carries only the tool *names* — so the descriptions are captured off the wire:

```bash
python -m te_harness.capture_catalog     # -> tokeneconomizer/vendor/claude_tool_catalog.json
```

It runs the agent exactly as a task run does (same image, container, flags, and turn cap —
the CLI's offered tool set depends on the cap) with a stdlib recorder inside the container
recording the `tools` array off the request and forwarding to the real API. Captures merge
into any existing snapshot rather than replacing it, since the loaded set drifts slightly
between runs. Re-run it after a Claude Code upgrade.

### The grading file

`swebench_grading.json` maps each workload instance to `base_commit`,
`version`, `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS`. The repo's `swebench_tasks.json`
carries only `instance_id` / `problem_statement` / `repo`, which cannot be graded. Rebuild
it from the HF parquet exports (all but 30 are in SWE-bench_Verified; those 30 xarray
instances come from the full SWE-bench test split). A new run executes the 277 of
`workloads.runnable_instances()`, which drops the one instance `SWEBENCH_EXCLUDED` names
and adds the 49 `SWEBENCH_ADDED` uses to de-saturate the sklearn and sympy streams:

```bash
cd "$DATA/swebench"
curl -sL -o verified.parquet  "https://huggingface.co/api/datasets/princeton-nlp/SWE-bench_Verified/parquet/default/test/0.parquet"
curl -sL -o full_test.parquet "https://huggingface.co/api/datasets/princeton-nlp/SWE-bench/parquet/default/test/0.parquet"
python3 -c "
import pyarrow.parquet as pq, json
K=('repo','base_commit','environment_setup_commit','version','test_patch','patch','FAIL_TO_PASS','PASS_TO_PASS')
out={}
for f in ('full_test.parquet','verified.parquet'):
    for r in pq.read_table(f).to_pylist():
        out.setdefault(r['instance_id'], {k: r[k] for k in K})
ids=[t['instance_id'] for t in json.load(open('swebench_tasks.json'))]
json.dump({i: out[i] for i in ids if i in out}, open('swebench_grading.json','w'))"
```

## External tool scaffolds (the baseline arms)

The baselines themselves live in the CORE package, at `tokeneconomizer/baselines/`, and
are stdlib-only: they reach a container only through the callables this harness hands them
(`exec_`, `copy_in`, `read_bytes`, `host_run` on `ScaffoldContext`). What follows is what
*this* harness has to get right to drive them. Any other harness that can implement those
four callables can run the same baselines unchanged.

They install somebody else's off-the-shelf tool for the sealed agent so its cost and
resolve land on the same honest basis as a mechanism. "Into the container" is the
SWE-bench case, not the contract: Web-Bench runs the agent on the HOST, and the same
baseline installs into a per-task `$HOME` and a per-task `bin/` instead. What differs is
carried by `AgentProfile.home` / `AgentProfile.bin_dir`, so a scaffold never branches on
the harness -- and one that hardcodes `/usr/local/bin` silently no-ops off-container
(`chmod` fails, `&&` short-circuits, the row reads `rtk_installed: false`). They are **baselines, not
mechanisms**: nothing in `tokeneconomizer/` imports the package on its own — not
`__init__.py`, not anything under `mechanisms/` — so `import tokeneconomizer` stays
dependency-free and no mechanism can see a baseline.

| Scaffold | Arms | What it installs | Host requirement |
|---|---|---|---|
| `rtk` | `rtk`, `trimech-rtk`, `trimech-rtk-web`, `codex-trimech-rtk` | pinned rtk **0.45.0** musl binary, plus the integration that agent has: a PreToolUse hook on Claude (`settings.json`) and Copilot (`$COPILOT_HOME/hooks/`), instruction files on Codex | staged binary cache (`TE_SCAFFOLD_CACHE`, else `$DATA/scaffolds`) |
| `serena` | `serena` | `uv tool install serena-agent` in-container, an MCP server, and a nav-tool nudge | none (needs network *from* the container) |
| `repomap` | `repomap`, `trimech-repomap`, `codex-trimech-repomap` | aider's token-budgeted repo map, written as project instructions | `aider` on the host — `pip install -e '.[baselines]'` |

There is deliberately **no `trimech-serena`**: Serena's contract is that the agent navigates
through its MCP tools, which is exactly what evidence untooling prunes when a tool goes
unused, so the combined arm would measure that contention rather than either baseline. RTK
hooks `Bash` (always in `PROTECTED_TOOLS`, so untooling can never drop it) and the repo map
writes a file and touches no tool — neither contends, which is why those two combinations
are well-defined.

### Which baselines each domain can host

`rtk` is the only one Web-Bench can run (`te_harness/webbench.py` `WEBBENCH_SCAFFOLDS`);
`scripts/sweep_plan.py` `SWEEP_ARMS_WEBBENCH_UNAVAILABLE` is the planner-side twin, and
`tests/test_sweep_arms.py` asserts the two agree.

| Scaffold | SWE-bench | Web-Bench | Why not |
|---|---|---|---|
| `rtk` | yes | **yes** | — it needs no container and no language |
| `repomap` | yes | no | its generator walks `*.py`; Web-Bench projects are TS/JS |
| `serena` | yes | no | an MCP navigator for a codebase; Web-Bench ships a file set |

Two Web-Bench-specific differences, both handled by the runner rather than the baseline:

* **Artifacts are excluded from the ANSWER, not from a diff.** There is no `git diff` to
  subtract from — the evaluator applies whatever files the runner returns — so `_collect`
  refuses to ship the scaffold's own names, recording them in `run_config.json` →
  `scaffold.answer_excludes`.
* **`COPILOT_HOME` is isolated per task-run when a scaffold is active.** The Copilot
  Web-Bench runner deliberately keeps the real `$HOME` (a fresh one would orphan the
  ~170 MB package cache), but both hook writers land in the CLI's config dir — rtk's
  `hooks/rtk-rewrite.json` and the net guard's `settings.json` — which would be the
  operator's own `~/.copilot`. Only that variable is redirected, seeded with the real auth,
  so a sweep cannot leave a hook behind on the machine. The scaffold resolves the directory
  as `${COPILOT_HOME:-$HOME/.copilot}` in the shell, which is right on both lanes.

### Three things that make a scaffold arm silently fake

* **The graded diff.** A scaffold writes project instructions *into* `/testbed`, so
  `git diff` picks them up and the official grader would apply the scaffold's own prompt
  text as part of the agent's fix. Each scaffold declares `patch_excludes`; the runner
  subtracts them (`git diff -- . ':(exclude,top)CLAUDE.md' …`) and records what it
  subtracted in `run_config.json`. With no scaffold the diff command is left byte-identical
  to the pre-scaffold form, so stored baseline rows stay reproducible.
* **RTK's hook.** `rtk init -g` **declines** to patch the agent's settings
  non-interactively — it prints a "MANUAL STEP" notice and exits 0. Trusting it yields an
  arm where `rtk --version` works, every provenance field looks healthy, and the hook never
  fires. The scaffold registers the hook itself and `rtk_hook_registered` records whether
  that took; a `true` install with a `false` hook logs a warning naming the failure.
* **Serena is a nudge, not an exclusion.** The original study asked the agent to stop using
  the native navigation tools via an instruction and did not pass `--disallowed-tools`.
  That is kept: `Read` is in `PROTECTED_TOOLS`, so an exclusion would be filtered out and
  the arm would run native with an unused MCP server attached. If the agent keeps grepping,
  that **is** the measurement.

A scaffold that fails is recorded, not raised: the row carries `scaffold_error` (or
`rtk_installed: false`, `serena_installed: false`, `map_ok: false`) and the task runs
native. A lane of flagged rows is diagnosable; a lane of missing rows is not. **Filter those
rows out before quoting a scaffold arm's mean** — they are native rows wearing a scaffold's
name.

### Adding one

Subclass `Scaffold` (`tokeneconomizer/baselines/base.py`), implement `setup(ctx)` returning a
notes dict, declare `patch_excludes`, register it in `SCAFFOLDS`, and map its arm(s) in
`ARM_SCAFFOLDS`. Per-agent differences (instructions path, native tool names, `$HOME`, how
an MCP config is wired) come from `ctx.profile`, never from a hardcoded assumption — writing
`CLAUDE.md` on a Copilot run produces an arm that is native with a stray file in it. Add
the arm to `ARMS` in `scripts/run_experiment.py` with `scaffold="<name>"`;
`tests/test_sweep_arms.py` asserts the two maps agree.

## The answer-key guard

A SWE-bench instance's fix commit is public on GitHub, so an agent that can fetch it is not
solving the task — it is reading the answer key, and the resolve verdict it produces is
worthless. So this applies to **every arm, `baseline` included**: it is a property of the
measurement, not of an arm. Leaving it to untooling would be worse than useless, since
untooling drops a tool only after observing disuse — precisely the agents that browse would
keep the ability.

**One policy.** A single case-insensitive substring, `github`. Re-derived 2026-09-09 over
3,538 stored run dirs and 558 network-shell calls: it matches 352, every one of them reading
a repository's source, history, PR diffs or `.patch` files, with **0 false positives** (no
`pip`/`npm`/`npx`/`yarn` install blocked; pip alone accounts for 180 of the legitimate
calls) and **0 misses** (nothing naming a `.patch`, a `/commit/`, a `/pull/`, a mirror, or a
benchmark repo escaped it). That is the argument for keeping it this narrow rather than
cutting the container's network: the wider rule also deletes the tool surface the untooling
mechanism is measured on, and charges the baseline for a restriction the cheat route never
needed.

**Two enforcement points**, because a hook is only as good as the CLI's cooperation:

| layer | how | Claude | Copilot | Codex |
|---|---|---|---|---|
| `PreToolUse` hook | exit 2 + reason on stderr | ✅ | ✅ | ✅ since 2026-09-30 — `$CODEX_HOME/hooks.json` + `--dangerously-bypass-hook-trust` |
| `/etc/hosts` block | answer-key hosts → `127.0.0.1` in the task container | ✅ | ✅ | ✅ |

The hook is the layer that tells the **model why**, which a DNS failure cannot. The hosts
block needs no cooperation from any CLI, so it is identical on all three lanes, and it was
the only layer Codex had until 2026-09-30. Verified in a real SWE-bench container: afterwards `curl`, `wget`,
`git clone` and `python -c "import urllib.request"` all fail with `Connection refused` for
the answer-key hosts, while `pypi.org`, `files.pythonhosted.org` and a real `pip download`
keep working. `127.0.0.1` rather than `0.0.0.0` so the connection is refused at once instead
of hanging to a timeout. The stanza is appended idempotently behind a marker comment and
then **verified** (`getent hosts github.com` must answer `127.0.0.1`) — a write can succeed
into a container whose resolver ignores `/etc/hosts`, and that failure is otherwise
invisible.

`TE_ALLOW_NETWORK_TOOLS=1` lifts every layer, or none.

**Neither layer is bullet-proof**, and the row telemetry is what makes that manageable:

| field | means |
|---|---|
| `net_guard_installed` | the hook is armed *and* proved it refuses a probe |
| `net_guard_blocks` | hook refusals this task |
| `answer_key_hosts_blocked` | the DNS stanza is installed *and* `github.com` resolves to loopback |
| `answer_key_attempts` | answer-key-shaped commands found in the transcript afterwards — **agent-independent**, so it is the number to watch on any row whose `net_guard_installed` is false (every Codex row before 2026-09-30); `None` when there is no transcript, never `0` |

An IP literal, a redirect, a mirror or a base64'd URL walks past both layers. A model
routing around them shows up as `answer_key_attempts` climbing while `net_guard_blocks`
stays flat — which is why attempts are counted from the trace and not inferred from
refusals.

## Not covered yet

* **Two tool descriptions are unobtainable.** The catalog is captured (below), but `Task`
  and `ToolSearch` stay name-only: they exist *only* in first-party mode, and pointing the
  CLI at any base URL — including the capture shim — removes them from the offered set.
  Their schemas reach the model's context but never the transcript (a `ToolSearch`
  tool_result is empty by design), so there is nowhere to read them from short of
  intercepting first-party TLS. The judge tolerates a name with no description.
