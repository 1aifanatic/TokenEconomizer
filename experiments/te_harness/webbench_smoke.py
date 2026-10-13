"""Offline wiring check for the Web-Bench harness: everything except the LLM.

``webbench.WebBenchRunner`` joins two loops that are easy to get subtly wrong -- the
economizer's driver stepping one task at a time, and Web-Bench's Node evaluator owning its
own sequential task loop behind an HTTP agent contract. A desync there does not crash; it
silently pairs one task's answer with another task's grade. So the rendezvous needs a test
that runs the REAL evaluator, the REAL HTTP contract and the REAL Playwright grading, with
only the paid agent call replaced.

This runner is that test. It is the production ``WebBenchRunner`` with one method
overridden: instead of invoking Claude Code, ``_segment`` copies the project's own
reference solution (``projects/<project>/src``) into the agent workspace and writes a
synthetic ``result`` event in the CLI's schema. Every task should therefore be graded
RESOLVED -- the reference solution is what the suite was written against -- so any failure
is a harness fault, not a model result.

What it proves: the evaluator config, its launch and shutdown, the agent endpoint, the
Started/verdict parsing, the per-attempt rendezvous, workspace materialization, the
changed-file answer, the rows the driver writes, and the driver's stream hooks.
What it does not prove: anything about the agent.

    python3 scripts/run_experiment.py --arm baseline --agent claude --repo calculator \\
      --adapter webbench --tasks-file "$TOKENECONOMIZER_WEBBENCH_TASKS" \\
      --workload workloads/webbench.json --limit 3 \\
      --results-root /tmp/wb_smoke --runner te_harness.webbench_smoke:make_runner --fresh

Its rows are NOT a benchmark: `resolved` reflects the reference solution, not an agent.

Set ``TE_WEBBENCH_SMOKE_FAIL=<task-id>`` to make one task answer with an intentionally empty
page instead of the reference. That exercises the other half of the rendezvous, which the
happy path never reaches: the retry loop (a second Started/request/verdict round for the same
task) and the stream-break short-circuit that must return every later task as unreached
rather than blocking on a Started line the evaluator will never print. Use it on
`calculator`, whose whole app is a single `index.html`, so overwriting that file reliably
fails the suite.
"""
import json
import os
import shutil

from .webbench import (_MAX_FILE_BYTES, _SKIP_DIRS, _SKIP_FILES, WebBenchRunner, _log,
                       _install_signal_cleanup)


class ReferenceWebBenchRunner(WebBenchRunner):
    """WebBenchRunner with the paid agent call replaced by the reference solution."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.agent = "claude"                       # keep the transcript schema honest
        self.fail_task = os.environ.get("TE_WEBBENCH_SMOKE_FAIL", "").strip()
        _log("webbench_smoke: REFERENCE runner -- no agent is called, rows are NOT a "
             "benchmark; every task should grade resolved")
        if self.fail_task:
            _log(f"webbench_smoke: TE_WEBBENCH_SMOKE_FAIL={self.fail_task} -- that task "
                 f"will answer with an empty page, so it should fail every attempt and "
                 f"end the stream")

    def optimizer_call(self, prompt, model="agent"):
        """No LLM: a benign, parseable optimizer answer costing a nominal 40 tokens, so the
        mechanisms' between-task hooks still run and the ledger still sees overhead."""
        return '{"extractors": [], "facts": []}', 40.0

    def _segment(self, workspace, prompt, transcript_fh, exclude_tools, max_turns, guard):
        """Copy the reference solution into the workspace, then write a transcript in the
        CLI's stream-json schema so `tokeneconomizer.trace` has something real to read."""
        project = self._session_project
        src = os.path.join(self.repo, "projects", project, "src")
        copied = _copy_tree(src, workspace)
        if self.fail_task and self._current_task == self.fail_task:
            copied = _blank_pages(workspace) or copied
            _log(f"webbench_smoke: answering {self.fail_task} with an empty page")

        # A minimal but schema-correct transcript: two tool calls and a result event with a
        # cache-heavy breakdown, which is what pricing.effective_tokens reads.
        lines = []
        for i, rel in enumerate(copied[:8]):
            tid = f"t{i}"
            lines.append({"message": {"content": [
                {"type": "tool_use", "id": tid, "name": "Write",
                 "input": {"file_path": rel}}]}})
            lines.append({"message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
        result = {
            "type": "result", "is_error": False, "num_turns": max(1, len(copied)),
            "usage": {"input_tokens": 900, "cache_read_input_tokens": 7000,
                      "cache_creation_input_tokens": 400, "output_tokens": 350},
            "total_cost_usd": 0.0,
        }
        lines.append(result)
        for line in lines:
            transcript_fh.write(json.dumps(line) + "\n")
        transcript_fh.flush()
        return result


_EMPTY_PAGE = ("<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"UTF-8\">"
               "<title>empty</title></head>\n<body></body>\n</html>\n")


def _blank_pages(workspace):
    """Overwrite every HTML file in the workspace with a valid but empty page.

    Valid, not empty-string: the evaluator's Writer treats empty content as "leave the
    existing file alone", and its file validator rejects malformed HTML -- either would make
    the injected failure a harness error rather than the graded failure being tested.
    """
    hit = []
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in names:
            if name.endswith(".html"):
                path = os.path.join(root, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(_EMPTY_PAGE)
                hit.append(os.path.relpath(path, workspace).replace(os.sep, "/"))
    return hit


def _copy_tree(src, dst):
    """Copy `src` over `dst`, skipping what is never part of a Web-Bench answer. Returns
    the relative paths written."""
    written = []
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in names:
            if name in _SKIP_FILES or name.startswith("."):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, src).replace(os.sep, "/")
            try:
                if os.path.getsize(path) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copyfile(path, out)
            written.append(rel)
    return written


def make_runner():
    _install_signal_cleanup()
    return ReferenceWebBenchRunner()
