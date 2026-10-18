"""SwebenchAdapter: the SWE-bench task domain, as a DomainAdapter.

Every reading method delegates to the core's `trace` reader and tool sets, so
this adapter carries the canonical SWE-bench behavior with no dependency on the Docker/CLI
harness. The only domain-specific I/O is `problem_statements()`, which loads the task-spec
JSON (issue text per instance) named by the `TOKENECONOMIZER_SWEBENCH_TASKS` env var. That
path is a PLACEHOLDER: point it at your SWE-bench_Verified instances JSON
(a list of objects with `instance_id` + `problem_statement`).
"""
import json
import os
import re

from tokeneconomizer import trace
from tokeneconomizer.toolprune.tools import INAPPLICABLE_TOOLS, PROTECTED_TOOLS

# Task-spec source for the per-task tool judge. PLACEHOLDER: set this env var to the path
# of your SWE-bench instances JSON (list of {instance_id, problem_statement, ...}).
SWEBENCH_TASKS_ENV = "TOKENECONOMIZER_SWEBENCH_TASKS"

# Strip direct links to the gold solution (the repo's own PR/issue/commit/blob URLs) from a
# problem statement so the prompt can't hand the agent the answer's location. Bare ticket
# numbers like #1234 are kept (with the network isolated they point nowhere).
_GOLD_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/"
    r"(?:pull|pulls|issues|issue|commit|commits|compare|blob|tree)/\S+", re.I)


def untool_exclusions(used_tools=None, extra_inapplicable=()):
    """The tools to exclude from one sealed task's schema.

    The static `INAPPLICABLE_TOOLS` set (inapplicable by construction for a sealed single
    task), widened by `extra_inapplicable`, less any tool `used_tools` shows in use across the
    stream so far (a repository that genuinely uses one is never starved). `PROTECTED_TOOLS`
    are never excluded."""
    used = set(used_tools or ())
    cand = list(INAPPLICABLE_TOOLS) + [t for t in extra_inapplicable
                                       if t not in INAPPLICABLE_TOOLS]
    return [t for t in cand if t not in PROTECTED_TOOLS and t not in used]


def redact_gold_pointers(text):
    """Return (redacted_text, n_redacted) with gold-solution URLs masked."""
    n = len(_GOLD_URL_RE.findall(text or ""))
    return _GOLD_URL_RE.sub("[link redacted]", text or ""), n


# SWE-bench's task containers have no network, so on this benchmark the web tools cannot work;
# they join the static inapplicable set here, in the benchmark's own adapter, rather than in
# the shared list every domain reads.
NETWORK_TOOLS = ("WebSearch", "WebFetch")


class SwebenchAdapter:
    name = "swebench"

    def __init__(self, tasks_path=None):
        """`tasks_path` locates the SWE-bench instances JSON; defaults to the
        TOKENECONOMIZER_SWEBENCH_TASKS env var (a placeholder you set for your dataset)."""
        self.tasks_path = tasks_path or os.environ.get(SWEBENCH_TASKS_ENV)

    # ---- trajectory reading ----
    def load_trajectory(self, path):
        return trace.load_trajectory(path)

    def tool_usage(self, path):
        return trace.tool_usage(path)

    def parse_catalog(self, run_dir_path, agent):
        return trace.parse_catalog(run_dir_path, agent)

    def parse_catalog_with_source(self, run_dir_path, agent):
        return trace.parse_catalog_with_source(run_dir_path, agent)

    # ---- untooling tool-sets ----
    def protected_tools(self):
        return PROTECTED_TOOLS

    def inapplicable_tools(self):
        return tuple(INAPPLICABLE_TOOLS) + NETWORK_TOOLS

    def hardcoded_exclusions(self, used_tools):
        return untool_exclusions(used_tools, extra_inapplicable=NETWORK_TOOLS)

    # ---- spiral envelope ----
    def spiral_snapshot(self, path):
        from tokeneconomizer.loopguard import state_from_trajectory
        return state_from_trajectory(path).snapshot()

    # ---- per-task judge inputs ----
    def problem_statements(self):
        """{instance_id: redacted_problem_statement} loaded from the task-spec JSON.

        Raises if no task path is configured (set TOKENECONOMIZER_SWEBENCH_TASKS or pass
        tasks_path=) -- the per-task tool judge cannot run without the issue text."""
        if not self.tasks_path:
            raise RuntimeError(
                f"no SWE-bench task spec configured: set ${SWEBENCH_TASKS_ENV} or pass "
                f"tasks_path= to SwebenchAdapter (a JSON list of "
                f"{{instance_id, problem_statement}}).")
        with open(self.tasks_path, encoding="utf-8") as fh:
            instances = json.load(fh)
        return {
            i["instance_id"]:
                redact_gold_pointers((i.get("problem_statement") or "").strip())[0]
            for i in instances}

    def env_vocabulary(self):
        """SWE-bench repositories are Python projects in git checkouts."""
        from tokeneconomizer.microskills import PYTHON_GIT
        return PYTHON_GIT

    def classify_domain(self):
        """Domain-specific tool-classifier brief, or None to use the default. SWE-bench
        uses the default; other domains (read-only audits) override this."""
        return None
