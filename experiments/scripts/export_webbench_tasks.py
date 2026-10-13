#!/usr/bin/env python3
"""Export the Web-Bench DRIVER task list (registry A) from `workloads.WEBBENCH_TASKS`.

Web-Bench needs three registries to agree, and this writes the one that is neither committed
nor derivable at run time:

  A  driver task list   this file            $TOKENECONOMIZER_WEBBENCH_TASKS   (JSON list of
                                             {instance_id, problem_statement, repo})
  B  projects tree      the Web-Bench checkout, $TOKENECONOMIZER_WEBBENCH_PROJECTS
  C  workload record    workloads/webbench.json, via scripts/export_workloads.py

`tokeneconomizer/workloads.py` is the authority for WHICH projects and tasks are in the
sweep; A and C are both projections of it. Changing the project list therefore means three
commands, in this order:

    python scripts/webbench_tasks_from_yaml.py --all    # B: yml-only projects need a jsonl
    python scripts/export_workloads.py                  # C
    python scripts/export_webbench_tasks.py             # A

Leaving A stale does not fail loudly -- the driver simply streams the OLD project's tasks
into the new project's evaluator session, and the harness raises
`session is running 'X' but the task belongs to 'Y'` partway in.

    python scripts/export_webbench_tasks.py             # write to $TOKENECONOMIZER_WEBBENCH_TASKS
    python scripts/export_webbench_tasks.py --check     # verify only, exit 1 if stale
"""
import argparse
import json
import os
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_harness; the core at the root

from tokeneconomizer import workloads  # noqa: E402

PROJECTS_ENV = "TOKENECONOMIZER_WEBBENCH_PROJECTS"
TASKS_ENV = "TOKENECONOMIZER_WEBBENCH_TASKS"


def build(projects_root):
    """The driver rows, in the workload's ORDER -- each task builds on the one before it."""
    rows = []
    for proj, task_ids in workloads.WEBBENCH_TASKS.items():
        path = os.path.join(projects_root, proj, "tasks.jsonl")
        if not os.path.isfile(path):
            sys.exit(f"{proj}: no tasks.jsonl at {path}\n"
                     f"  yml-only project? run: python scripts/webbench_tasks_from_yaml.py {proj}")
        by_id = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    by_id[rec["id"]] = rec
        missing = [t for t in task_ids if t not in by_id]
        if missing:
            sys.exit(f"{proj}: tasks.jsonl is missing {missing} named by WEBBENCH_TASKS")
        for tid in task_ids:
            rows.append({"instance_id": tid,
                         "problem_statement": by_id[tid].get("description", ""),
                         "repo": proj})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None, help=f"default: ${TASKS_ENV}")
    ap.add_argument("--projects-root", default=None, help=f"default: ${PROJECTS_ENV}")
    ap.add_argument("--check", action="store_true",
                    help="verify the file matches the registry; write nothing, exit 1 if stale")
    args = ap.parse_args()

    projects_root = args.projects_root or os.environ.get(PROJECTS_ENV)
    if not projects_root:
        sys.exit(f"no Web-Bench projects root: set ${PROJECTS_ENV} or pass --projects-root")
    out = args.out or os.environ.get(TASKS_ENV)
    if not out:
        sys.exit(f"no output path: set ${TASKS_ENV} or pass --out")

    rows = build(projects_root)
    want = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    have = open(out, encoding="utf-8").read() if os.path.isfile(out) else None

    projects = sorted({r["repo"] for r in rows})
    if have == want:
        print(f"ok  {out}\n    {len(rows)} tasks, {len(projects)} projects: {' '.join(projects)}")
        return
    if args.check:
        sys.exit(f"STALE  {out}  ({'missing' if have is None else 'differs from the registry'})\n"
                 f"       regenerate with: python scripts/export_webbench_tasks.py")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(want)
    print(f"wrote {out}\n      {len(rows)} tasks, {len(projects)} projects: {' '.join(projects)}")


if __name__ == "__main__":
    main()
