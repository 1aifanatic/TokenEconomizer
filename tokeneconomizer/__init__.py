"""TokenEconomizer: externally-delivered, agent-agnostic middleware that aims to cut a sealed
coding agent's amortized token cost without hurting task success.

Hold the sealed agent fixed (Claude Code, Copilot CLI, Codex); make the environment learn
a cheaper interface to itself. Three independently-switchable mechanisms read recent agent
traces and make adaptive changes to cut effective token cost, each at a different point in
a task's lifecycle:

  * injection  -- inject a firing-gated, learned per-repo operating manual before the task.
  * tooling    -- drop inapplicable tool schemas / capability packages at task construction
                  (generic evidence untooling; Codex-native capability untooling).
  * spiral     -- kill or nudge a stuck rollout in flight (generic guard; Codex-native
                  post-green finalize).

One normalized trace-reading seam (`trace`) and one honest cost ledger (`ledger`) are
shared across all three. The mechanisms are harness-agnostic: task execution and the paid
optimizer call enter through the `runner.AgentRunner` seam, and the task domain enters
through a `DomainAdapter` (default SWE-bench). See `facade.Economizer` for the entry point.

The exact task set the experiments ran -- which instances of which repos, per domain -- is
data in `workloads`, so it is available on a bare clone with no results tree.
"""
from .config import (EconomizerConfig, InjectionConfig, ToolingConfig, SpiralConfig)
from .facade import Economizer
from . import ledger, trace, runner, workloads

__all__ = ["Economizer", "EconomizerConfig", "InjectionConfig", "ToolingConfig",
           "SpiralConfig", "ledger", "trace", "runner", "workloads"]
