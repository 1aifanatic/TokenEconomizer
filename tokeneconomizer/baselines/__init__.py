"""External tool scaffolds — the baselines the mechanisms are compared against.

These are NOT mechanisms. A mechanism is TokenEconomizer's own controller acting through
the harness's existing seams; a scaffold here is somebody else's off-the-shelf tool
(`rtk`, Serena, aider's repo map) installed into the sealed agent's container so its cost
and resolve can be measured on the same honest basis.

They sit in the core package, beside the mechanisms they are the comparison for, and the
two invariants that makes people nervous about are both kept structurally rather than by
convention:

  * **Nothing here reaches a container.** Every container interaction arrives as a callable
    on `ScaffoldContext` (`exec_`, `copy_in`, `read_bytes`, `host_run`), supplied by
    whichever harness is driving. No module in this package imports Docker, or subprocess-
    invokes it, or imports `te_harness` -- so any harness that can implement those four can
    run these baselines, and `tests/test_baselines.py` asserts the import surface.
  * **Nothing imports this package unless it is asked to.** `tokeneconomizer/__init__.py`
    does not, and neither does anything under `mechanisms/`, so `import tokeneconomizer`
    still pulls in nothing heavy and the economizer cannot see a baseline even by accident.
    The one non-stdlib import in the tree is `_repomap_gen.py`, which is a subprocess entry
    point rather than a module and is never imported by anything.

One scaffold answers three questions, and nothing else:

  * `mounts(profile)`      -- extra `docker run -v` specs needed at container start
  * `setup(ctx)`           -- install/configure itself in the started container, returning
                              a notes dict that lands in `row.json` (`arm_setup`, versions,
                              whether each step actually took effect)
  * `patch_excludes`       -- paths it writes under /testbed that must NEVER appear in the
                              graded diff

The `patch_excludes` contract is the one that silently corrupts a result if you get it
wrong. A scaffold that writes project instructions into `/testbed/CLAUDE.md` puts that file
in `git diff`, so the graded patch would carry the scaffold's own prompt into the official
evaluation -- scoring the scaffold's text rather than the agent's fix. The runner subtracts
these paths before grading, and records what it subtracted.

What a scaffold may NOT do:

  * change the cost basis. Setup runs before the agent starts and its tokens are the
    agent's own; the scaffold's install traffic is not billed to the model because it is
    not model traffic. An LLM-calling scaffold would have to charge itself through
    `tokeneconomizer.ledger` like a mechanism does, and none of these do.
  * exclude a native tool. Serena wants the agent to stop using Read/Grep/Glob, and the
    original study achieved that with an INSTRUCTION, not `--disallowed-tools`. That is
    kept deliberately: Read is in PROTECTED_TOOLS, so an exclusion would be filtered out
    and the arm would silently run as native. An instruction that the agent may disobey is
    a real property of the baseline, and measuring the baseline as it actually behaves is
    the point.

On the word "scaffold": the directory is `baselines/` because that is the category --
external tool baselines, as opposed to mechanisms. The per-item noun stays "scaffold"
because `baseline` is already the name of the NAIVE arm, and a row carrying both
`arm: "baseline"` and `baseline: "rtk"` would be unreadable.

Registry: `get(name)` resolves a scaffold; `ARM_SCAFFOLDS` maps an arm name to one, and is
the single place that mapping exists (`experiments/scripts/run_experiment.py` reads it so the arm
registry and the harness cannot disagree about which arm carries which scaffold).
"""
from .base import Scaffold, ScaffoldContext, AgentProfile, ScaffoldError
from .repomap import RepoMapScaffold
from .rtk import RtkScaffold
from .serena import SerenaScaffold

__all__ = ["Scaffold", "ScaffoldContext", "AgentProfile", "ScaffoldError",
           "SCAFFOLDS", "ARM_SCAFFOLDS", "get", "for_arm"]

SCAFFOLDS = {
    "rtk": RtkScaffold,
    "serena": SerenaScaffold,
    "repomap": RepoMapScaffold,
}

# Arm -> scaffold. The `trimech-*` arms co-activate a scaffold with all three mechanisms.
#
# There is deliberately NO `trimech-serena`. Serena's contract is that the agent navigates
# through its MCP tools instead of the native ones, and evidence untooling drops tool
# schemas that go unused -- so the two arms contend for the same lever, and a combined arm
# would measure that contention rather than either baseline. RTK (which hooks Bash, always
# protected) and the repo map (which writes a file and touches no tool) do not contend, so
# their combinations are well-defined.
ARM_SCAFFOLDS = {
    "rtk": "rtk",
    "serena": "serena",
    "repomap": "repomap",
    "trimech-rtk": "rtk",
    "trimech-rtk-web": "rtk",      # Web-Bench's spiral tuning; same scaffold
    "trimech-repomap": "repomap",
    # The Codex sibling of trimech-rtk. On Codex the scaffold is instruction-based (there is
    # no hook the CLI honours), so the arm carries an `rtk_compliance` field -- see rtk.py.
    "codex-trimech-rtk": "rtk",
    # The Codex sibling of trimech-repomap. The map is the project AGENTS.md under /testbed,
    # which the Codex profile declares as its instructions file and already excludes from
    # the graded diff; nothing else on that lane writes the file.
    "codex-trimech-repomap": "repomap",
}


def get(name):
    """The scaffold instance for `name`, or None for a falsy name (= no scaffold)."""
    if not name:
        return None
    try:
        return SCAFFOLDS[name]()
    except KeyError:
        raise ScaffoldError(
            f"unknown scaffold {name!r}; known: {', '.join(sorted(SCAFFOLDS))}") from None


def for_arm(arm):
    """The scaffold this arm carries, or None. Unknown arms simply carry none."""
    return get(ARM_SCAFFOLDS.get(arm or ""))
