"""Stage protocol: the lifecycle hooks the Economizer facade calls around one task.

Each mechanism implements only the hooks it needs; unused hooks keep the no-op default.
The hooks fire in this order per task (see facade.Economizer):

    before_task(task, injected)  -> str        # pre-rollout: may augment injected prefix
    build_tools(task)            -> list|None  # construction: tool-exclusion set
    run_kwargs(task)             -> dict       # construction: kwargs for the agent runner
    after_task(task, row, run_dir_path, srow)  # post-rollout: update state + telemetry
                                 -> opt_tokens (int/float; 0 for stages with no overhead)

Hooks receive the srow (stream row dict) so a mechanism can attach its own telemetry keys.
"""


class Stage:
    name = "base"

    def before_task(self, task, injected):
        return injected

    def build_tools(self, task):
        return None

    def run_kwargs(self, task):
        return {}

    def after_task(self, task, row, run_dir_path, srow):
        return 0
