#!/usr/bin/env python3
"""Derive `projects/<name>/tasks.jsonl` from a Web-Bench project's `tasks.yml`.

Some Web-Bench projects ship only `tasks.yml`. Two readers on our side take `tasks.jsonl`
and nothing else -- `te_harness.webbench.WebBenchStream._tasks` and
`te_bench.adapters.webbench.WebBenchAdapter.tasks_path` -- so those projects need a
derived jsonl before they can be run or judged.

The yml is left in place ON PURPOSE. Web-Bench's own evaluator prefers it when both exist
(`tools/evaluator/src/settings/bench.ts`), so deleting it would move the evaluator onto our
derived file, and editing one without the other would silently desync the graded task order
from the driver's. The yml stays authoritative; this writes its faithful mirror.

    python scripts/webbench_tasks_from_yaml.py zustand            # write the jsonl
    python scripts/webbench_tasks_from_yaml.py --all              # every swept yml project
    python scripts/webbench_tasks_from_yaml.py --check --all      # verify, exit 1 if stale

`--check` is what `tests/test_workloads.py` calls: drift between the two files fails the
suite instead of surfacing as a mislabelled cell after a paid lane.
"""
import argparse
import json
import os
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_harness; the core at the root

try:
    import yaml
except ImportError:                                          # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

PROJECTS_ROOT_ENV = "TOKENECONOMIZER_WEBBENCH_PROJECTS"
# The keys the readers use. `id` and `description` are load-bearing (the driver keys tasks by
# id; the untool judge reads the description); `date` and `level` are carried so the derived
# file is a faithful mirror rather than a lossy projection.
FIELDS = ("id", "date", "level", "description")


def projects_root(explicit=None):
    root = explicit or os.environ.get(PROJECTS_ROOT_ENV)
    if not root:
        sys.exit(f"no Web-Bench projects root: set ${PROJECTS_ROOT_ENV} or pass --projects-root")
    return root


def render(yml_path):
    """The jsonl text this project's tasks.yml maps to. One compact object per line."""
    with open(yml_path, encoding="utf-8") as fh:
        tasks = yaml.safe_load(fh) or []
    out = []
    for task in tasks:
        # `date` round-trips through PyYAML as a datetime.date, which json cannot encode; the
        # jsonl form the shipped files use is the plain ISO string.
        # `description` is a YAML block scalar (`|`), so PyYAML hands back a trailing
        # newline that the shipped jsonl files do not carry. Verified against `prisma`, the
        # one project shipping BOTH files: its 20 records match the yml exactly once the
        # descriptions are stripped. (Only the CONTENT is a convention -- the shipped files
        # disagree on separator style, `flex` compact and `prisma` spaced, so that is ours.)
        rec = {}
        for k in FIELDS:
            v = task.get(k)
            if v is None:
                rec[k] = ""
            elif k == "date":
                rec[k] = str(v)
            elif k == "description":
                rec[k] = v.strip()
            else:
                rec[k] = v
        # Compact separators to match the shipped jsonl files byte-for-byte in shape.
        out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    return "".join(line + "\n" for line in out)


def swept_yml_projects(root):
    """Projects in the SWEEP that ship a tasks.yml -- the only ones this has to keep in step.

    Deliberately not "every project with a tasks.yml": the checkout has ~18 of those, most of
    them outside the sweep, and one (`prisma`) ships both files. Writing to those would mutate
    a checkout the sweep never reads."""
    from te_bench import workloads
    for name in workloads.WEBBENCH_TASKS:
        if os.path.isfile(os.path.join(root, name, "tasks.yml")):
            yield name


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="*", help="project name(s); omit with --all")
    ap.add_argument("--all", action="store_true",
                    help="every SWEPT project that ships a tasks.yml")
    ap.add_argument("--check", action="store_true",
                    help="verify the jsonl matches the yml; write nothing, exit 1 if stale")
    ap.add_argument("--projects-root", default=None)
    args = ap.parse_args()

    root = projects_root(args.projects_root)
    names = list(swept_yml_projects(root)) if args.all else args.project
    if not names:
        ap.error("name at least one project, or pass --all")

    stale = []
    for name in names:
        yml = os.path.join(root, name, "tasks.yml")
        if not os.path.isfile(yml):
            sys.exit(f"{name}: no tasks.yml at {yml}")
        jsonl = os.path.join(root, name, "tasks.jsonl")
        want = render(yml)
        have = open(jsonl, encoding="utf-8").read() if os.path.isfile(jsonl) else None
        if have == want:
            print(f"  ok      {name}  ({want.count(chr(10))} tasks)")
            continue
        if args.check:
            stale.append(name)
            print(f"  STALE   {name}  ({'missing' if have is None else 'differs'})")
            continue
        with open(jsonl, "w", encoding="utf-8") as fh:
            fh.write(want)
        print(f"  wrote   {name}  ({want.count(chr(10))} tasks) -> {jsonl}")

    if stale:
        sys.exit(f"\n{len(stale)} project(s) stale: {' '.join(stale)}\n"
                 f"regenerate with: python scripts/webbench_tasks_from_yaml.py --all")


if __name__ == "__main__":
    main()
