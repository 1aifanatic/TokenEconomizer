# Running the experiments

The sweep runs three agents — Claude Code, the GitHub Copilot CLI and Codex — each on its own
native authentication, against SWE-bench and Web-Bench, once per arm. This page is the run
book: how each agent is billed, how to check the wiring before paying for anything, the arm
catalog with one command per lane, and what to do with the rows afterwards.

Before this page: [`SETUP.md`](SETUP.md) (venv, data root, Docker, `.env`) and
[`BENCHMARKS.md`](BENCHMARKS.md) (task images and datasets). Commands below run from
`experiments/`.

## 1. Authentication — each agent's own

| Agent | Billed through | What to set (in `.env` or the shell) |
|---|---|---|
| Claude Code | the Anthropic API | `ANTHROPIC_API_KEY` |
| Codex | the OpenAI API | `OPENAI_API_KEY`; `OPENAI_BASE_URL` only for a key issued against a regional OpenAI endpoint (`https://<region>.api.openai.com/v1`) |
| Copilot CLI | your GitHub Copilot subscription | `/login` in `copilot` on the host, or `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` |
| Copilot CLI | Copilot's own BYOK | `COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_TYPE`, `COPILOT_PROVIDER_API_KEY` or `_BEARER_TOKEN`, and a pinned model (see [`COPILOT.md`](COPILOT.md)) |

Each agent bills its own vendor directly. The launchers ignore `ANTHROPIC_BASE_URL` and
`ANTHROPIC_AUTH_TOKEN` (they say so once and unset them), and refuse an `OPENAI_BASE_URL`
that is not an OpenAI host. Copilot-specific steps — signing in, keeping a token warm through
a long sweep, and checking a finished row — are in [`COPILOT.md`](COPILOT.md).

The model is part of the cost basis, so every lane pins one: the results tree is named
`<agent>_<domain>_<model-tag>` (for example `claude_swebench_opus48`), and two models never
share a tree.

## 2. Check the wiring for free

```bash
bash scripts/launch/smoke_test.sh          # offline: StubRunner, temp dirs, no Docker, no model
AGENT=claude MODEL=claude-sonnet-5 DOMAIN=swebench DRYRUN=1 \
  bash scripts/launch/main_sweep.sh        # the plan and its size, nothing run
```

`DRYRUN=1` works on a fresh clone with nothing configured; the billed probes skip themselves.

## 3. One paid cell first

One task, one arm, in the foreground, before any lane:

```bash
AGENT=claude MODEL=claude-sonnet-5 DOMAIN=swebench ARMS=baseline \
  REPOS=sympy/sympy SEEDS=0 LIMIT=1 DETACH=0 \
  RUNNER=te_harness.swebench:make_runner \
  bash scripts/launch/main_sweep.sh
```

Then read the row (`$TOKENECONOMIZER_RESULTS/<tree>/baseline/sympy_s0/*/row.json`):
`num_turns` must be above zero and the token fields populated. A mechanism-bearing arm needs
more than one task to fire anything — injection proposes on a 5-task cadence, untooling
needs 3 consecutive unused tasks, and the spiral guard waits for its minimum turn count — so
pilot `trimech` with `LIMIT=6` before trusting it.

## 4. Claude Code and Copilot — `main_sweep.sh`

One invocation fills one tree: one agent, one model, one domain, every arm × group × seed.
The main sweep is both agents × `claude-opus-4-8` and `claude-sonnet-5` × both domains, eight
lanes; `scripts/launch/full_sweep.sh` launches all eight. Each lane is detached by default
and resumes where it stopped (`SKIP_DONE=1`).

### swebench — 12 arms

| Arm | What runs |
|---|---|
| `baseline` | no mechanisms, harness-default effort (`high`) — the cost basis every arm pairs against |
| `baseline-medium` | `baseline` at `TE_EFFORT_LEVEL=medium` (the arm sets it) |
| `baseline-low` | `baseline` at `TE_EFFORT_LEVEL=low` |
| `inject-only` | micro-skill synthesis and injection only |
| `untool-only` | evidence untooling and the tool judge only |
| `spiral-only` | spiral guard, judge and nudge only |
| `trimech` | all three mechanisms together |
| `rtk` | `baseline` + rtk 0.45.0 |
| `serena` | `baseline` + Serena |
| `repomap` | `baseline` + aider's repo map |
| `trimech-rtk` | `trimech` + rtk |
| `trimech-repomap` | `trimech` + the repo map |

```bash
for AGENT in claude copilot; do for MODEL in claude-opus-4-8 claude-sonnet-5; do
  AGENT=$AGENT MODEL=$MODEL DOMAIN=swebench \
    RUNNER=te_harness.swebench:make_runner \
    bash scripts/launch/main_sweep.sh
done; done
```

### webbench — 5 arms

| Arm | What runs |
|---|---|
| `baseline` | no mechanisms, harness-default effort |
| `baseline-medium` | `baseline` at `TE_EFFORT_LEVEL=medium` |
| `baseline-low` | `baseline` at `TE_EFFORT_LEVEL=low` |
| `trimech-web` | `trimech` with the spiral stage tuned for short streams |
| `rtk` | `baseline` + rtk 0.45.0 |

```bash
for AGENT in claude copilot; do for MODEL in claude-opus-4-8 claude-sonnet-5; do
  AGENT=$AGENT MODEL=$MODEL DOMAIN=webbench \
    RUNNER=te_harness.webbench:make_runner \
    bash scripts/launch/main_sweep.sh
done; done
```

Web-Bench reaches the agent through the evaluator's HTTP seam with no per-task repository
container, so `serena`, `repomap` and `trimech-repomap` cannot run there, and the planner
refuses them. The single-mechanism arms are answered on SWE-bench and are not planned for
Web-Bench; they still run if named in `ARMS`.

### The frontier lane

`claude-opus-5` runs four arms per domain on both agents, in trees of its own (never pooled
with the main sweep's):

```bash
for AGENT in claude copilot; do
  AGENT=$AGENT MODEL=claude-opus-5 DOMAIN=swebench \
    ARMS="baseline baseline-medium baseline-low trimech" \
    RUNNER=te_harness.swebench:make_runner bash scripts/launch/main_sweep.sh
  AGENT=$AGENT MODEL=claude-opus-5 DOMAIN=webbench \
    ARMS="baseline baseline-medium baseline-low trimech-web" \
    RUNNER=te_harness.webbench:make_runner bash scripts/launch/main_sweep.sh
done
```

`full_sweep.sh` does not launch these two.

## 5. Codex — `codex_ab.sh`

Codex runs one arm per invocation, through its own launcher, on `gpt-5.6-terra` and
`gpt-5.6-sol`. `BASELINE=0` runs only the named arm, so the loop below fills one tree per
model and domain with every arm. The Codex mechanisms are native to it: untooling works on
capability packages rather than single tool schemas, and the spiral guard nudges in the same
thread rather than stopping a turn.

### swebench — 12 arms

| Arm | What runs |
|---|---|
| `baseline` | no mechanisms, harness-default effort (`high`) |
| `baseline-medium` | `baseline` at `TE_EFFORT_LEVEL=medium` |
| `baseline-low` | `baseline` at `TE_EFFORT_LEVEL=low` |
| `codex-inject` | environment facts plus the distiller, injected at session start (the proposer bills) |
| `codex-untool` | evidence-gated capability untooling |
| `codex-spiral` | the defensive spiral profile: same-thread nudges, no abort, no effort change |
| `codex-trimech` | all three Codex mechanisms |
| `rtk` | `baseline` + rtk 0.45.0 in its Codex instruction mode |
| `serena` | `baseline` + Serena |
| `repomap` | `baseline` + aider's repo map |
| `codex-trimech-rtk` | `codex-trimech` + rtk |
| `codex-trimech-repomap` | `codex-trimech` + the repo map |

```bash
REPOS="django/django matplotlib/matplotlib scikit-learn/scikit-learn sphinx-doc/sphinx sympy/sympy pydata/xarray"
for MODEL in gpt-5.6-terra gpt-5.6-sol; do
  for ARM in baseline baseline-medium baseline-low codex-inject codex-untool codex-spiral \
             codex-trimech rtk serena repomap codex-trimech-rtk codex-trimech-repomap; do
    MODEL=$MODEL ARM=$ARM BASELINE=0 DOMAIN=swebench REPOS="$REPOS" SEEDS="0 1 2 3 4" \
      RUNNER=te_harness.swebench:make_runner DETACH=0 \
      bash scripts/launch/codex_ab.sh
  done
done
```

### webbench — 5 arms

| Arm | What runs |
|---|---|
| `baseline` | no mechanisms, harness-default effort |
| `baseline-medium` | `baseline` at `TE_EFFORT_LEVEL=medium` |
| `baseline-low` | `baseline` at `TE_EFFORT_LEVEL=low` |
| `codex-trimech` | all three Codex mechanisms |
| `rtk` | `baseline` + rtk 0.45.0 in its Codex instruction mode |

```bash
PROJECTS="calculator expressjs react zustand table"
for MODEL in gpt-5.6-terra gpt-5.6-sol; do
  for ARM in baseline baseline-medium baseline-low codex-trimech rtk; do
    MODEL=$MODEL ARM=$ARM BASELINE=0 DOMAIN=webbench REPOS="$PROJECTS" SEEDS="0 1 2 3 4" \
      RUNNER=te_harness.webbench:make_runner DETACH=0 \
      bash scripts/launch/codex_ab.sh
  done
done
```

On Web-Bench the Codex agent runs on the host; the runner blocks the answer-key hosts in an
unprivileged user and mount namespace and refuses to start if it cannot.

## 6. Narrowing a lane

Every launcher takes the same knobs: `ARMS` (or `ARM`), `REPOS`, `SEEDS`, `LIMIT` (tasks per
cell), `DRYRUN=1`, `DETACH=0`, `ON_FAIL=abort`. The variables and their defaults are listed at
the top of each script and in [`../scripts/launch/README.md`](../scripts/launch/README.md).

## 7. Aggregating and comparing

```bash
python scripts/aggregate.py $TOKENECONOMIZER_RESULTS/<tree>          # -> <tree>/index/*.csv
python scripts/compare_arms.py \
  --baseline $TOKENECONOMIZER_RESULTS/<tree>/baseline/<cell> \
  --arm      $TOKENECONOMIZER_RESULTS/<tree>/<arm>/<cell>
```

`aggregate.py` rewrites the whole index on every call and refuses an `--arms` subset that
would drop arms from it. `compare_arms.py` pairs tasks by instance id and prices them on the
cache-aware effective-token basis, with each arm's own mechanism calls charged to it. Never
average two trees into one cell: the model and the agent are both part of the cost basis.
