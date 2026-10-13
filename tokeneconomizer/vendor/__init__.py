"""Vendored, dependency-free building blocks shared by the economizer mechanisms.

These three modules were lifted verbatim from the original research harness and carry
NO third-party or cross-package dependencies (Python stdlib only). They are vendored
here so `tokeneconomizer` is fully self-contained -- no `sys.path` injection into a
sibling benchmark tree, no external install:

  * pricing  -- cache-aware effective-token cost weighting (MODEL_WEIGHTS price table)
  * reflect  -- LLM-free waste detector + gated reflection + evidence-untool tool judge
  * spiral   -- runtime spiral-state folder + cheapest-sufficient escalation ladder

The only edit applied on vendoring was rewiring spiral's lazy `import reflect` to the
relative `from . import reflect` so the two stay a matched pair inside this package.
"""
from . import pricing, reflect, spiral

__all__ = ["pricing", "reflect", "spiral"]
