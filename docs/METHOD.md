# Method — thesis, amortized setting, and the seams

## Thesis

Hold the **sealed agent fixed** (Claude Code, Copilot CLI, Codex) and make the
**environment learn a cheaper interface to itself**, so the agent solves tasks at lower
token cost with **no regression in task success**. The optimization is delivered
**externally** (no agent changes) and must **transfer across agents**.

The cost we optimize is amortized over a **stream** of tasks on one repository, not any
single task. Some mechanism cost is paid once (a between-task proposer tick) and reused by
every later task, so the metric is the **amortized accuracy–cost curve** over the stream,
not per-task overhead in isolation.

## Design constraints (learned empirically)

- **Optimize turns, not bytes.** Cost is driven by round-trips × re-read context, not
  per-command output size — so compressing command output alone is not enough.
- **Displacement is hard.** Sealed agents under-adopt nudged tools; prefer environment-side
  levers the agent already uses over ones it must opt into.
- **Cache-aware cost or nothing.** Most raw tokens in an agent session are cached re-reads,
  so comparing on `tokens_total` flatters methods that prune cheap re-reads. Everything is
  measured on `pricing.effective_tokens + ledger.overhead`.
- **Resolve is a hard guardrail.** A cheaper arm that loses task success is not shipped. The
  spiral finalize/abort rules are gated to be resolve-safe by construction.

## The three mechanisms

Each acts at a different point in a task's lifecycle and is independently switchable:

| Mechanism | Lifecycle point | What it does |
|---|---|---|
| **injection** | before the task | Inject a firing-gated, learned per-repo operating manual (canonical test command, "a repro is redundant", required env var, …). Two layers share one lifecycle: an LLM-free environment-fact layer (`envfacts`: recurring failure→repair shapes, refused hosts, empty probes; vocabulary from the domain adapter) and a paid proposer that authors open-vocabulary facts + extractors over a compact digest. Profiles `distiller` (Claude/Copilot default), `envfacts` (Codex default, no paid call) and `envfacts+distiller` (both; the proposer is told what is already injected). A validated lifecycle (formation / decay / firing gate) keeps only facts that actually fire. |
| **tooling** | at task construction | Drop inapplicable tool schemas. Generic: evidence untooling (drop a tool loaded-but-unused for N tasks) + an optional per-task instruction-aware judge. Codex-native: remove whole inapplicable capability packages from the request contract, evidence-gated and resolve-safe. |
| **spiral** | in flight / at finalize | Kill or nudge a stuck rollout against a learned per-repo resolved-turn envelope. Generic: cheap gate → sampled LLM judge → nudge-then-abort. Codex-native: one same-thread finalize nudge once a trustworthy green passes and every failed scope is cleared. |

## The two seams that keep the core self-contained

The mechanisms are harness- and domain-agnostic. Everything environment-specific enters
through two small, named surfaces:

### 1. Trajectory + cost — vendored, stdlib-only

- `tokeneconomizer.trace` — the ONE place a saved rollout transcript is read
  (`load_trajectory` / `tool_usage` / `detect` / `parse_catalog`); auto-detects the
  supported agent transcript schemas.
- `tokeneconomizer.ledger` — the ONE definition of amortized cost, over the vendored
  `pricing` table. Mechanisms write per-task overhead fields; aggregation reads them back
  through the same module, so emitted and reported ledgers cannot drift.
- `tokeneconomizer.vendor` — `pricing` / `reflect` / `spiral`, copied in verbatim
  (stdlib-only), so `import tokeneconomizer` needs no external benchmark tree.

### 2. Execution + domain — behind interfaces

- `tokeneconomizer.runner.AgentRunner` — the seam to the task-execution harness. The
  economizer never launches Docker, calls an LLM API, or shells out to an agent CLI itself;
  it needs only (a) a paid **optimizer call** for the proposer/judge/nudge, and (b) an
  optional **task executor**. A concrete Docker + CLI + gold-grading runner is an external
  plugin (see [`experiments/`](../experiments/README.md)), with image/dataset paths as
  placeholders.
- `tokeneconomizer.adapters.DomainAdapter` — the seam to the task domain. The mechanisms
  read the domain only through this (trajectory reading, waste signals, untool tool-sets,
  spiral snapshot, per-task issue text). A new benchmark = a new adapter, no mechanism
  edits. Ships with SWE-bench (canonical), Web-Bench, CredData, ReposVul, CyberGym, and a
  WebArena stub; dataset roots are env-var placeholders.

## Entry point

```python
from tokeneconomizer import Economizer, EconomizerConfig

econ = Economizer.from_args(args, repo=repo, results_root=root, fresh=True,
                            runner=my_runner)         # my_runner: AgentRunner or optimizer callable
# per task:
injected      = econ.injection_prefix(task)
exclude_tools = econ.build_tools(task, i)
run_kwargs    = econ.run_kwargs(task)
# ... execute the task via the runner with those decisions ...
opt_tokens, reason = econ.after_task(task, row, run_dir_path, srow)
```
