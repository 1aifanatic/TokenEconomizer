"""Domain adapters for the economizer.

The three mechanisms (injection / tooling / spiral) are domain-neutral: they read a
normalized trajectory and count turns/tokens. Everything domain-specific enters through a
small, named surface -- a `DomainAdapter` (see `base.py`):

  * SwebenchAdapter   -- SWE-bench (the canonical domain).
  * WebBenchAdapter   -- Web-Bench (thin subclass; task-spec source only).
  * CredDataAdapter   -- CredData secret-finding audit (fixed prompt + scan classifier).
  * ReposVulAdapter   -- ReposVul vulnerability audit (same thin-subclass pattern).
  * CyberGymAdapter   -- CyberGym PoC-authoring tasks.
  * WebArenaAdapter   -- stub marking exactly what a browser-episode eval must supply.

The facade threads ONE adapter (default SwebenchAdapter) into all three mechanisms. A new
domain = a new adapter, no mechanism edits. Dataset roots are supplied via env-var
placeholders (TOKENECONOMIZER_*), so no host paths are baked in.
"""
from .base import DomainAdapter
from .swebench import SwebenchAdapter
from .webbench import WebBenchAdapter
from .creddata import CredDataAdapter
from .reposvul import ReposVulAdapter
from .cybergym import CyberGymAdapter
from .webarena import WebArenaAdapter

__all__ = ["DomainAdapter", "SwebenchAdapter", "WebBenchAdapter", "CredDataAdapter",
           "ReposVulAdapter", "CyberGymAdapter", "WebArenaAdapter"]
