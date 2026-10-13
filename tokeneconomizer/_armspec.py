"""The `Arm` type and its two constructors, shared by the arm tables.

A leaf module on purpose: `mechanisms/arms.py` and `baselines/arms.py` both import from
here, and `tokeneconomizer/arms.py` imports both of them, so the primitives cannot live in
either of those without a cycle.

An `Arm` is (mode, config-factory, env-overlay, scaffold):

* ``mode``     -- the agent's prompt-prefix scheme, which lands in the run-dir name so
                  ``aggregate.py`` can recover it. It is ``manual4`` exactly when the arm
                  enables injection (that is what manual4 *is*: the micro-skill prefix) and
                  ``standalone`` otherwise. `tests/test_sweep_arms.py` pins that, because
                  the field is written per arm rather than derived and could drift.
* ``cfg``      -- a zero-arg factory returning this arm's `EconomizerConfig`. A factory,
                  not an instance, so no two arms can share mutable config state.
* ``env``      -- harness-environment overlay applied BEFORE the runner is constructed, for
                  arm properties the economizer does not own (reasoning effort lives in the
                  agent CLI, not in a mechanism).
* ``scaffold`` -- names an EXTERNAL TOOL BASELINE the harness installs into every task
                  container. **A name, not an object**: the string is resolved lazily by
                  the harness through ``tokeneconomizer.baselines.get()``. That is what lets
                  an arm table mention `rtk` without importing the baselines package, which
                  in turn is what keeps `import tokeneconomizer` free of their dependencies.

A scaffold deliberately does not appear in `EconomizerConfig`: the economizer must not be
able to see or reach one. The arm tables are where a mechanism config and a scaffold name
meet, and the only place an arm name maps to either.
"""
import collections

from .config import EconomizerConfig, InjectionConfig, ToolingConfig, SpiralConfig

__all__ = ["Arm", "cfg", "arm"]

Arm = collections.namedtuple("Arm", "mode cfg env scaffold")


def cfg(injection=None, tooling=None, spiral=None):
    """An `EconomizerConfig` with every unnamed mechanism left at its inert default."""
    return EconomizerConfig(
        injection=injection or InjectionConfig(),
        tooling=tooling or ToolingConfig(),
        spiral=spiral or SpiralConfig())


def arm(mode, config, env=None, scaffold=None):
    return Arm(mode, config, env or {}, scaffold)
