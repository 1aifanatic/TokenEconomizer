"""The three lifecycle mechanisms of the economizer, each acting at a different point in
a task's lifecycle and independently switchable:

  * injection    (mechanisms.injection.PrefixStage)  -- before the task: inject a
                 firing-gated, learned per-repo operating manual.
  * tooling      (mechanisms.tooling.ToolingStage)   -- at task construction: drop
                 inapplicable tool schemas (generic evidence untooling; Codex-native
                 capability untooling).
  * spiral       (mechanisms.spiral.SpiralStage +    -- in flight / at finalize: kill or
                 mechanisms.codex_spiral)              nudge a stuck rollout; Codex-native
                                                        post-green finalize.

All three read a normalized trajectory through the one trace seam (tokeneconomizer.trace)
and charge their LLM overhead through the one ledger (tokeneconomizer.ledger).
"""
from .base import Stage
from .injection import PrefixStage, Distiller
from .tooling import ToolingStage
from .spiral import SpiralStage
from .codex_spiral import CodexSpiralPolicy, DEFAULT_POLICY

__all__ = ["Stage", "PrefixStage", "Distiller", "ToolingStage", "SpiralStage",
           "CodexSpiralPolicy", "DEFAULT_POLICY"]
