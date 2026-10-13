"""Economizer facade: drives the three lifecycle mechanisms around one task.

A caller builds one Economizer per repo stream and calls, per task, in this order:

    injected      = econ.injection_prefix(task)      # learned manual for this task
    exclude_tools = econ.build_tools(task, i)         # untool exclusion set / capability mask
    run_kwargs    = econ.run_kwargs(task)             # spiral params for the runner
    ... run the task via the AgentRunner with those decisions ...
    opt_tokens, reason = econ.after_task(task, row, run_dir_path, srow)

The facade owns runner resolution (the ``runner or make_runner()`` fallback so the paid
proposer / tool-judge / spiral-judge / nudge-writer closures work even when no standing
runner is threaded) and calls each mechanism's post-task hook in a fixed order
(tooling -> spiral -> injection) so the emitted row is deterministic.

`adapter` is the DomainAdapter the mechanisms read the task domain through (default
SwebenchAdapter). `runner` may be an AgentRunner or a bare ``(prompt, model) ->
(text, eff_tokens)`` optimizer callable; it is adapted for the mechanisms automatically.
"""
from .config import EconomizerConfig
from .runner import as_optimizer_callable
from .adapters.swebench import SwebenchAdapter
from .mechanisms.injection import PrefixStage
from .mechanisms.tooling import ToolingStage
from .mechanisms.spiral import SpiralStage


class Economizer:
    def __init__(self, config, *, agent, repo, results_root, fresh,
                 optimizer_model="agent", max_turns=80,
                 runner=None, make_runner=None, adapter=None):
        """`runner` supplies the paid optimizer call; `make_runner` is a zero-arg factory
        used to lazily build one only when a mechanism actually needs it. Either may be an
        AgentRunner or a bare optimizer callable. `adapter` defaults to SwebenchAdapter."""
        self.config = config
        self.adapter = adapter if adapter is not None else SwebenchAdapter()

        def _resolve():
            r = runner if runner is not None else (make_runner() if make_runner else None)
            return as_optimizer_callable(r)

        # ---- injection ----
        # The paid proposer needs a live optimizer runner; resolve one lazily. The LLM-free
        # `envfacts` profile makes no call, so it gets none (config.needs_optimizer agrees).
        injection_runner = _resolve() if config.injection.paid else None
        self.injection = PrefixStage(
            config.injection, repo, results_root, fresh,
            runner=injection_runner, optimizer_model=optimizer_model,
            adapter=self.adapter, agent=agent)

        # ---- tooling (untooling) ----
        judge_tool_runner = _resolve() if config.tooling.judge else None
        self.tooling = ToolingStage(
            config.tooling, agent, optimizer_model,
            judge_tool_runner=judge_tool_runner,
            adapter=self.adapter, repo=repo, results_root=results_root, fresh=fresh)

        # ---- spiral ----
        spiral_runner = None
        if config.spiral.guard and (config.spiral.judge or config.spiral.nudge_llm):
            spiral_runner = _resolve()
        self.spiral = SpiralStage(
            config.spiral, max_turns,
            runner=spiral_runner, optimizer_model=optimizer_model,
            adapter=self.adapter, agent=agent)

    @classmethod
    def from_args(cls, args, *, repo, results_root, fresh, runner=None,
                  make_runner=None, adapter=None):
        return cls(EconomizerConfig.from_args(args),
                   agent=args.agent, repo=repo, results_root=results_root,
                   fresh=fresh, optimizer_model=getattr(args, "optimizer_model", "agent"),
                   max_turns=getattr(args, "max_turns", 80),
                   runner=runner, make_runner=make_runner, adapter=adapter)

    # ---- per-task hooks the orchestrator calls ----
    def injection_prefix(self, task=None):
        """The learned per-repo manual for this task ('' if injection disabled)."""
        return self.injection.render(task=task) if self.config.injection.enabled else ""

    def build_tools(self, task, i):
        """Tool-exclusion set / capability mask for this task (None if untooling off)."""
        return self.tooling.build_tools(task, i)

    def run_kwargs(self, task):
        """Per-task runner kwargs from the tooling and spiral mechanisms."""
        out = dict(self.tooling.run_kwargs(task))
        out.update(self.spiral.run_kwargs(task))
        return out

    def after_task(self, task, row, run_dir_path, srow):
        """Run every mechanism's post-task hook in a fixed order and return
        (opt_tokens, reason|None). `reason` is set only by the injection stage."""
        self.tooling.after_task(task, row, run_dir_path, srow)
        self.spiral.after_task(task, row, run_dir_path, srow)
        if self.config.injection.enabled:
            return self.injection.after_task(task, row, run_dir_path, srow)
        return 0, None
