#!/usr/bin/env python3
"""Write the SWE-bench files the harness reads, for the 277 evaluated tasks.

    swebench_tasks.json     {instance_id, repo, problem_statement} per task: the driver's task
                            list ($TOKENECONOMIZER_SWEBENCH_TASKS)
    swebench_grading.json   instance_id -> the fields the official grader needs
                            ($TE_SWEBENCH_GRADING)
    image_tags.txt          the Docker image of each task, one per line, to `docker pull`

The ids come from `te_bench.workloads.runnable_instances("swebench")`. Most are in
SWE-bench_Verified; the rest exist only in the full SWE-bench test split, so both are read.
Needs the `datasets` package, which the SWE-bench grader installs.

    python scripts/export_swebench_tasks.py --out-dir "$DATA/swebench"
"""
import argparse
import json
import os
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_bench; the core at the root

from te_bench import workloads  # noqa: E402

SPLITS = ("princeton-nlp/SWE-bench_Verified", "princeton-nlp/SWE-bench")
GRADING_FIELDS = ("repo", "base_commit", "environment_setup_commit", "version", "test_patch",
                  "patch", "FAIL_TO_PASS", "PASS_TO_PASS")


def build(rows_by_split, wanted):
    """(tasks, grading) for the `wanted` ids, in stream order; Verified rows win."""
    found = {}
    for rows in rows_by_split:
        for r in rows:
            if r["instance_id"] in wanted:
                found.setdefault(r["instance_id"], r)
    order = [i for ids in workloads.runnable_instances("swebench").values() for i in ids]
    tasks = [{"instance_id": i, "repo": found[i]["repo"],
              "problem_statement": found[i]["problem_statement"]} for i in order if i in found]
    grading = {i: {k: found[i][k] for k in GRADING_FIELDS} for i in order if i in found}
    return tasks, grading


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", required=True, help="where to write the three files")
    args = ap.parse_args()

    from datasets import load_dataset
    wanted = {i for ids in workloads.runnable_instances("swebench").values() for i in ids}
    tasks, grading = build((load_dataset(s, split="test") for s in SPLITS), wanted)
    missing = sorted(wanted - set(grading))
    if missing:
        sys.exit(f"{len(missing)} evaluated ids are in neither split: {missing[:5]} ...")

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "swebench_tasks.json"), "w", encoding="utf-8") as fh:
        json.dump(tasks, fh)
    with open(os.path.join(args.out_dir, "swebench_grading.json"), "w", encoding="utf-8") as fh:
        json.dump(grading, fh)
    with open(os.path.join(args.out_dir, "image_tags.txt"), "w", encoding="utf-8") as fh:
        fh.writelines(workloads.swebench_image_tag(t["instance_id"]) + "\n" for t in tasks)
    print(f"wrote {len(tasks)} tasks, their grading fields and image tags to {args.out_dir}")


if __name__ == "__main__":
    main()
