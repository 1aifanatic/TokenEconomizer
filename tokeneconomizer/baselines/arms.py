"""Arms that are *only* an external tool baseline — no mechanism active.

These are the comparison points: somebody else's off-the-shelf tool installed into the
agent's container, with the economizer inert. The scaffold does the work; the arm just
names which one, and `ARM_SCAFFOLDS` in this package's `__init__` maps arm -> scaffold for
the harness.

The `trimech-rtk` / `trimech-repomap` arms are NOT here. They are `trimech` with a scaffold
co-activated, so they live beside `trimech` in `mechanisms/arms.py` -- the mechanisms are
the subject of those arms and the scaffold is the control, not the other way round.

Nothing here imports a scaffold class: `scaffold=` is a string the harness resolves lazily
through `tokeneconomizer.baselines.get()`, so importing this module costs nothing.
"""
from .._armspec import arm, cfg

__all__ = ["BASELINE_ARMS"]

BASELINE_ARMS = {
    "rtk": arm(
        "standalone",
        lambda: cfg(), scaffold="rtk"),
    "serena": arm(
        "standalone",
        lambda: cfg(), scaffold="serena"),
    "repomap": arm(
        "standalone",
        lambda: cfg(), scaffold="repomap"),
}
