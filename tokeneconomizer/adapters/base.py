"""DomainAdapter: the complete task-domain coupling surface of the economizer, named.

The three mechanisms depend on their task domain ONLY through the methods below. This
protocol is documentation-as-code: it enumerates exactly what a new domain (a different
benchmark) must supply for the injection / tooling / spiral mechanisms to run unchanged.

Where each is used:

  load_trajectory    -> spiral state, injection distill, detect
  tool_usage         -> tooling used-tool accrual
  parse_catalog      -> tooling catalog snapshot (evidence untooling)
  detect             -> LLM-free waste signals
  protected_tools    -> untool never-drop work surface
  inapplicable_tools -> untool hardcoded drop set
  hardcoded_exclusions -> the concrete drop list given tools seen in use so far
  spiral_snapshot    -> resolved-task turn snapshot for the per-stream envelope tracker
  problem_statements -> {task_id: redacted problem statement} for the per-task tool judge
  classify_domain    -> (optional) a domain-specific brief for a tool classifier; None uses
                        the default. Additive: not every mechanism path consults it.
  env_vocabulary     -> (optional) the `envfacts.Vocabulary` naming this domain's
                        interpreters, test runners, package tools, fetchers and probes, so
                        the environment-fact families read a Node repository as they read a
                        Python one. Absent -> the Python/git default.

The task EXECUTOR + GRADER (run one instance, grade it) is deliberately NOT on this
protocol: it lives at the orchestrator/runner layer, ABOVE the economizer (see
`tokeneconomizer.runner.AgentRunner`), so a new benchmark's executor is swapped there.

Introducing a new domain is a matter of supplying an adapter -- no mechanism edits.
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class DomainAdapter(Protocol):
    """Everything the economizer needs to know about a task domain."""

    name: str

    # ---- trajectory reading (normalized ordered call list) ----
    def load_trajectory(self, path):
        """-> [(tool_name, args_dict, result_text, success_bool)] over a saved transcript."""
        ...

    def tool_usage(self, path):
        """-> {tool_name: count} over a saved transcript."""
        ...

    def parse_catalog(self, run_dir_path, agent):
        """-> {tool_name: description} loaded by the agent for a task (or {} if unavailable)."""
        ...

    def parse_catalog_with_source(self, run_dir_path, agent):
        """-> ({tool: desc}, source_name) -- OPTIONAL. source_name is None when no catalog
        source produced anything, which untooling reports as a defect rather than treating
        as "no tools loaded". Adapters that do not implement it fall back to parse_catalog
        and report the source as 'unknown'."""
        return self.parse_catalog(run_dir_path, agent), "unknown"

    # ---- waste signals (LLM-free) ----
    def detect(self, path):
        """-> dict of task-agnostic inefficiency signals + evidence for this domain."""
        ...

    # ---- untooling tool-sets ----
    def protected_tools(self):
        """-> frozenset of tools that must never be dropped (the real work surface)."""
        ...

    def inapplicable_tools(self):
        """-> tuple of tools inapplicable-by-construction for a single sealed task."""
        ...

    def hardcoded_exclusions(self, used_tools):
        """-> list of tools to drop under the hardcoded untool path, given the set of
        tools observed in use across the stream so far."""
        ...

    # ---- spiral envelope ----
    def spiral_snapshot(self, path):
        """-> a resolved-task turn snapshot for the per-stream envelope tracker."""
        ...

    # ---- per-task judge inputs ----
    def problem_statements(self):
        """-> {task_id: redacted_problem_statement} used by the untool per-task judge."""
        ...

    # ---- environment-fact vocabulary (optional) ----
    def env_vocabulary(self):
        """-> an `envfacts.Vocabulary` for this domain (OPTIONAL; default Python/git)."""
        from ..mechanisms.envfacts import PYTHON_GIT
        return PYTHON_GIT
