"""Single import point for trace reading -- the one normalized seam every mechanism
reads agent trajectories through.

All three mechanisms (injection / tooling / spiral) consume a saved rollout transcript
through the four functions below, so the transcript-schema knowledge lives in exactly one
place. The heavy lifting is delegated to the vendored `reflect` reader, which auto-detects
the supported transcript schemas (Claude Code / Copilot CLI).

`parse_catalog` is the only agent-specific branch: it returns the tool catalog the agent
loaded for a task. For Codex there is no per-task JSON tool dump, so we return the known
default surface (inlined below, so this package needs no runner/`agents` dependency).
"""
import json
import os
import re

from .vendor import reflect

# Codex ships a fixed default tool surface (it does not dump JSON tool schemas into a
# debug log). Inlined here -- verbatim from the original CodexDriver.DEFAULT_TOOL_CATALOG
# -- so catalog-size telemetry reflects what is actually loaded (and prunable) without a
# dependency on the Docker/CLI runner layer.
CODEX_DEFAULT_TOOL_CATALOG = {
    "shell": "Run shell commands to inspect, edit, and test the repository.",
    "apply_patch": "Apply source-code file changes.",
    "web_search": "Search the web (inapplicable on air-gapped SWE-bench).",
    "view_image": "Attach a local image to context (unused on text tasks).",
    "update_plan": "Maintain a step plan (scaffolding, unused here).",
    "spawn_agent": "Spawn a collaborating subagent.",
    "send_input": "Send input to a collaborating subagent.",
    "resume_agent": "Resume a collaborating subagent.",
    "wait_agent": "Wait for a collaborating subagent.",
    "close_agent": "Close a collaborating subagent.",
}


# rtk in its Codex mode is the one scaffold that changes the COMMAND LINE rather than hooking
# it: the agent is told to type `rtk pytest`, `rtk git status`, `rtk proxy <cmd>`. Every
# classifier that reads a command's first token -- the environment-fact vocabulary, the
# spiral's test-run detector -- would otherwise stop seeing a test run, a fetch or a repo
# query on exactly the arm that co-activates them (`codex-trimech-rtk`). One definition, so
# the classifiers cannot disagree about what counts as the wrapper. `rtk proxy` is rtk's own
# passthrough (run unfiltered, still tracked); a bare `rtk` with no command is left alone.
RTK_PREFIX_RE = re.compile(r"^rtk\s+(?:proxy\s+)?(?=\S)")


def strip_rtk_prefix(command):
    """`rtk pytest -q` -> `pytest -q`; anything else unchanged."""
    return RTK_PREFIX_RE.sub("", (command or "").lstrip(), count=1)


def load_trajectory(path):
    """Ordered [(tool_name, args, result_text, success)] over a saved transcript.jsonl.
    Auto-detects the supported agent transcript schemas (delegates to reflect)."""
    return reflect.load_trajectory(path)


def tool_usage(path):
    """{tool_name: count} over a saved transcript (delegates to reflect.tool_usage)."""
    return reflect.tool_usage(path)


def detect(path):
    """LLM-free signal dict over a saved transcript (delegates to reflect.detect)."""
    return reflect.detect(path)


# Ordered catalog sources for the Copilot CLI. The CLI has changed where it publishes the
# loaded tool catalog at least twice, and each time the OLD reader started returning {} --
# which untooling cannot distinguish from "this agent loaded no tools", so it degraded to a
# silent no-op (see reflect.parse_tool_catalog). So instead of one reader with a fallback,
# every known source is tried and the RICHEST result wins: descriptions beat names, more
# tools beat fewer. That way one pipeline serves every CLI build we have seen, chooses per
# run dir at runtime, and a build that publishes two sources is not at the mercy of which
# one we happened to try first.
#
#   schema_debug_log  {name: desc}  CLI <= ~1.0.81   the only source carrying descriptions
#   usage_checkpoint  {name: ""}    CLI ~1.0.82      transcript session.usage_checkpoint
#   flat_debug_log    {name: ""}    CLI >= 1.0.82    "tools": ["bash:66921ccc", ...]
#
# `catalog_sources` returns every source that produced anything, so a caller can record
# which one won and notice the next migration from the rows instead of from a null result.
def _read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def catalog_sources(run_dir_path, agent):
    """[(source_name, {tool: desc}), ...] for every catalog source that yielded tools."""
    if agent == "codex":
        return [("codex_default", dict(CODEX_DEFAULT_TOOL_CATALOG))]
    if agent == "claude":
        cat = reflect.parse_tool_catalog_claude(
            _read(os.path.join(run_dir_path, "transcript.jsonl")))
        return [("claude_transcript", cat)] if cat else []
    dbg = _read(os.path.join(run_dir_path, "copilot_debug.log"))
    tr = _read(os.path.join(run_dir_path, "transcript.jsonl"))
    out = []
    for name, cat in (("schema_debug_log", reflect.parse_tool_catalog(dbg)),
                      ("usage_checkpoint", parse_tool_names_copilot(tr)),
                      ("flat_debug_log", reflect.parse_tool_catalog_flat(dbg))):
        if cat:
            out.append((name, cat))
    return out


def _catalog_rank(item):
    """Richest-first: prefer a source with descriptions, then one with more tools."""
    _, cat = item
    return (sum(1 for v in cat.values() if v), len(cat))


def parse_catalog_with_source(run_dir_path, agent):
    """({tool: desc}, source_name) -- the richest catalog available for this run dir.

    source_name is None when NO source produced anything, which is the condition that used
    to pass silently and turn evidence untooling into a no-op. Callers should treat it as a
    defect signal, not as an empty catalog.
    """
    srcs = catalog_sources(run_dir_path, agent)
    if not srcs:
        return {}, None
    name, cat = max(srcs, key=_catalog_rank)
    return cat, name


def parse_catalog(run_dir_path, agent):
    """Agent-agnostic parse of the loaded tool catalog ({name: desc}) from a task's run
    dir -- Copilot debug log or Claude transcript. Returns {} if unavailable.

    Codex has no per-task JSON tool dump, so its known default surface is returned instead
    (CODEX_DEFAULT_TOOL_CATALOG) so catalog-size telemetry is honest.
    """
    return parse_catalog_with_source(run_dir_path, agent)[0]


def parse_tool_names_copilot(transcript_text):
    """{tool_name: ""} for the tools a Copilot run actually loaded.

    The names ride in `session.usage_checkpoint` events under
    data.promptCacheBreakState[].models[].tools[] -- the same list the model was sent, so
    this reflects what the agent really had, not a static default surface.
    """
    names = set()
    for line in (transcript_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        for pcb in ((ev.get("data") or {}).get("promptCacheBreakState") or []):
            for model in (pcb.get("models") or {}).values():
                for tool in (model.get("tools") or []):
                    name = tool.get("name")
                    if name:
                        names.add(name)
    return {n: "" for n in sorted(names)}
