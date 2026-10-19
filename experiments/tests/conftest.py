"""pytest setup for the reproduction suite (experiments/tests).

`experiments/` holds te_harness/, te_bench/, scripts/ and workloads/, so a test's "repo
root" (`_REPO` in the test files) is experiments/.
The shared core, `tokeneconomizer`, lives one level up at the repository root, and so do
the shared transcript writers (`fixtures`), with the plugin's own tests in tests/.
"""
import os
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TESTS)
for p in (os.path.dirname(_REPO), os.path.join(os.path.dirname(_REPO), "tests"), _REPO, _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)
