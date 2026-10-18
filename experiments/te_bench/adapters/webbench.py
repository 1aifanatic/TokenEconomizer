"""WebBenchAdapter: the economizer's domain surface for Web-Bench.

Web-Bench (ByteDance, arXiv 2505.07473, `bytedance/web-bench`) is 50 Node/TypeScript web
projects, each a stream of 20 sequentially dependent tasks (task N inherits the codebase
built by tasks 1..N-1). Its Node evaluator owns the task loop and grades with Playwright;
the coding agent is driven over HTTP by a Python agent server in the reproduction harness.

The inner coding-agent run per task is the SAME host agent SWE-bench's runner drives, so
the per-task `transcript.jsonl` is byte-compatible with the readers in the vendored
`reflect`. Every trajectory/tool/waste/envelope method transfers UNCHANGED -- that identity
IS the cross-domain transfer claim: only the *task source* changes. So this adapter
subclasses SwebenchAdapter and overrides exactly ONE method:

  problem_statements()  reads `projects/<name>/tasks.jsonl` (Web-Bench task specs) instead
                        of the SWE-bench instances JSON, so the untool per-task judge keys
                        on the real Web-Bench task description.

tasks.jsonl schema (one JSON object per line, `init` + `task-1..task-20`):
  {"id": "init"|"task-N", "date": "...", "level": "easy|...", "description": "..."}

The `projects/` tree is located by TOKENECONOMIZER_WEBBENCH_PROJECTS (a placeholder you set
to your Web-Bench checkout) or an explicit `projects_root=`.
"""
import json
import os

from .swebench import SwebenchAdapter, redact_gold_pointers

PROJECTS_ROOT_ENV = "TOKENECONOMIZER_WEBBENCH_PROJECTS"


class WebBenchAdapter(SwebenchAdapter):
    name = "webbench"

    def __init__(self, project, projects_root=None):
        """`project` is the Web-Bench project name (= a stream, e.g. 'zustand', 'table').
        `projects_root` locates the `projects/` tree (defaults to the
        TOKENECONOMIZER_WEBBENCH_PROJECTS env-var placeholder)."""
        self.project = project
        self.projects_root = projects_root or os.environ.get(PROJECTS_ROOT_ENV)

    def tasks_path(self):
        if not self.projects_root:
            raise RuntimeError(
                f"no Web-Bench projects root: set ${PROJECTS_ROOT_ENV} or pass "
                f"projects_root=.")
        return os.path.join(self.projects_root, self.project, "tasks.jsonl")

    def env_vocabulary(self):
        """Web-Bench projects are Node/npm workspaces: the same families, a different
        vocabulary (node/deno as interpreters, jest/vitest/playwright as runners, npm/yarn/
        pnpm as package tools)."""
        from tokeneconomizer.microskills import NODE_WEB
        return NODE_WEB

    def problem_statements(self):
        """{task_id: redacted_description} for this project's 20-task stream, keyed by the
        same id the orchestrator uses (`init`, `task-1`..`task-20`)."""
        out = {}
        with open(self.tasks_path(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                tid = rec.get("id")
                if not tid:
                    continue
                out[tid] = redact_gold_pointers((rec.get("description") or "").strip())[0]
        return out
