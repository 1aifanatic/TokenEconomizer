# TokenEconomizer — reproducing the paper's experiments

This directory holds the code behind the paper: the Docker benchmark harness, the sweep and
aggregation scripts, the evaluated task sets, and the run book. It is **not part of the
plugin** and is not packaged with it. The plugin, and the shared core both use, live at the
repository root; see [`../README.md`](../README.md).

Everything here keeps the layout the experiments were run with: `te_harness/`, `scripts/`,
`workloads/` and `docs/` sit side by side. The shared core, `tokeneconomizer`, stays at the
repository root, one level up, and the scripts put both directories on `sys.path`
themselves (an editable install, `pip install -e ..`, works too). Run commands from this
directory (`cd experiments`), or by path from anywhere; the scripts locate their own root. The `.env` the scripts read is
`experiments/.env`, else the repository root's.

### Proactive runtime cost control for sealed coding agents

**Externally-delivered, agent-agnostic middleware that aims to cut a sealed coding agent's
amortized token cost without hurting task success.**

---

## The problem

A sealed coding agent — Claude Code, the Copilot CLI, Codex — is optimized for **task
success, not the token bill**. You cannot retrain it, patch it, or see inside it. What you
get is a fixed interface: a launch command, an instruction channel, a tool contract, and a
stream of events. Meanwhile its transcript grows monotonically and is replayed into every
model call.

That shape produces three costs that recur on *every task in a stream*, not once:

| | Cost region | What it looks like |
|---|---|---|
| 1 | **Repeated environment discovery** | Re-probing the test command, the build, the repo layout — things the agent already worked out on the previous task, and the task before that. |
| 2 | **The fixed tool contract** | Every tool schema re-sent on every turn, including the ones this repository will never need. |
| 3 | **The unproductive tail** | Tokens burned after a rollout has stopped making progress but before it gives up. |

An experienced engineer working in a familiar repository avoids all three. They already know
the test command, they don't reach for tools the project doesn't use, and they abandon a dead
approach early.

## The question this repository asks

**Can an external controller supply that familiarity while leaving the agent completely
fixed** — no weight updates, no fine-tuning, no changes to agent code?

A static plugin is not enough, and that is the substantive claim. How much there is to save,
and where, depends on the repository, the harness, the agent's tool surface, and the
trajectory currently unfolding. So the controller has to be **stateful and observational**:
it reads completed and live traces, maintains per-repository state across a task stream, and
acts only through interfaces the harness already exposes — the launch call, the instruction
channel, and the event stream.

Three independently-switchable mechanisms each target one cost region, at a different point
in a task's lifecycle:

```
task lifecycle:   ── inject ──▶ construct tools ──▶ ═══ rollout ═══▶ finalize ──
                       │              │                    │
                   injection       tooling              spiral
              learned per-repo   drop inapplicable   kill/nudge a stuck
              operating manual   tool schemas /      rollout vs a learned
              before the task    capabilities        resolved-turn envelope
              (between tasks)    (before each task)  (during rollout)
```

Because they are independently switchable, each can be measured alone as an ablation of the
combined stack rather than as a separate system.

| Mechanism | Module | What it does |
|---|---|---|
| **injection** | `mechanisms/injection.py` | A paid proposer learns a firing-gated per-repo operating manual (the canonical test command, "a repro script is redundant here", a required env var) over the task stream, and injects it before each task. A formation / decay / firing-gate lifecycle keeps only facts that actually fire. |
| **tooling** | `mechanisms/tooling.py`, `mechanisms/codex_untool.py` | Drops inapplicable tool schemas: evidence untooling (loaded-but-unused for *N* consecutive tasks) plus an optional instruction-aware judge. Never drops a protected tool — without an edit path and a shell there is no task. |
| **spiral** | `mechanisms/spiral.py`, `mechanisms/codex_spiral.py` | Kills or nudges a stuck rollout against a learned per-repo resolved-turn envelope: cheap gate → sampled LLM judge → nudge, then abort. |

## What is actually measured

The objective is to **minimize cache-aware effective cost while holding resolve rate** (or
F1, on the classification domains).

Two properties make that honest rather than rhetorical:

- **Cache-aware cost.** Most raw transcript tokens are cached re-reads. Counting them at full
  weight would let a method look good merely by pruning cheap repetition, so cost is
  measured in fresh-input-token equivalents.
- **The controller pays for itself.** Every token the controller spends — proposer, tool
  judge, spiral judge, nudge writer — is charged back to TokenEconomizer in the same ledger
  as the task. Reported savings are **net of the mechanisms' own overhead**, so a mechanism
  that buys savings with paid calls has to win on the balance.

One consequence worth stating up front: a mechanism's overhead line is *not* a saving
available by deleting it, because the mechanisms shape the trajectory that produces every
other number in the row. Use the ledger to price a mechanism, not to predict its removal.

Results are not part of this repository; the code here produces them.

---

## How the repository is organised

The layout follows one rule: **the controller must not be able to reach the things it is
supposed to be independent of.** That is enforced structurally, by what sits underneath
what, rather than by convention.

### Three layers

**Layer 1 — `tokeneconomizer/`, the controller.** The mechanisms, the cost definition, and
the trace reader. It is **stdlib-only with zero runtime dependencies**: `import
tokeneconomizer` cannot touch Docker, an agent CLI, or an LLM SDK, because none of that code
lives beneath it. This is what makes the agent-agnostic claim checkable instead of
aspirational.

**Layer 2 — `te_harness/`, the reference harness.** The heavy machinery: launching a sealed
agent inside a benchmark's Docker image, applying the controller's per-task decisions,
streaming events back, and grading with the benchmark's *official* grader. It is a
**sibling** package of `tokeneconomizer`, not a submodule, and is excluded from the
distributed wheel — which is precisely what keeps Layer 1's guarantee structural. Swapping in
your own harness is a supported path, not a fallback: nothing above the seam changes, because
the controller never learns which harness it is talking to.

`te_live/` (at the repository root) is the second harness, and the proof of that claim: the
**live harness** runs the same mechanism classes inside a user's own Claude Code, Codex, Copilot
CLI or Pi sessions, through the hooks the agent exposes, and `plugin/` packages it for
installation. It is a sibling of the core for the same reason `te_harness` is — it reaches an
agent CLI — but it ships in the wheel, because it is what `te` runs. See
[`../docs/PLUGIN.md`](../docs/PLUGIN.md).

**Layer 3 — `scripts/`, the reproduction pipeline.** Planning a sweep, driving one arm,
aggregating row trees into CSVs, and comparing arms. Nothing here is imported by Layers 1
or 2.

### The four seams

Every crossing between layers goes through exactly one interface, which is why a new agent,
harness, or benchmark is an addition rather than an edit:

| Seam | Module | Separates |
|---|---|---|
| `AgentRunner` | `runner.py` | the controller from task execution and paid LLM calls |
| `DomainAdapter` | `adapters/` | the controller from what a "task" is |
| trace | `trace.py` | every mechanism from the shape of a rollout's event stream |
| ledger | `ledger.py` | every mechanism from the definition of cost |

The mechanisms read rollouts through the one trace seam and charge cost through the one
ledger, so a change to either is a change in one place, and no mechanism can quietly adopt
its own accounting.

### The tree

```
experiments/              the paper's reproduction code (this directory)
  te_harness/               Layer 2 — reference AgentRunner: Docker + agent CLIs + official graders
    swebench.py               agent in the instance's pinned image, graded by get_eval_report
    webbench.py               agent at the Node evaluator's HTTP seam, graded by its own suites
    copilot.py                Copilot CLI overrides (GitHub auth, own catalog, own accounting)
    codex.py                  Codex CLI seam — the OpenAI API, the capability→config
                              mapping, own accounting
    guards/                   measurement-integrity guards — refusing fetches of a benchmark's
                              own answer key, enforced twice: a PreToolUse hook in each CLI's
                              own hook file, and an /etc/hosts block in the task container
    capture_catalog.py        mines the real tool catalog off a live run's wire
  scripts/                  Layer 3 — reproduction pipeline (planning, driving, aggregating, comparing)
  workloads/                the evaluated task sets as committed JSON (ids only, ~28 KB)
  docs/                     setup, provisioning, the harness, the run book (see below)
  tests/                    offline suite for the harness and pipeline — no Docker, no datasets

../tokeneconomizer/       Layer 1 — the controller (stdlib-only, zero deps), shared with the plugin
  facade.py                 Economizer, the orchestration entry point
  config.py                 three independent on/off switches, one per mechanism
  arms.py  _armspec.py      named arm → exact configuration (the experiment's vocabulary)
  ledger.py  trace.py       the one cost definition + the one trace seam
  runner.py                 AgentRunner interface — the harness seam
  workloads.py              the evaluated task set, as data (which instances, per domain)
  mechanisms/               injection · tooling · spiral (+ the Codex-native variants)
  baselines/                third-party tool scaffolds (rtk · serena · aider repo-map) —
                            comparison points, not mechanisms
  adapters/                 DomainAdapter + swebench · webbench · creddata · reposvul ·
                            cybergym · webarena
  vendor/                   pricing · reflect · spiral, vendored verbatim, stdlib-only
../te_live/  ../plugin/   the plugin (see ../README.md)

── and deliberately NOT in the repository ───────────────────────────────────────
te_data/                  the data root, beside the repository: benchmark checkouts, task
                          specs, grading tables, Docker-image manifests, every results tree.
                          Reached only through TOKENECONOMIZER_* / DATA env vars.
```

### The data root lives *outside* the repository

Nothing large that the project reads or writes belongs in the checkout. Benchmark
checkouts, task specifications, grading tables, and every results tree live under **one data
root of your choosing, outside the repo** — conventionally `te_data/` beside it — and the
code reaches it only through `TOKENECONOMIZER_*` / `DATA` environment variables.

The reason is scale, and then three practical ones. A working root for this project runs to
tens of gigabytes (the Web-Bench monorepo alone is ~24 GB, results trees a few GB more),
on top of ~165 GB of pinned per-instance Docker images that live in the daemon's own store.
None of that is versionable, and none of it should be one `git add -A` away from a commit.
Beyond size:

- **A clone becomes disposable.** Delete or re-clone the repo and your results are untouched
  — which is what makes it safe to test a change in a throwaway clone against the corpus you
  already have.
- **Several checkouts can share one root.** Two clones, or a git worktree, can run against
  the same provisioned data and the same results tree without re-provisioning any of it.
- **No machine-specific path is baked into the code.** These variables have no built-in
  defaults on purpose: the harness raises at construction when one is missing rather than
  silently resolving to a path that only exists on whichever machine wrote the code.

`.gitignore` is the belt-and-braces half of the same decision — `results/`, `*.csv`,
`*.jsonl`, rendered images and `.env` are all excluded — but the primary defence is that
the data was never inside the tree to begin with. [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md)
and [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) cover the layout and what to put where.

### Provenance, kept as data

The identities of the evaluated tasks live in the core package (and are mirrored as committed
JSON under `workloads/`), not in a results tree. So the exact set a published number was
computed over can be re-provisioned and re-run without inheriting anyone's output directory.
The published corpus ran a fixed subset per domain rather than a whole benchmark; two
SWE-bench streams have since been extended because every arm had saturated them, so a *new*
run's task set is deliberately not identical to the recorded one — [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md)
spells out both.

---

## Where things are documented

| Document | What it covers |
|---|---|
| [`../docs/METHOD.md`](../docs/METHOD.md) | why the mechanisms are shaped this way |
| [`docs/SETUP.md`](docs/SETUP.md) | fresh clone → running, gated step by step |
| [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md) | machine requirements and Docker setup |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | per-benchmark provisioning and on-disk layout |
| [`docs/HARNESS.md`](docs/HARNESS.md) | the `AgentRunner` seam, and writing your own harness |
| [`docs/COPILOT.md`](docs/COPILOT.md) | running the Copilot CLI as the sealed agent |
| [`docs/RUNNING.md`](docs/RUNNING.md) | the run book: authentication per agent, the arm catalog, one command per lane |

**The plugin** — the mechanisms in your own sessions — is documented at the repository root:
[`../README.md`](../README.md) and [`../plugin/README.md`](../plugin/README.md).

**Start here:** [`../docs/METHOD.md`](../docs/METHOD.md) for the ideas,
[`docs/SETUP.md`](docs/SETUP.md) to get it running, and
[`docs/RUNNING.md`](docs/RUNNING.md) to reproduce the experiments.

Requires Python ≥ 3.10. The core has no runtime dependencies; `requirements.txt` here covers
the test suite (`python -m pytest tests`, from this directory, or
`python -m pytest` at the repository root for both suites).
