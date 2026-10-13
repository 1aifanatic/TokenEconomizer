"""te_harness — the Layer-2 task-execution harness for TokenEconomizer.

This is the reference implementation of `tokeneconomizer.runner.AgentRunner`: the Docker- /
CLI- / credential-heavy machinery that sits BELOW that seam, and the one that produced every
published row. It is the harness the results are reproducible with, not a requirement of the
method -- the economizer above the seam never learns which harness it is talking to.

It is a SIBLING package of `tokeneconomizer`, not a submodule of it, and is excluded from
the distributed wheel (see pyproject.toml). That boundary is what keeps the core's
stdlib-only guarantee structural rather than a convention: `import tokeneconomizer` cannot
reach Docker or an agent CLI because the code that shells out to them is not under it.
(This package happens to be stdlib-only at import time too -- the official SWE-bench grader
is imported lazily in `swebench._import_swebench` -- so nothing here adds an install-time
dependency either.)

Use it from the repo's driver, which puts experiments/ and the repository root (where the
shared core lives) on `sys.path` itself, so no PYTHONPATH is needed from any working
directory:

    python scripts/run_experiment.py --runner te_harness.swebench:make_runner ...

Both `make_runner` factories take the driver's `--agent` label and return the runner for
that agent -- `claude` (Claude Code, on the Anthropic API), `copilot` (GitHub Copilot CLI,
on its own GitHub or BYOK auth) or `codex` (OpenAI Codex CLI, on the OpenAI API). An
unknown label raises rather than falling back, because a runner that quietly ran the wrong
agent would produce plausible rows under the wrong name. Copilot setup: docs/COPILOT.md.
"""
import os as _os
import sys as _sys

# The shared core, `tokeneconomizer`, lives at the repository root, one level above
# experiments/. Appended rather than prepended, so an installed core (`pip install -e .`)
# still wins; this only covers `python -m te_harness....` run from experiments/ on a clone.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _ROOT not in _sys.path:
    _sys.path.append(_ROOT)

from .swebench import (make_runner, ClaudeSwebenchRunner,  # noqa: E402,F401
                       CopilotSwebenchRunner, RUNNERS as SWEBENCH_RUNNERS)
from .webbench import (make_runner as make_webbench_runner,  # noqa: E402,F401
                       WebBenchRunner, CopilotWebBenchRunner,
                       RUNNERS as WEBBENCH_RUNNERS)
from . import copilot  # noqa: E402,F401

__all__ = ["make_runner", "ClaudeSwebenchRunner", "CopilotSwebenchRunner",
           "make_webbench_runner", "WebBenchRunner", "CopilotWebBenchRunner",
           "SWEBENCH_RUNNERS", "WEBBENCH_RUNNERS", "copilot"]
