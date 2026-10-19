"""The benchmark domain adapters the experiments ran.

Each implements the core's `tokeneconomizer.domain.DomainAdapter`, the whole surface through
which a task domain reaches the mechanisms:

  * SwebenchAdapter   -- SWE-bench (the canonical domain).
  * WebBenchAdapter   -- Web-Bench (thin subclass; task-spec source only).
  * CredDataAdapter   -- CredData secret-finding audit (fixed prompt + scan classifier).
  * ReposVulAdapter   -- ReposVul vulnerability audit (same thin-subclass pattern).

The facade threads ONE adapter (default SwebenchAdapter) into all three mechanisms. A new
domain = a new adapter, no mechanism edits. Dataset roots are supplied via env-var
placeholders (TOKENECONOMIZER_*), so no host paths are baked in.
"""
from tokeneconomizer.domain import DomainAdapter
from .swebench import SwebenchAdapter
from .webbench import WebBenchAdapter
from .creddata import CredDataAdapter
from .reposvul import ReposVulAdapter

__all__ = ["DomainAdapter", "SwebenchAdapter", "WebBenchAdapter", "CredDataAdapter",
           "ReposVulAdapter"]
