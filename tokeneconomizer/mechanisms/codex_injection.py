"""The Codex lane's injection defaults -- a thin layer over the shared `envfacts` module.

The detector families, the environment-fact distiller and the vocabulary plug-in live in
`mechanisms/envfacts.py` and are shared by every lane: replayed over the stored Claude Code
and Copilot streams they form the same kinds of facts (and abstain on the one stream whose
shell was activated), so nothing about them is Codex-specific. What IS specific to the Codex
lane is recorded here rather than scattered:

  * the default profile is `envfacts+distiller` (2026-09-12): the LLM-free layer with the
    paid proposer on top at cadence 10. It was `envfacts` while the harness left the project
    environment inactive, when the free layer's interpreter fact formed. The harness now
    activates the environment, the interpreter fact no longer forms on a post-fix baseline, and the proposer
    is what remains to find repository facts. Its spend lands in `optimizer_tokens` like anywhere else; the
    LLM-free ablation is the arm `codex-inject-envfacts`;
  * Codex's `command_execution` items carry exact exit codes, which the shared observation
    layer reads directly through `trace.load_trajectory` (Claude and Copilot supply an error
    flag and the outcome is parsed from text);
  * Codex has a developer-role instruction channel (`developer_instructions` in the per-task
    `config.toml`, accepted by codex-cli 0.153.1) that could carry the block instead of the
    prompt prefix. Not wired: every lane injects through `build_prompt` today.

Every public name of `envfacts` is re-exported so existing imports keep working.
"""
from .envfacts import *                            # noqa: F401,F403
from .envfacts import _is_bare, _PASSTHROUGH, _root  # noqa: F401  (offline analyses)

CODEX_DEFAULT_PROFILE = "envfacts+distiller"
CODEX_FREE_PROFILE = "envfacts"          # the LLM-free ablation (arm codex-inject-envfacts)
