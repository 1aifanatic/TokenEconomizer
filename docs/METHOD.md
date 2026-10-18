# Method — thesis, amortized setting, and the seams

## Thesis

Hold the **agent fixed** (Claude Code, Codex, Copilot CLI, Pi) and make the **environment
learn a cheaper interface to itself**, without losing task success. The optimization is
delivered **externally** -- no change to the agent -- and has to **transfer across agents**.

The cost is amortized over a **stream** of tasks (sessions) on one repository, not any single
task. Some mechanism cost is paid once (a between-session proposer call) and reused by every
later session, so the quantity of interest is the **amortized accuracy–cost curve** over the
stream, not per-task overhead in isolation.

## Design constraints

- **Optimize turns, not bytes.** Cost is driven by round-trips × re-read context, not
  per-command output size, so compressing command output alone is not enough.
- **Prefer levers the agent already uses.** Agents under-adopt tools they are nudged toward;
  environment-side levers they already read (the context at session start, the tool list) do
  not depend on adoption.
- **Cache-aware cost or nothing.** Most raw tokens in an agent session are cached re-reads, so
  comparing on raw token totals flatters methods that prune cheap re-reads. Everything is
  measured on `pricing.effective_tokens` plus `ledger.overhead`.
- **Task success is a guardrail.** A mechanism that is cheaper but loses task success is not
  shipped. The loop guard's stop and the Codex finalize rule are gated to spare a trajectory
  that is still making progress.

## The three mechanisms

Each acts at a different point of a session and is switched on or off on its own:

| Mechanism | Package | When | What it does |
|---|---|---|---|
| **Micro-skill synthesis and injection** | `tokeneconomizer.microskills` | before a session | State short, learned repository facts up front. An LLM-free layer (`envfacts`) forms facts from recurring failure→repair shapes of a command (naming the repair with the best record as typed, one piece of evidence per session) and of its test-label arguments, an environment variable the code asks for and the value that worked, refused hosts and empty probes; its vocabulary comes from the domain adapter. An optional paid proposer (`distiller`) authors open-vocabulary facts and extractors over a compact digest, told which facts the free layer already states. Every fact goes through one lifecycle (`lifecycle`): formation on repeated sessions (for a proposed fact about the code, sessions that edited different files), retirement on contradiction, and a firing gate so only facts whose failure keeps occurring stay injected. |
| **Tool pruning** | `tokeneconomizer.toolprune` | at session construction | Drop tool definitions a repository's work does not use. Evidence pruning drops a tool loaded but unused for N sessions, never a protected work-surface tool or one ever used, with an optional per-task instruction-aware judge. On Codex the unit is a capability package removed from the request contract, evidence-gated. |
| **Failure-loop intervention** | `tokeneconomizer.loopguard` | during a turn | Notice a trajectory stuck repeating the same failure, against a learned per-repository envelope of how long turns that end well run: a free gate, then either a sampled LLM judge or a strict heuristic, then a redirect, then a stop. On Codex, a same-thread finalize redirect once a trustworthy green check passes and every failed scope is cleared. |

## The seams that keep the mechanisms self-contained

The mechanisms know neither the harness nor the domain. Everything specific enters through a
few small, named surfaces, all in the standard-library-only package:

- `tokeneconomizer.trace` -- the ONE place a saved session log is read (`load_trajectory`,
  `call_cwds`, `tool_usage`, `parse_catalog`), with one module per agent's format.
- `tokeneconomizer.ledger` and `tokeneconomizer.pricing` -- the ONE definition of amortized
  cost. Mechanisms record per-task overhead fields; the plugin's session ledger and the
  experiments' aggregation read them back through the same module, so the recorded and the
  reported cost cannot drift.
- `tokeneconomizer.domain.DomainAdapter` -- the seam to the task domain: reading a session,
  the tool sets pruning protects or excludes, a trajectory's loop signals, task text for the
  optional judge, and the micro-skill vocabulary. A new domain is a new adapter, with no
  mechanism edit. The plugin's adapter is `tokeneconomizer.plugin.adapter.LiveAdapter`; the
  benchmark adapters (SWE-bench, Web-Bench, CredData, ReposVul, CyberGym, a WebArena stub) are
  in `experiments/te_bench/adapters/`.
- `tokeneconomizer.stage.Stage` -- the lifecycle hooks a mechanism offers a caller
  (`before_task`, `build_tools`, `run_kwargs`, `after_task`).

Who runs the stages is outside the package:

- **The plugin** (`tokeneconomizer.plugin`) runs them inside a person's sessions through each
  agent's hooks: `PrefixStage` and `ToolingStage` over each finished session, and the loop guard
  one tool call at a time (`loopguard.TurnGuard`). The mechanisms never import it.
- **The experiments** (`experiments/te_bench`) run them over benchmark streams: the
  `Economizer` facade wires the three stages around one adapter, and `te_bench.runner.AgentRunner`
  is the seam to a task-execution harness (Docker, an agent CLI, a grader), with
  `experiments/te_harness` as the reference implementation. The benchmark-only controllers --
  `SpiralStage`, the streamed `NudgeGuard`, the Codex finalize policy -- live there too.

## Entry point (the experiments)

```python
from te_bench.facade import Economizer

econ = Economizer.from_args(args, repo=repo, results_root=root, fresh=True,
                            runner=my_runner)         # an AgentRunner, or an optimizer callable
# per task:
injected      = econ.injection_prefix(task)
exclude_tools = econ.build_tools(task, i)
run_kwargs    = econ.run_kwargs(task)
# ... execute the task through the runner with those decisions ...
opt_tokens, reason = econ.after_task(task, row, run_dir_path, srow)
```
