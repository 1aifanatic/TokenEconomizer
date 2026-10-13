#!/usr/bin/env python3
"""Host-side aider repo-map generator. Prints the map to stdout.

A separate process, not an import, for two reasons: aider pulls a large dependency tree
that must never enter the runner's own address space (the runner is the thing whose token
accounting has to stay trustworthy, and aider constructs models), and a generator that
crashes or hangs on a pathological repo then costs one non-zero exit instead of the task.

Usage: _repomap_gen.py <src_dir> <map_tokens> [mentioned_idents_csv]
"""
import os
import sys

from aider.io import InputOutput
from aider.models import Model
from aider.repomap import RepoMap

# Directories that are not the library under test. Their contents rank well (many files,
# many symbols) while never containing the fix, so leaving them in spends the token budget
# on documentation and benchmarks instead of on the code the agent has to change.
SKIP = ("/.git", "/__pycache__", "/.tox", "/build", "/dist", "/.eggs", ".egg-info",
        "/doc/", "/docs/", "/examples/", "/benchmarks/", "/tutorial/", "/asv_benchmarks/")


def main():
    repo = os.path.abspath(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 2048
    mentioned = set(sys.argv[3].split(",")) if len(sys.argv) > 3 and sys.argv[3] else set()

    files = []
    for dirpath, _dirnames, filenames in os.walk(repo):
        if any(s in dirpath for s in SKIP):
            continue
        files.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(".py"))

    # stdout is the MAP and nothing else. aider's console writes its progress and warnings
    # to stdout ("Initial repo scan can be slow in larger repos, but only happens once.",
    # model-metadata warnings), and the scaffold takes this process's stdout verbatim as the
    # map body -- so every line aider prints would land in the agent's instructions. Point
    # sys.stdout at stderr while aider is constructed and runs, and write the map afterwards.
    real_stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        io = InputOutput()
        # Used ONLY for its token counter -- no model is called and no key is needed. The
        # map is produced by static analysis; the counter just enforces the budget.
        model = Model("gpt-4o")
        rm = RepoMap(map_tokens=budget, root=repo, main_model=model, io=io, verbose=False)
        text = rm.get_repo_map([], files, mentioned_idents=mentioned) or ""
    finally:
        sys.stdout = real_stdout
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
