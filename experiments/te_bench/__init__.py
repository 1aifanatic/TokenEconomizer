"""The benchmark side of TokenEconomizer: what the experiments run the core with.

The core (`tokeneconomizer`, at the repository root) is what the plugin ships: the three
mechanisms, the trace seam, the ledger and their configuration. Everything here exists only to
run those mechanisms over benchmark streams, and nothing in the core or the plugin imports it:

  * `adapters/`   -- the benchmark domains (SWE-bench, Web-Bench, CredData, ReposVul, ...).
  * `facade`      -- `Economizer`, the three mechanisms wired around one adapter.
  * `runner`      -- the `AgentRunner` seam a harness implements (te_harness is the reference).
  * `workloads`   -- the exact task sets the experiments ran.
  * `arms`, `mechanism_arms`, `_armspec` -- the named experiment arms.
  * `baselines/`  -- the external-scaffold comparisons (rtk, Serena, aider's repo map).
"""
