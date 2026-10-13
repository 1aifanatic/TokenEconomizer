#!/usr/bin/env python3
"""Export the canonical workloads to ``workloads/<domain>.json`` (committed, not ignored).

``tokeneconomizer/workloads.py`` is the authority; these JSON files are the machine-readable
copy the shell launchers and any non-Python harness can read without importing the package.
They are tiny (ids only, no issue text) and therefore **committed** — unlike the results
tree, which is regenerated per run and git-ignored.

    python scripts/export_workloads.py           # rewrite workloads/*.json
    python scripts/export_workloads.py --check    # verify they match (exit 1 if stale)

``tests/test_workloads.py`` runs the same comparison, so drift fails the suite.
"""
import argparse
import json
import os
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_harness; the core at the root

from tokeneconomizer import workloads  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "workloads")

_DOC = {
    "swebench": "SWE-bench task streams for the Claude/Copilot trimech A/B and the Codex arms.",
    "webbench": "Web-Bench streams; task order is load-bearing (each task builds on the last).",
    "creddata": "CredData repositories audited for secrets, graded to line-level F1.",
    "reposvul": "ReposVul instances (Python split) audited for vulnerabilities, graded to F1.",
}
_GROUP_FIELD = {"swebench": "repo", "webbench": "project",
                "creddata": "instance", "reposvul": "instance"}
_SEEDS = {"swebench": list(workloads.SWEBENCH_SEEDS),
          "webbench": list(workloads.WEBBENCH_SEEDS),
          "creddata": list(workloads.CREDDATA_SEEDS),
          "reposvul": list(workloads.REPOSVUL_SEEDS)}


def build(domain):
    """The exported document for one domain."""
    groups = {"swebench": workloads.SWEBENCH_INSTANCES,
              "webbench": workloads.WEBBENCH_TASKS,
              "creddata": {"creddata": workloads.CREDDATA_INSTANCES},
              "reposvul": {"reposvul": workloads.REPOSVUL_INSTANCES}}[domain]
    # The exported manifest is what the LAUNCHERS run, so it carries the runnable set
    # (canonical PLUS workloads.SWEBENCH_ADDED, MINUS workloads.SWEBENCH_EXCLUDED), not the
    # historical one. The excluded ids and their reasons ship alongside, so a short cell
    # reads as "deliberately skipped" rather than "silently missing", and the added ids ship
    # alongside too, so a LONGER cell reads as "deliberately extended" rather than "someone
    # ran the wrong list". workloads.task_counts()/total_tasks() are unchanged.
    groups = workloads.runnable_instances(domain) if domain == "swebench" else groups
    doc = {
        "domain": domain,
        "description": _DOC[domain],
        "source": "tokeneconomizer/workloads.py (regenerate: python scripts/export_workloads.py)",
        "group_field": _GROUP_FIELD[domain],
        "seeds": _SEEDS[domain],
        "total_tasks": sum(len(v) for v in groups.values()),
        "task_counts": {g: len(v) for g, v in groups.items()},
        "groups": {g: list(v) for g, v in groups.items()},
    }
    if domain == "swebench" and workloads.SWEBENCH_EXCLUDED:
        doc["excluded"] = dict(sorted(workloads.SWEBENCH_EXCLUDED.items()))
    if domain == "swebench" and workloads.SWEBENCH_ADDED:
        doc["added"] = {g: sorted(v) for g, v in sorted(workloads.SWEBENCH_ADDED.items())}
    if domain == "swebench":
        doc["full_split_only"] = sorted(workloads.SWEBENCH_FULL_SPLIT_ONLY)
        doc["notes"] = (
            "These ids are NOT all in SWE-bench_Verified: 'full_split_only' lists the "
            "instances that exist only in the full princeton-nlp/SWE-bench test split. "
            "Docker image tag: swebench/sweb.eval.x86_64.<id with __ -> _1776_>:latest. "
            "'excluded' lists ids dropped from THIS run list with the reason; they are "
            "still part of the recorded workload in tokeneconomizer/workloads.py "
            "(SWEBENCH_INSTANCES) and were run by the published results, so a cell that "
            "skips them is not directly comparable to one that did not. "
            "'added' lists ids that EXTEND a stream beyond the published workload to "
            "de-saturate it (scikit-learn and sympy, to 60 runnable tasks each); they are "
            "all outside SWE-bench_Verified, so provision them from the full test split, "
            "and results on an extended stream are not comparable to the published ones "
            "for that repo.")
    return doc


def path_for(domain):
    return os.path.join(OUT_DIR, f"{domain}.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if any file is missing or stale")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    stale = []
    for domain in workloads.DOMAINS:
        doc = build(domain)
        dest = path_for(domain)
        text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
        if args.check:
            current = open(dest, encoding="utf-8").read() if os.path.exists(dest) else None
            if current != text:
                stale.append(os.path.relpath(dest, REPO))
            continue
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {os.path.relpath(dest, REPO)}  "
              f"({doc['total_tasks']} tasks, {len(doc['groups'])} groups)")

    if args.check:
        if stale:
            print("STALE (run: python scripts/export_workloads.py):", *stale, sep="\n  ")
            return 1
        print(f"workloads/*.json up to date ({len(workloads.DOMAINS)} domains)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
