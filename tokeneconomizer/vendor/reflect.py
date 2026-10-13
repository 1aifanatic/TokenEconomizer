#!/usr/bin/env python3
"""
Gated reflection for the amortized self-optimization loop.

A single unified engine: the detector surfaces BOTH environment-discovery waste
(re-probing the interpreter/conda env) AND navigation waste (re-searching for the
same code, re-reading the same files), and the optimizer turns either into durable,
task-agnostic notes (env facts + "where things live" facts) for one compounding
manifest. (Env-discovery is largely solved by the realistic baseline; navigation is
the remaining lever — both flow through the same machinery.)

Two stages, so the paid optimizer runs only when a free check finds something:

  STAGE A  detect(trajectory)            free, every task — arithmetic on the
                                         trajectory we already capture.
  GATE     trigger(signals, window)      fire the paid optimizer ONLY on an
                                         obvious / recurring inefficiency; default
                                         is no-op (the interface stays unchanged).
  STAGE B  optimize(signals, state, ...) paid, rare — an LLM call (model
                                         configurable, default = the agent being
                                         optimized) that emits durable, TASK-AGNOSTIC
                                         repo notes addressing the detected waste.

Because triggers fire often early in a stream and rarely once the interface
matures, optimizer overhead is front-loaded and self-extinguishing.

The detector understands the Copilot CLI transcript schema
(tool.execution_start / tool.execution_complete). Stdlib only.
"""
import json
import os
import re
import subprocess

# ---- inefficiency signal patterns (kept in sync with trace analysis) ---------
ENV_PROBE_RE = re.compile(
    r"(which\s+python|ls\s+/opt/miniconda3|conda\s+env\s+list|conda\s+activate|"
    r"source\s+activate|compgen\s+-c|/envs/|command\s+-v\s+python|"
    r"ls\s+/usr/bin.*python|find\s+/.*python|\bpython\d?\s+--version)",
    re.IGNORECASE)

# Generic command-INTENT buckets (NOT repo-specific): agents routinely burn a
# round-trip discovering the right *form* of a common operation. We watch the
# test-running family because switching forms within a task ("tried pytest, then
# fell back to the repo's runner") is a high-precision, leakage-safe, recurring
# discovery that a durable "use <tool> to run tests" note eliminates.
TEST_FORM_RES = {
    "pytest": re.compile(r"\b(pytest|py\.test|python\s+-m\s+pytest)\b", re.I),
    "runtests.py": re.compile(r"runtests(\.py)?", re.I),
    "manage.py test": re.compile(r"manage\.py\s+test", re.I),
    "tox": re.compile(r"\btox\b", re.I),
    "nox": re.compile(r"\bnox\b", re.I),
    "unittest": re.compile(r"python\s+-m\s+unittest", re.I),
    "trial": re.compile(r"\btrial\b", re.I),
}
# Infrastructure-error markers (NOT test failures like "FAIL:"): these signal the
# command itself was wrong/misconfigured. They matter because agents pipe through
# `2>&1 | tail`, so the shell exit code is 0 and a success-based detector is blind.
ERROR_MARKER_RE = re.compile(
    r"(Traceback \(most recent call last\)|ModuleNotFoundError|ImportError|"
    r"cannot import|command not found|No such file or directory|"
    r"unrecognized arguments|no tests ran|errors during collection|"
    r"is not a registered|ERROR: .*not found|usage: )", re.IGNORECASE)
# External fetches: web/github tools or bash curl/wget to a non-local URL. Attempts
# remain potential discovery waste; successful fetches are the integrity flag because
# blocked attempts cannot import solution evidence into the sealed repository.
EXTERNAL_URL_RE = re.compile(r"\b(curl|wget)\b[^\n]*https?://", re.IGNORECASE)
# Backstop integrity flag: ANY mention of a GitHub gold-source host in a command,
# regardless of the client used (curl, python urllib/requests, etc.). github.com
# matches api.github.com too; api.githubcopilot.com (the model endpoint) is NOT
# matched, so legitimate model traffic never trips this.
GOLD_HOST_RE = re.compile(
    r"(?:github\.com|githubusercontent\.com|codeload\.github)", re.IGNORECASE)
# A network attempt is still useful diagnostic evidence, but an isolation shim or a
# refused connection means no external bytes entered the task.  Keep those attempts
# distinct from successful fetches so integrity validation does not reject a sealed run.
BLOCKED_FETCH_RE = re.compile(
    r"(?:(?:gold|network)-isolation:.*blocked|failed to connect|connection refused|"
    r"could not resolve host|network is unreachable|name or service not known|"
    r"operation timed out)", re.IGNORECASE)
# A command that RUNS code to verify behavior — the test/reproduction step of the
# edit->test->fail iteration loop. Broader than TEST_FORM_RES (it also matches the
# ad-hoc `python -c` reproductions agents use). Used to detect EXECUTION-iteration
# waste (re-running an unchanged command), which drives the costly multi-turn spirals.
TEST_RUN_RE = re.compile(
    r"(runtests(\.py)?|\bpytest\b|py\.test|manage\.py\s+test|python\s+-m\s+unittest|"
    r"\btox\b|\bnox\b|\btrial\b|python\d?\s+-c\b)", re.IGNORECASE)


def _norm_cmd(c):
    return re.sub(r"\s+", " ", c.strip()).lower()


def load_trajectory(path):
    """Return an ordered list of (toolName, command_or_args, result_text, success)
    from a saved agent transcript.jsonl. Auto-detects the format: Copilot CLI emits
    tool.execution_start/complete events; Claude Code emits `assistant` messages whose
    content[] carries `tool_use` blocks (and `user` messages with `tool_result`)."""
    out, pend = [], {}
    if not os.path.exists(path):
        return out
    lines = [l.strip() for l in open(path) if l.strip()]
    # Format sniff: Codex emits completed command/file items directly (there is no
    # start/result pairing). Handle it before the Claude/Copilot paths so every
    # downstream economizer stage sees the same normalized tuple stream.
    if any('"type":"item.completed"' in line.replace(" ", "") for line in lines):
        return _load_trajectory_codex(lines)
    # format sniff: Claude if any line is an assistant/user message-envelope event.
    is_claude = False
    for line in lines:
        if '"tool_use"' in line or ('"type": "assistant"' in line
                                    or '"type":"assistant"' in line):
            is_claude = True
            break
    if is_claude:
        return _load_trajectory_claude(lines)
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        d = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
        if t == "tool.execution_start":
            pend[d.get("toolCallId")] = {"name": d.get("toolName"),
                                         "args": d.get("arguments", {})}
        elif t == "tool.execution_complete":
            info = pend.get(d.get("toolCallId"), {"name": "?", "args": {}})
            res = d.get("result") or {}
            content = res.get("content") if isinstance(res, dict) else ""
            out.append((info["name"], info["args"],
                        content if isinstance(content, str) else "",
                        d.get("success")))
    return out


_SHELL_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)
_SHELL_GIT_APPLY_PATH_RE = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.M)


def _shell_patch_paths(command):
    """Paths touched by a patch applied through the shell, () when it is not one."""
    command = command or ""
    if "*** Begin Patch" in command:
        found = _SHELL_PATCH_FILE_RE.findall(command)
    elif re.search(r"\bgit\s+apply\b", command):
        found = _SHELL_GIT_APPLY_PATH_RE.findall(command)
    else:
        return ()
    return tuple(dict.fromkeys(p.strip() for p in found if p.strip()))


def _load_trajectory_codex(lines):
    """Normalize Codex ``exec --json`` item events.

    Commands become ``shell`` calls with a conventional ``command`` argument;
    file-change items become ``apply_patch`` calls carrying the changed paths.
    Agent-message items are deliberately omitted because they are not tool calls.
    """
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "item.completed":
            continue
        item = ev.get("item") or {}
        kind = item.get("type")
        if kind == "command_execution":
            command = item.get("command") or ""
            output = item.get("aggregated_output") or ""
            try:
                ok = int(item.get("exit_code")) == 0
            except (TypeError, ValueError):
                ok = False
            # A patch applied THROUGH THE SHELL (`apply_patch <<'PATCH' ... *** Begin
            # Patch`) is an edit as much as a native file_change item is; on some model
            # providers it is the only form the CLI emits (2026-09-12). Surface
            # it as the same `apply_patch` call every consumer already reads -- injection
            # drift, the untool audit, the waste signals -- so a provider change cannot
            # blind them.
            paths = _shell_patch_paths(command)
            if paths and (ok or "Success." in output):
                out.append((
                    "apply_patch",
                    {"path": paths[0], "paths": list(paths), "changes": [],
                     "via": "shell"},
                    "",
                    True,
                ))
            out.append(("shell", {"command": command}, output, ok))
        elif kind == "file_change":
            changes = item.get("changes") or []
            paths = [c.get("path") for c in changes
                     if isinstance(c, dict) and c.get("path")]
            out.append((
                "apply_patch",
                {"path": paths[0] if paths else "", "paths": paths,
                 "changes": changes},
                "",
                item.get("status") == "completed",
            ))
    return out


def _load_trajectory_claude(lines):
    """Claude Code stream-json variant of load_trajectory. Pairs each `tool_use` block
    (in an `assistant` message) with its matching `tool_result` (in a later `user`
    message) by tool_use id, yielding the same (name, args, result_text, success)
    tuples the Copilot path returns so all downstream analysis is format-agnostic."""
    out, order, seen = [], [], {}
    results = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tid = b.get("id")
                seen[tid] = {"name": b.get("name"), "args": b.get("input", {})}
                order.append(tid)
            elif b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c
                                 if isinstance(x, dict))
                results[tid] = (c if isinstance(c, str) else "",
                                not b.get("is_error", False))
    for tid in order:
        info = seen[tid]
        res_text, ok = results.get(tid, ("", True))
        out.append((info["name"], info["args"], res_text, ok))
    return out


def _basename(p):
    return p.rstrip("/").split("/")[-1] if p else p


# ---- v4.1 mechanism A: untooling (per-turn-prefix minimization) ---------------
# The sealed agent re-feeds every loaded tool's JSON schema in its context EVERY
# turn (cache_read), so unused tool schemas are a fixed per-turn tax. Because the rollout is
# single-shot we cannot re-enable a tool mid-task, so we exclude ONLY tools that are
# inapplicable BY CONSTRUCTION for one sealed SWE-bench task — the agentic-
# orchestration / session / memory / scheduling / docs meta-tools that cannot help
# edit-and-test a repo and that the agent would only invoke by mistake. File/shell/
# search/edit tools (bash, view, grep, glob, edit, create, read_bash) are NEVER
# excluded. The set is intentionally conservative; widen only with stream evidence.
INAPPLICABLE_TOOLS = (
    # sub-agent orchestration (a single sealed task has nothing to delegate to)
    "task", "list_agents", "read_agent", "write_agent",
    # session DB / scheduling / skills / CLI self-docs — irrelevant to solving code
    "sql", "session_store_sql", "manage_schedule", "skill",
    "fetch_copilot_cli_documentation",
    # persistent memory (also off by default headless) and its vote tool
    "store_memory", "vote_memory",
    # interactive Q&A: headless can never get an answer
    "ask_user",
    # ---- Claude Code tool names (PascalCase; disjoint from the Copilot names
    # above, so listing both is safe regardless of --agent). These are
    # inapplicable-by-construction for one sealed headless SWE-bench task.
    # Work surface (Bash/Edit/Read/Write/Grep/Glob) is kept.
    # sub-agent orchestration + workflow engine (nothing to delegate to):
    "Agent", "Workflow",
    # task list / scheduling / cron (no session persistence in a single task):
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
    # plan-mode + git-worktree session controls (irrelevant to a sealed rollout):
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    # skills, notebooks, and network tools (network is scrubbed in-container):
    "Skill", "NotebookEdit", "WebSearch", "WebFetch",
    # interactive Q&A: headless can never get an answer:
    "AskUserQuestion",
)
# Tools that must always stay available regardless of usage (the real work surface).
PROTECTED_TOOLS = frozenset({
    "bash", "view", "grep", "glob", "edit", "create", "str_replace",
    "read_bash", "stop_bash", "list_bash", "read_file",
    # Codex's complete native work surface (fixed; neither schema is removable).
    "shell", "apply_patch",
    # Claude Code work-surface names:
    "Bash", "Edit", "Write", "Read", "Grep", "Glob", "MultiEdit",
})


def tool_usage(trajectory_path):
    """Map toolName -> call count for one trajectory (free; for untooling evidence
    and stream-wide zero-usage learning)."""
    counts = {}
    for name, _args, _res, _ok in load_trajectory(trajectory_path):
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def untool_exclusions(used_tools=None, extra_inapplicable=()):
    """Return the list of tool names to exclude from the agent's schema this task.

    Conservative by default: the static INAPPLICABLE_TOOLS set (inapplicable by
    construction for a sealed single task). `used_tools` is the set of tools observed
    in use across the stream so far; any inapplicable tool that HAS been used is kept
    (so a repo that genuinely uses one is never starved). PROTECTED_TOOLS are never
    excluded even if listed. `extra_inapplicable` lets a caller widen the set with
    stream evidence (e.g. tools with zero uses across a long window)."""
    used = set(used_tools or ())
    cand = list(INAPPLICABLE_TOOLS) + [t for t in extra_inapplicable
                                       if t not in INAPPLICABLE_TOOLS]
    return [t for t in cand if t not in PROTECTED_TOOLS and t not in used]


# ---- v4.2: self-contained untooling (optimizer-reasoned, not hardcoded) -------
# The v4.1 exclusion set above is a HAND-AUTHORED prior. v4.2 makes untooling
# self-contained: introspect the agent's ACTUAL loaded tool catalog (from its debug
# log — agent-agnostic), then let the optimizer LLM classify each tool KEEP/EXCLUDE/
# UNSURE from its description + a fixed domain brief. EXCLUDE drops immediately; UNSURE
# stays LOADED (safe) until observed-unused over a window, then demotes to EXCLUDE; a
# periodic re-audit reloads the full catalog to recover from drift. Prototype validated:
# deterministic, reproduces the high-value hand exclusions (incl. the ~9.6k-token `task`
# schema), zero unsafe over-exclusions. The hardcoded list remains the offline fallback.

# Matches `"name": "<tool>", ... "description": "<text>"` in the (JSON-in-JSON) debug
# log. Allows optional intervening scalar fields (e.g. `"title": null,`) between name and
# description — newer Copilot CLI schemas insert `"title"`, which previously caused every
# tool except the few lacking a title to be missed.
_CATALOG_RE = re.compile(
    r'"name":\s*"([a-z_]+)"\s*,\s*(?:"[a-z_]+":\s*(?:"[^"]*"|null|true|false|[0-9.]+)\s*,\s*)*'
    r'"description":\s*"')


# Recovering the loaded tool catalog on the Copilot CLI, and why this fallback exists.
#
# Three sources have been tried, in the order the CLI killed them:
#   1. `_CATALOG_RE` over debug-log tool SCHEMAS ("name"+"description" objects). Dead by
#      1.0.82 -- newer builds write no schemas at all.
#   2. `trace.parse_tool_names_copilot` over the transcript's `session.usage_checkpoint`
#      -> `data.promptCacheBreakState[].models[].tools[]`. This is what actually carried
#      1.0.82, and 1.0.83 stopped emitting the event entirely (measured: 1 occurrence per
#      1.0.82 transcript, 0 per 1.0.83 transcript).
#   3. This parser: the flat catalog the debug log still writes on BOTH versions --
#
#          "tools": [ "bash:66921ccc", "read_bash:71c40652", ... ]
#
# With (1) and (2) both dead on 1.0.83 the catalog parsed EMPTY, so `catalog_tools` stayed
# empty, the drop-candidate loop had nothing to iterate, and evidence untooling silently
# became a no-op -- every `untool-*` / `trimech*` arm ran as its own baseline while still
# being named an untooling arm. Measured 2026-09-05 on one sklearn seed: CLI 1.0.82 gave
# `untool_catalog_size=17` and dropped 8 tools on 30 of 31 tasks; CLI 1.0.83, same harness
# and same repo, gave `untool_catalog_size=0` and dropped nothing on 31 of 31.
#
# Source (3) is preferred over (2) because it survived both upgrades. Names only -- the
# fingerprint form carries no descriptions, the same consequence (2) had: evidence
# untooling works (it needs names), the instruction-aware tool JUDGE has nothing to reason
# over and will drop nothing while still billing for the call.
_CATALOG_FLAT_BLOCK_RE = re.compile(r'"tools"\s*:\s*\[(.*?)\]', re.DOTALL)
_CATALOG_FLAT_ITEM_RE = re.compile(r'"([a-z][a-z0-9_]*):[0-9a-f]{6,}"')


def parse_tool_catalog_flat(debug_log_text):
    """{tool_name: ""} from the CLI >= 1.0.83 flat `"tools": ["name:fingerprint", ...]`."""
    out = {}
    for block in _CATALOG_FLAT_BLOCK_RE.findall(debug_log_text or ""):
        for name in _CATALOG_FLAT_ITEM_RE.findall(block):
            out.setdefault(name, "")
    return out


def parse_tool_catalog(debug_log_text, max_desc=200):
    """Extract {tool_name: short_description} from a saved copilot debug log. Works on
    the JSON-in-JSON escaped payload (the tool schemas the model is sent). Agent-agnostic
    in spirit: any agent whose log carries name/description tool defs parses the same.

    Schema-only by design: the flat name:fingerprint catalog is a SEPARATE source
    (`parse_tool_catalog_flat`), and `trace.catalog_sources` tries both and picks the
    richest. Folding the fallback in here would make the winning source unreportable --
    a run using the flat catalog would still claim it had parsed schemas."""
    if not debug_log_text:
        return {}
    s = (debug_log_text.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\'))
    out = {}
    for m in _CATALOG_RE.finditer(s):
        name = m.group(1)
        if name in out:
            continue
        desc = s[m.end():m.end() + max_desc + 40].split('\n')[0][:max_desc]
        out[name] = desc
    return out


# Claude Code emits no debug log of raw schemas the way the Copilot CLI does. Its
# stream-json transcript carries the loaded tool NAMES (the `system` init event's
# `tools` list) but not their descriptions. The descriptions are a static property
# of the CLI build, so we snapshot them ONCE (host-side, from a recorded request) into
# claude_tool_catalog.json and JOIN the transcript's live tool names against it.
# This is the agent-agnostic analog of parse_tool_catalog: same {name: desc} output,
# same downstream classify_tools/learned_exclusions path — only the source differs.
_CLAUDE_CATALOG_SNAPSHOT = os.path.join(os.path.dirname(__file__),
                                        "claude_tool_catalog.json")


def _load_claude_catalog_snapshot(path=None):
    p = path or _CLAUDE_CATALOG_SNAPSHOT
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_tool_catalog_claude(transcript_text, max_desc=200, snapshot=None):
    """Extract {tool_name: short_description} for a Claude Code run. Reads the loaded
    tool NAMES from the transcript's `system` init event and joins them to the
    description snapshot (claude_tool_catalog.json). Names present in the transcript
    but missing from the snapshot get an empty description (the classifier still sees
    the name); snapshot entries whose tool was NOT loaded this run are skipped, so the
    catalog reflects what the agent was actually sent. Returns {} if no system event."""
    if not transcript_text:
        return {}
    snap = snapshot if snapshot is not None else _load_claude_catalog_snapshot()
    names = []
    for line in transcript_text.splitlines():
        line = line.strip()
        if not line or '"type"' not in line or '"system"' not in line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "system" and isinstance(ev.get("tools"), list):
            names = ev["tools"]
            break
    return {n: (snap.get(n, "") or "")[:max_desc] for n in names}


TOOL_CLASSIFY_DOMAIN = (
    "You configure the toolset for an autonomous coding agent that will solve GitHub "
    "issues by READING, EDITING, and TESTING source code in a single repository. "
    "Execution constraints: the agent runs HEADLESS and SINGLE-SHOT (no human is "
    "available to answer questions), in one sealed container per task (no sub-agents, no "
    "persistent cross-session state, no scheduling, no external memory). Each loaded "
    "tool's full JSON schema is re-sent to the model on EVERY turn, costing tokens, so "
    "tools that cannot possibly help edit-and-test code in this setting should be "
    "removed.\n\nFor EACH tool below, classify it:\n"
    "  KEEP    = could plausibly help read/navigate/edit/run/test code in a repo\n"
    "  EXCLUDE = structurally cannot help in this headless single-shot code-fixing "
    "setting\n"
    "  UNSURE  = ambiguous\n"
    "Be conservative: only EXCLUDE when you are confident the tool is inapplicable BY "
    "CONSTRUCTION. Return ONLY a JSON object {tool_name: {\"class\": "
    "\"KEEP|EXCLUDE|UNSURE\", \"reason\": \"<short>\"}}.\n\nTOOLS:\n"
)


def classify_tools(catalog, runner, model="agent", domain=None):
    """Optimizer step (v4.2): classify each tool in `catalog` ({name: desc}) as
    KEEP/EXCLUDE/UNSURE via one LLM call. Returns ({tool: {class, reason}}, tokens).
    Leakage-safe (reads only tool metadata + a fixed domain brief, never task content).
    On any failure returns ({}, tokens) so the caller can fall back to the hardcoded
    list. PROTECTED_TOOLS are force-KEEP regardless of the model (never let the LLM drop
    the core work surface).

    `domain` overrides the classification brief (defaults to the SWE-bench code-fixing
    brief TOOL_CLASSIFY_DOMAIN). A different benchmark (e.g. a read-only secret-finding
    scan) passes its own brief so applicability is judged against ITS task, not SWE-bench's
    — this is what makes the mechanism self-contained across benchmarks rather than
    tied to a hardcoded SWE-bench assumption. The brief must instruct the model to return
    the same {tool: {class, reason}} JSON."""
    if runner is None or not catalog:
        return {}, 0
    prompt = ((domain or TOOL_CLASSIFY_DOMAIN)
              + "\n".join(f"- {n}: {d}" for n, d in catalog.items())
              + "\n\nReturn the JSON classification:")
    raw, real = runner(prompt, model)
    raw = raw or ""
    tokens = real if real is not None else _approx_tokens(prompt) + _approx_tokens(raw)
    out = {}
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            for t, v in obj.items():
                cls = (v.get("class") if isinstance(v, dict) else v) or "UNSURE"
                cls = str(cls).upper()
                if cls not in ("KEEP", "EXCLUDE", "UNSURE"):
                    cls = "UNSURE"
                reason = (v.get("reason") if isinstance(v, dict) else "") or ""
                out[t] = {"class": cls, "reason": str(reason)[:160]}
        except Exception:
            return {}, tokens
    # safety: never EXCLUDE a protected work tool, whatever the model said.
    for t in list(out):
        if t in PROTECTED_TOOLS and out[t]["class"] == "EXCLUDE":
            out[t] = {"class": "KEEP", "reason": "protected work tool (forced)"}
    return out, tokens


TOOL_JUDGE_DOMAIN = (
    "You configure the toolset for an autonomous coding agent that is about to solve ONE "
    "specific GitHub issue by READING, EDITING, and TESTING source code in a single "
    "repository, HEADLESS and SINGLE-SHOT (no human, one sealed container, no sub-agents / "
    "scheduling / external memory). Every LOADED tool's JSON schema is re-sent on EVERY turn "
    "and costs tokens. Below are the ISSUE text and the currently-available tools. For THIS "
    "specific issue, mark each tool:\n"
    "  LOAD = plausibly useful to read/navigate/edit/run/test code to solve THIS issue\n"
    "  DROP = this specific issue will not need it\n"
    "Be conservative: DROP only when you are confident the tool is irrelevant to THIS issue. "
    "Do NOT assume a tool is needed merely because it exists. Return ONLY a JSON object "
    "{tool_name: {\"load\": true|false, \"reason\": \"<short>\"}}.\n"
)


def judge_tools_for_task(problem_statement, catalog, runner, model="agent",
                         domain=None, max_issue_chars=4000):
    """Per-task, INSTRUCTION-AWARE tool judge (v4.4). Reads the (redacted) issue text plus
    the candidate tool catalog ({name: desc}) and returns ({tool: load_bool}, tokens): which
    tools to LOAD for THIS task. Complements the trace-based rule — the caller unions the
    DROP set with the evidence exclusions (and never drops PROTECTED or ever-used tools).

    Leakage-safe: the caller MUST pass the SAME redacted problem statement the agent receives
    (run_agent._redact_gold_pointers), never the gold patch or tests. On any failure returns
    ({}, tokens) so the caller keeps everything loaded (safe). `tokens` is the cache-aware
    EFFECTIVE cost of this one-shot call (fresh input x1 + output x5 when approximated), on
    the same basis as exec, so judge overhead folds into the honest amortized task cost."""
    if runner is None or not catalog or not problem_statement:
        return {}, 0
    brief = domain or TOOL_JUDGE_DOMAIN
    prompt = (brief + "\nISSUE:\n" + problem_statement[:max_issue_chars]
              + "\n\nTOOLS:\n" + "\n".join(f"- {n}: {d}" for n, d in catalog.items())
              + "\n\nReturn the JSON classification:")
    try:
        raw, real = runner(prompt, model)
    except Exception:
        return {}, 0
    raw = raw or ""
    tokens = real if real is not None else (_approx_tokens(prompt) + 5 * _approx_tokens(raw))
    out = {}
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            for t, v in obj.items():
                load = v.get("load") if isinstance(v, dict) else v
                out[t] = bool(load)
        except Exception:
            return {}, tokens
    # safety: never DROP a protected work tool, whatever the judge said.
    for t in PROTECTED_TOOLS:
        if t in out:
            out[t] = True
    return out, tokens


def learned_exclusions(classification, used_tools=None, unused_streaks=None,
                       unsure_demote_after=4):
    """Turn a v4.2 classification into the exclusion list for the NEXT task.

    EXCLUDE-classified tools are excluded (unless protected or since-observed-in-use).
    UNSURE tools stay LOADED until they have gone `unused_streaks[tool]` >=
    `unsure_demote_after` consecutive tasks without use, then they are excluded too
    (evidence-based demotion — safe because they were loaded and observed). KEEP never
    excluded. Falls back to [] when there is no classification yet (caller then loads the
    full catalog for that task = a re-audit/warm-up turn)."""
    if not classification:
        return []
    used = set(used_tools or ())
    streaks = unused_streaks or {}
    excl = []
    for t, v in classification.items():
        if t in PROTECTED_TOOLS or t in used:
            continue
        cls = v.get("class")
        if cls == "EXCLUDE":
            excl.append(t)
        elif cls == "UNSURE" and streaks.get(t, 0) >= unsure_demote_after:
            excl.append(t)
    return excl


def detect(trajectory_path):
    """STAGE A — free. Compute inefficiency signals from a saved trajectory, plus
    TASK-AGNOSTIC evidence the optimizer can turn into durable notes. Two families:

      ENV-DISCOVERY  re-probing for the interpreter / conda env (empty bash probes).
      NAVIGATION     re-finding code: repeated search queries (grep/glob) and files
                     read multiple times — i.e. the agent re-locating the same things.

    Evidence is restricted to *where things are* signals (search queries, file
    paths) — never the contents of a fix — so it cannot encode a task's solution.
    Notes derived from it are location/architecture facts, validated leakage-safe by
    the optimizer's task-agnostic constraint and future-only credit."""
    calls = load_trajectory(trajectory_path)
    bash = [c for c in calls if c[0] in ("bash", "shell")]
    cmds = [(_norm_cmd(a.get("command", "")), res, ok) for _, a, res, ok in bash]

    def is_empty(res):
        return re.sub(r"<shellId.*", "", res or "").strip() in ("", "(empty)")

    # --- env-discovery family ---
    probe_cmds = [c for c, _, _ in cmds if ENV_PROBE_RE.search(c)]
    empty_probe_cmds = [c for c, r, _ in cmds if is_empty(r) and ENV_PROBE_RE.search(c)]
    failed = sum(1 for _, _, ok in cmds if ok is False)
    seen = {}
    for c, _, _ in cmds:
        seen[c] = seen.get(c, 0) + 1
    repeats = sum(v - 1 for v in seen.values() if v > 1)
    env_evidence = []
    for c in (empty_probe_cmds or probe_cmds):
        if c not in env_evidence:
            env_evidence.append(c)
        if len(env_evidence) >= 5:
            break

    # --- navigation family (structured tools + grep-in-bash) ---
    searches, reads = [], []
    for name, args, _, _ in calls:
        if name in ("grep",):
            q = (args.get("pattern") or "").strip()
            if q:
                searches.append(q.lower())
        elif name in ("glob",):
            q = (args.get("pattern") or "").strip()
            if q:
                searches.append(q.lower())
        elif name in ("view",) or "read_file" in (name or ""):
            # built-in view OR the read_file MCP tool (navcache/shadowread arms rename
            # reads to read_file; count them so nav signals + anchoring still work)
            p = args.get("path") or ""
            if p:
                reads.append(p)
    # grep run through bash, too
    for c, _, _ in cmds:
        m = re.search(r"\b(?:grep|rg)\b[^|]*?['\"]([^'\"]{2,40})['\"]", c)
        if m:
            searches.append(m.group(1).lower())

    def _dups(seq):
        s = {}
        for x in seq:
            s[x] = s.get(x, 0) + 1
        return s

    sdup, rdup = _dups(searches), _dups(reads)
    repeated_searches = sum(v - 1 for v in sdup.values() if v > 1)
    redundant_reads = sum(v - 1 for v in rdup.values() if v > 1)
    # evidence: the repeated queries / files (what the agent kept re-locating)
    nav_evidence = ([f"searched repeatedly: {q!r}" for q, n in sdup.items() if n > 1][:4]
                    + [f"re-read file: {_basename(p)}" for p, n in rdup.items() if n > 1][:4])

    # --- tooling-discovery family (generic, type-agnostic friction) ---
    # (a) superseded test-runner form: the agent used >1 distinct test-runner form
    #     in one task -> it discovered the right one the hard way. Ordered, so we can
    #     report "tried X then settled on Y".
    test_forms_seq = []
    for c, _, _ in cmds:
        for form, rx in TEST_FORM_RES.items():
            if rx.search(c):
                if not test_forms_seq or test_forms_seq[-1] != form:
                    test_forms_seq.append(form)
                break
    distinct_forms = list(dict.fromkeys(test_forms_seq))
    superseded_tools = max(0, len(distinct_forms) - 1)
    tooling_evidence = []
    if superseded_tools:
        tooling_evidence.append(
            "tried test runners in order " + " -> ".join(test_forms_seq)
            + f"; '{distinct_forms[-1]}' is the form that stuck")

    # (b) infrastructure errors in command output (masked by pipes; success=True)
    error_cmds = [(c, r) for c, r, _ in cmds if ERROR_MARKER_RE.search(r or "")]
    error_outputs = len(error_cmds)
    for c, r in error_cmds[:3]:
        m = ERROR_MARKER_RE.search(r or "")
        snippet = (m.group(0) if m else "")[:60]
        tooling_evidence.append(f"command errored ({snippet}): {c[:70]}")

    # (c) external fetches: web/github tools or bash curl/wget to a URL. Recorded for
    #     filtering (integrity) and as possible discovery waste; NOT used to trigger
    #     the optimizer (no leakage-safe note can come from "it searched the web").
    external_fetches = 0
    external_fetch_successes = 0
    for name, args, result, success in calls:
        if name in ("web_fetch", "web_search") or (name or "").startswith("github"):
            external_fetches += 1
            external_fetch_successes += int(
                success is not False and not BLOCKED_FETCH_RE.search(result or ""))
    for c, result, success in cmds:
        # curl/wget to any URL, OR any reference to a GitHub gold-source host via any
        # client (python urllib/requests, etc.) — the latter is the backstop that
        # catches non-curl exfiltration the URL regex would miss.
        if EXTERNAL_URL_RE.search(c) or GOLD_HOST_RE.search(c):
            external_fetches += 1
            external_fetch_successes += int(
                success is not False and not BLOCKED_FETCH_RE.search(result or ""))

    # --- execution-iteration family (targets the edit->test->fail loop that drives
    #     the costly multi-turn spirals; cost is ~linear in turns, and these patterns
    #     are turns the agent could have avoided). Evidence is COUNTS ONLY — no
    #     filenames, commands, or test labels — so it stays strictly leakage-safe
    #     (a process-discipline note, never a hint at the fix location). ---
    exec_evidence = []
    # (d) re-running an UNCHANGED test/repro command (the suite is deterministic, so
    #     a repeat of the exact same command yields the exact same result = wasted turn)
    test_cmd_counts = {}
    for c, _, _ in cmds:
        if TEST_RUN_RE.search(c):
            test_cmd_counts[c] = test_cmd_counts.get(c, 0) + 1
    repeated_test_runs = sum(v - 1 for v in test_cmd_counts.values() if v > 1)
    if repeated_test_runs:
        exec_evidence.append(
            f"re-ran an identical test/repro command {repeated_test_runs} extra "
            "time(s) (the suite is deterministic — one run gives the same result)")
    # (e) edit churn: many edits to a SINGLE file, with re-reads interleaved — the
    #     trial-and-error edit storm. Count per file basename (count only, no names).
    edit_seq = []  # ordered basenames of edit targets
    file_edits = {}
    for name, args, _, _ in calls:
        if name in ("edit", "str_replace", "create", "apply_patch"):
            paths = args.get("paths") or [
                args.get("path") or args.get("filePath") or ""]
            for path in paths:
                p = _basename(path)
                if p:
                    edit_seq.append(p)
                    file_edits[p] = file_edits.get(p, 0) + 1
    edit_churn = max(file_edits.values()) if file_edits else 0
    if edit_churn >= 3:
        exec_evidence.append(
            f"made {edit_churn} edits to a single file (trial-and-error iteration); "
            "forming a hypothesis from one focused read before editing, and "
            "verifying with the cheapest reproduction, avoids edit-test churn")

    return {
        "n_bash": len(bash),
        "n_tool_calls": len(calls),
        "env_probes": len(probe_cmds),
        "empty_outputs": sum(1 for _, r, _ in cmds if is_empty(r)),
        "failed_cmds": failed,
        "repeated_cmds": repeats,
        "n_searches": len(searches),
        "repeated_searches": repeated_searches,
        "redundant_reads": redundant_reads,
        "superseded_tools": superseded_tools,
        "error_outputs": error_outputs,
        "external_fetches": external_fetches,
        "external_fetch_successes": external_fetch_successes,
        "repeated_test_runs": repeated_test_runs,
        "edit_churn": edit_churn,
        "evidence_env_cmds": env_evidence,
        "evidence_nav": nav_evidence,
        "evidence_tooling": tooling_evidence,
        "evidence_exec": exec_evidence,
    }


# ---- the gate ----------------------------------------------------------------
DEFAULT_THRESHOLDS = {
    # "obvious in this task": any single signal at/above these fires
    "env_probes": 5,
    "empty_outputs": 6,
    "repeated_cmds": 3,
    "repeated_searches": 3,
    "redundant_reads": 5,
    "superseded_tools": 1,   # one wrong-then-right tool switch is a clean discovery
    "error_outputs": 2,      # repeated infra errors -> a durable "how to" note
    "repeated_test_runs": 1,  # re-running an unchanged test/repro = a wasted turn
    "edit_churn": 5,          # >=5 edits to one file = a trial-and-error edit storm
    # "recurring across window": a signal averaging >= this over the last W tasks
    "window": 3,
    "recurring_env_probes_avg": 2.0,
    "recurring_empty_avg": 3.0,
    "recurring_repeated_searches_avg": 2.0,
    "recurring_superseded_avg": 0.5,
    "recurring_edit_churn_avg": 3.0,
}


def trigger(signals, recent_signals, thr=None):
    """GATE — decide whether to invoke the paid optimizer. Returns (bool, reason).
    Fires on an obvious single-task spike (env-discovery / navigation / tooling-
    discovery / execution-iteration) OR a pattern recurring across the window;
    otherwise no-op (interface unchanged). external_fetches is deliberately NOT a
    trigger (no leakage-safe note can be distilled from web/github access)."""
    thr = thr or DEFAULT_THRESHOLDS
    # (1) obvious single-task inefficiency.
    # NB (v1.4): edit_churn and redundant_reads are deliberately NOT triggers. They
    # measure trial-and-error iteration / re-reading, which is solve-loop work bound to
    # task difficulty, not interface friction. Firing on them produced generic process-
    # habit notes ("read the function first", "batch reads") that added upfront reading
    # WITHOUT shrinking the edit->test loop, inflating cost on matplotlib. They are still
    # computed in detect() and logged as diagnostics, just not acted upon.
    for k in ("superseded_tools", "error_outputs",
              "repeated_test_runs",
              "env_probes", "empty_outputs", "repeated_cmds", "repeated_searches"):
        if signals.get(k, 0) >= thr[k]:
            return True, f"obvious:{k}={signals[k]}>={thr[k]}"
    if METHOD == "v1.3":
        # v1.3 also triggers on edit_churn / redundant_reads (P1, removed in v1.4).
        for k in ("edit_churn", "redundant_reads"):
            if signals.get(k, 0) >= thr[k]:
                return True, f"obvious:{k}={signals[k]}>={thr[k]}"
    # (2) recurring across the window (more general, less leakage-prone)
    win = recent_signals[-thr["window"]:]
    if len(win) >= thr["window"]:
        for sig_key, thr_key in (("env_probes", "recurring_env_probes_avg"),
                                 ("empty_outputs", "recurring_empty_avg"),
                                 ("repeated_searches", "recurring_repeated_searches_avg"),
                                 ("superseded_tools", "recurring_superseded_avg")):
            avg = sum(s.get(sig_key, 0) for s in win) / len(win)
            if avg >= thr[thr_key]:
                return True, f"recurring:{sig_key}_avg={avg:.1f}"
    return False, "below-threshold"


# ---- coverage suppression ----------------------------------------------------
# Signals where a SINGLE durable note addresses ALL future occurrences (e.g. "use
# runtests.py, not pytest" covers every later pytest detour; an env-setup note covers
# every later env re-probe). Once the manifest covers such a signal, re-firing the paid
# optimizer on it just re-derives a duplicate and returns empty — pure waste.
# error_outputs stays uncovered (each infra error can need a genuinely different
# 'use X to do Y' fix). NB (v1.4): edit_churn / redundant_reads are no longer triggers
# (see trigger()), so they are not listed here.
GLOBALLY_COVERABLE = {"superseded_tools", "env_probes",
                      "repeated_test_runs", "repeated_searches"}


def primary_signal(reason):
    """Extract the signal key from a trigger() reason string, e.g.
    'obvious:superseded_tools=1>=1' or 'recurring:env_probes_avg=2.0' -> the key."""
    if not reason or ":" not in reason:
        return None
    body = reason.split(":", 1)[1]
    key = re.split(r"[=]", body, 1)[0]
    key = re.sub(r"_avg$", "", key)
    return key


def is_covered(reason, state):
    """True if the gate fired on a GLOBALLY_COVERABLE signal that the manifest already
    covers — so the paid optimizer would only re-derive a duplicate. Suppress it."""
    k = primary_signal(reason)
    return k in GLOBALLY_COVERABLE and k in set(state.get("covered_signals", []))


def mark_covered(reason, state):
    """Record that a GLOBALLY_COVERABLE signal is now addressed by the manifest. Call
    only after the optimizer actually ADDED a note in response to this firing."""
    k = primary_signal(reason)
    if k in GLOBALLY_COVERABLE:
        cov = state.setdefault("covered_signals", [])
        if k not in cov:
            cov.append(k)


# ---- the paid optimizer ------------------------------------------------------
# METHOD selects the optimizer generation (set via AGENTEO_METHOD env, default v1.4):
#   v1.4 (default): bans process-habit notes (P2) and does NOT trigger on edit_churn/
#                   redundant_reads (P1) — the committed milestone.
#   v1.3          : the prior hardened global manifest — ALLOWS process-discipline
#                   notes (category d) and triggers on edit_churn/redundant_reads.
# (v1.2 location notes are not exposed here; both v1.3/v1.4 forbid location facts.)
METHOD = os.environ.get("AGENTEO_METHOD", "v1.4")

OPTIMIZER_SYSTEM = (
    "You maintain a SHORT list of durable, GENERAL operating notes for working "
    "efficiently in a software repository. You are shown signals of wasted effort "
    "from a recent task trajectory and the current notes. Propose at most {k} NEW "
    "notes that would help FUTURE tasks avoid the same waste.\n\n"
    "GOAL: capture concrete, checkable facts about THIS repo's tooling and "
    "environment so future tasks use the right command/form the first time. NEVER "
    "reduce the number of attempts the agent is willing to make to reach a correct, "
    "test-passing solution, and NEVER tell it to stop early.\n\n"
    "STRICT RULES:\n"
    "- ALLOWED note categories ONLY: (a) the correct command/tool to "
    "build/test/run/lint here, and which forms DON'T work; (b) environment/config "
    "facts and gotchas (interpreter, import-path conventions, how to reproduce); "
    "(c) infrastructure-error fixes (the durable 'use X to do Y' fact behind a "
    "recurring traceback). EVERY note must name a concrete command, flag, path, "
    "tool, or environment fact that an unrelated future task could act on verbatim.\n"
    "- FORBIDDEN: (1) Do NOT write notes about WHERE specific components, symbols, "
    "subsystems, classes, or tests LIVE (no 'X lives in file Y', no per-file or "
    "per-symbol location facts) — these are task-specific, leak localization, and "
    "mislead unrelated tasks. (2) Do NOT write any note that caps iteration, "
    "discourages re-running tests, tells the agent to 'trust' a result, 'make one "
    "consolidated change', stop early, or otherwise reduce its willingness to keep "
    "iterating until the tests actually pass. (3) NEVER describe or hint at the "
    "solution to any specific task, which file to change, or which lines. (4) Do NOT "
    "write generic process/workflow-discipline advice (e.g. 'read the full function "
    "before editing', 'batch related reads in one pass', 'trace callers/data flow "
    "first', 'form a hypothesis before editing', 'reproduce with the cheapest check', "
    "'reuse earlier search results instead of re-searching') — these are not repo-"
    "specific, have no checkable command/path footprint, and slow the edit->test "
    "solve loop without preventing real interface friction.\n"
    "- Each note one line, concise, durable across code changes, and applicable to "
    "ANY task in this repo (not just tasks touching one file).\n"
    "- If nothing general, durable, and repo-wide can be added, return an empty list.\n"
    "Return ONLY a JSON list of strings."
)

# v1.3 optimizer: the pre-v1.4 prompt — ALLOWS process-discipline notes (category d)
# and omits FORBIDDEN(4). Location facts (FORBIDDEN 1) and effort-capping (FORBIDDEN 2)
# stay banned (those distinguish v1.3 from the discarded v1.2).
OPTIMIZER_SYSTEM_V13 = (
    "You maintain a SHORT list of durable, GENERAL operating notes for working "
    "efficiently in a software repository. You are shown signals of wasted effort "
    "from a recent task trajectory and the current notes. Propose at most {k} NEW "
    "notes that would help FUTURE tasks avoid the same waste.\n\n"
    "GOAL: make each step the agent takes cheaper / more informed. NEVER reduce the "
    "number of attempts the agent is willing to make to reach a correct, test-passing "
    "solution.\n\n"
    "STRICT RULES:\n"
    "- ALLOWED note categories ONLY: (a) the correct command/tool to build/test/run/"
    "lint here, and which forms DON'T work; (b) environment/config facts and gotchas; "
    "(c) infrastructure-error fixes (the durable 'use X to do Y' fact behind a "
    "recurring traceback); (d) step-cheapening workflow habits that do NOT cap effort "
    "(e.g. read a section once before editing it, batch related reads in one pass, "
    "confirm an import path before running a probe).\n"
    "- FORBIDDEN: (1) Do NOT write notes about WHERE specific components, symbols, "
    "subsystems, classes, or tests LIVE (no per-file/per-symbol location facts). "
    "(2) Do NOT write any note that caps iteration, discourages re-running tests, "
    "tells the agent to 'trust' a result, stop early, or otherwise reduce its "
    "willingness to keep iterating. (3) NEVER hint at the solution to any task.\n"
    "- Each note one line, concise, durable, applicable to ANY task in this repo.\n"
    "- If nothing general and durable can be added, return an empty list.\n"
    "Return ONLY a JSON list of strings."
)


def _optimizer_system():
    return OPTIMIZER_SYSTEM_V13 if METHOD == "v1.3" else OPTIMIZER_SYSTEM


def build_optimizer_prompt(signals, state, k=3):
    cur = [n["text"] for n in state["notes"] if not n.get("pruned")]
    env_ev = signals.get("evidence_env_cmds", [])
    tool_ev = signals.get("evidence_tooling", [])
    counts = {k2: v for k2, v in signals.items()
              if not k2.startswith("evidence_")}
    # v1.4: only the Class-A evidence blocks (tooling-discovery and wasted env-
    # discovery commands) are surfaced. The execution-iteration (edit-churn) and
    # repeated-navigation blocks were removed — they solicited generic process-habit
    # notes ("read the section once", "batch reads") that inflated the solve loop
    # without preventing real interface friction.
    blocks = ""
    if tool_ev:
        blocks += ("\n\nTooling-discovery waste (the agent used the wrong command/tool "
                   "form first, then corrected, or hit an infrastructure error — infer "
                   "the durable 'use <tool/command> to do <task>' fact, NOT any fix):\n"
                   + "\n".join(f"  - {c}" for c in tool_ev))
    if env_ev:
        blocks += ("\n\nWasted environment-discovery commands (returned nothing — "
                   "infer the durable env fact that makes them unnecessary):\n"
                   + "\n".join(f"  $ {c}" for c in env_ev))
    if METHOD == "v1.3":
        # v1.3 also surfaces execution-iteration + repeated-navigation evidence, which
        # solicit the process-discipline notes v1.3 allows (and v1.4 banned).
        exec_ev = signals.get("evidence_exec", [])
        nav_ev = signals.get("evidence_nav", [])
        if exec_ev:
            blocks += ("\n\nExecution-iteration waste (edit churn / re-running):\n"
                       + "\n".join(f"  - {c}" for c in exec_ev))
        if nav_ev:
            blocks += ("\n\nRepeated-navigation waste (re-searching / re-reading):\n"
                       + "\n".join(f"  - {c}" for c in nav_ev))
    return (_optimizer_system().format(k=k)
            + "\n\nCurrent notes:\n" + ("\n".join(f"- {c}" for c in cur) or "(none)")
            + "\n\nWaste signal counts from the latest task:\n" + json.dumps(counts)
            + blocks
            + "\n\nReturn the JSON list of new notes:")


def optimize(signals, state, model="agent", k=3, runner=None):
    """STAGE B — paid. Ask the configured optimizer model for new durable notes.
    `runner(prompt, model) -> (str, real_tokens)` performs the LLM call (so the model
    is fully configurable and the call is injectable/testable). Returns
    (notes, tokens) where tokens is the call's REAL token usage when the runner can
    recover it, else a char-based approximation. Parses a JSON list of strings;
    tolerant of extra prose around it."""
    prompt = build_optimizer_prompt(signals, state, k=k)
    if runner is None:
        # no runner wired (offline/dry): propose nothing, cost nothing.
        return [], 0
    raw, real = runner(prompt, model)
    raw = raw or ""
    notes = _parse_notes(raw)
    tokens = real if real is not None else _approx_tokens(prompt) + _approx_tokens(raw)
    return notes[:k], tokens


def _parse_notes(raw):
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        return [str(x).strip() for x in arr if str(x).strip()]
    except Exception:
        return []


# ---- v2.1 anchored-knowledge optimizer ---------------------------------------
# A SEPARATE, parallel optimizer that produces FILE-ANCHORED structural facts about
# the files the agent navigated heavily this task. Kept separate from the global
# optimize() so the global tier is byte-identical to the v1.3 hardened optimizer
# (clean A/B: v2.1 = v1.3 global manifest + anchored). These facts are the location/
# structure knowledge the v1.3 global optimizer had to BAN from the global manifest —
# safe here because they are surfaced ONLY when the agent
# opens that file (no global pollution, no pre-leak of localization).
ANCHORED_OPTIMIZER_SYSTEM = (
    "You record durable, FILE-SPECIFIC structural knowledge about a code repository, "
    "to help FUTURE tasks that edit the SAME files work with less re-exploration. You "
    "are shown files the agent navigated/re-read heavily in a recent task. For each "
    "file worth a note, state ONE concise, durable fact about THAT FILE that a future "
    "editor of it would want to know up front: what it contains, key classes/base "
    "classes and their contract, conventions, extension points, or gotchas.\n\n"
    "STRICT RULES:\n"
    "- The fact must be about the FILE'S STABLE STRUCTURE, true regardless of any "
    "specific task — NOT the solution to the current task, NOT which lines to change, "
    "NOT a fix. If the only thing you can say about a file is task-specific, skip it.\n"
    "- One short line per file. Durable across code changes when possible.\n"
    "- Only include files where a genuine, reusable structural fact exists.\n"
    "Return ONLY a JSON list of objects: [{\"path\": \"<repo-relative path>\", "
    "\"text\": \"<one-line structural fact>\"}].")


def _heavy_files(trajectory_path, min_reads=2, top=4):
    """Repo-relative paths the agent read >= min_reads times (navigation hotspots),
    most-read first. These are where anchored knowledge has the most future payoff."""
    counts = {}
    for name, args, _, _ in load_trajectory(trajectory_path):
        a = args if isinstance(args, dict) else {}
        if name == "view" or "read_file" in (name or ""):
            p = (a.get("path") or "").strip()
            if p:
                counts[p] = counts.get(p, 0) + 1
    heavy = sorted((p for p, c in counts.items() if c >= min_reads),
                   key=lambda p: -counts[p])[:top]
    return heavy


def optimize_anchored(trajectory_path, state, model="agent", k=4, runner=None):
    """STAGE B (anchored) — paid, parallel to optimize(). Ask the model for durable
    file-structural facts about the navigation-hotspot files. Returns (items, tokens)
    where items = [{path, text, tag}]. No-op (0 cost) when no heavy files or no runner."""
    if runner is None:
        return [], 0
    heavy = _heavy_files(trajectory_path)
    if not heavy:
        return [], 0
    existing = {p: [n["text"] for n in b] for p, b in (state.get("anchored") or {}).items()}
    prompt = (ANCHORED_OPTIMIZER_SYSTEM
              + "\n\nFiles the agent navigated/re-read heavily this task:\n"
              + "\n".join(f"- {p}" for p in heavy)
              + "\n\nExisting anchored facts (do not duplicate):\n"
              + (json.dumps(existing) if existing else "(none)")
              + "\n\nReturn the JSON list of {path, text}:")
    raw, real = runner(prompt, model)
    items = _parse_anchored(raw or "")
    # keep only facts whose path is one we actually navigated (no hallucinated paths)
    heavy_set = set(heavy)
    items = [{"path": it["path"], "text": it["text"], "tag": "uncertain"}
             for it in items if it.get("path") in heavy_set and it.get("text")][:k]
    tokens = real if real is not None else _approx_tokens(prompt) + _approx_tokens(raw)
    return items, tokens


def _parse_anchored(raw):
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        out = []
        for x in arr:
            if isinstance(x, dict) and x.get("path") and x.get("text"):
                out.append({"path": str(x["path"]).strip(),
                            "text": str(x["text"]).strip()})
        return out
    except Exception:
        return []


def _approx_tokens(text):
    return max(1, len(text) // 4)


def _runner_effective_or_approx(runner, prompt, raw, real):
    """Use recovered usage, then a runner's model-priced estimate, then legacy ×5."""
    if real is not None:
        return real
    estimator = getattr(runner, "estimate_effective", None)
    if callable(estimator):
        try:
            return estimator(prompt, raw)
        except Exception:
            pass
    return _approx_tokens(prompt) + 5 * _approx_tokens(raw)


# ---- v2.2 subsystem fix-map optimizer ----------------------------------------
# v2.1's anchored tier failed on realistic streams: it anchored at FILE granularity
# (cross-task file-revisit ~12-33%) and learned from within-task re-reads, so its
# delivery surface was near-zero. v2.2 fixes BOTH: it captures from the RESOLVED
# patch's EDITED files (the proven solve sites), anchors at SUBSYSTEM (dir) level —
# where revisit is 79% on xarray — and records only SYMBOL-level structure (no line
# numbers, no defect), so it survives the median ~69-line churn between visits
# (measured symbol-survival = 100% median). Delivery is verify-on-inject (the reader
# drops any mapped symbol that no longer exists), so drift can't mislead.
FIXMAP_OPTIMIZER_SYSTEM = (
    "You record a durable SUBSYSTEM MAP for a code repository, to help FUTURE tasks "
    "that edit the SAME directory navigate to the fix site faster. You are shown the "
    "directory of a just-RESOLVED task and the functions/classes that were edited. For "
    "each, state ONE concise, durable line: the symbol name and its responsibility, "
    "and the test file that validates it.\n\n"
    "STRICT RULES:\n"
    "- Anchor on the SYMBOL (function/class name) and its STABLE ROLE — NEVER line "
    "numbers, NEVER the specific bug or fix, NEVER what to change. A future reader "
    "must not be able to reconstruct this task's solution from your map.\n"
    "- One short line per symbol: `symbol — role (validated by tests/<file>)`.\n"
    "- Skip anything that is only true for this one defect.\n"
    "Return ONLY a JSON list of {\"path\": \"<dir>\", \"text\": \"<line>\"}.")


def _edited_symbols(patch_path):
    """From a unified diff, map each edited .py file to the def/class symbols whose
    bodies were touched. Returns {dir: set(symbols)}; line numbers are NOT retained."""
    dirs = {}
    cur, hunk_syms = None, set()
    if not os.path.isfile(patch_path):
        return dirs
    sym_re = re.compile(r"(?:def|class)\s+(\w+)")
    for ln in open(patch_path, errors="replace"):
        if ln.startswith("+++ b/"):
            cur = ln[6:].strip()
        elif ln.startswith("@@") and cur and cur.endswith(".py") and "test" not in cur:
            m = sym_re.search(ln)  # hunk header often carries the enclosing def/class
            if m:
                dirs.setdefault(os.path.dirname(cur), set()).add(m.group(1))
        elif (ln.startswith("+") or ln.startswith("-")) and cur and cur.endswith(".py") \
                and "test" not in cur:
            m = sym_re.search(ln)
            if m:
                dirs.setdefault(os.path.dirname(cur), set()).add(m.group(1))
    return dirs


def optimize_fixmap(patch_path, state, model="agent", k=4, runner=None):
    """v2.2 — paid, fires only on RESOLVED tasks. Build symbol-level subsystem facts
    from the edited dirs. Returns (items, tokens); items=[{path=dir, text, tag}]."""
    if runner is None:
        return [], 0
    dirs = _edited_symbols(patch_path)
    if not dirs:
        return [], 0
    existing = {d: [n["text"] for n in b] for d, b in (state.get("anchored") or {}).items()}
    desc = "\n".join(f"- {d}: {', '.join(sorted(s))}" for d, s in dirs.items() if s)
    if not desc.strip():
        return [], 0
    prompt = (FIXMAP_OPTIMIZER_SYSTEM
              + "\n\nResolved task edited these subsystems (dir: symbols):\n" + desc
              + "\n\nExisting subsystem maps (do not duplicate):\n"
              + (json.dumps(existing) if existing else "(none)")
              + "\n\nReturn the JSON list of {path, text}:")
    raw, real = runner(prompt, model)
    items = _parse_anchored(raw or "")
    valid_dirs = set(dirs)
    items = [{"path": it["path"], "text": it["text"], "tag": "uncertain"}
             for it in items if it.get("path") in valid_dirs and it.get("text")][:k]
    tokens = real if real is not None else _approx_tokens(prompt) + _approx_tokens(raw)
    return items, tokens


# ---- optional LLM-detector tier ----------------------------------------------
# Answers the design question "must the detector use a fixed vocabulary?". The free
# heuristic detect()/trigger() runs every task (cheap, and it also flags integrity /
# external-fetch regardless). This tier is an LLM that reads a LEAKAGE-SAFE PROCESS
# digest of one trajectory and decides, open-endedly, whether there is a recurring,
# task-agnostic inefficiency worth a durable note — without being told the signal
# types in advance. Because an LLM call per task would reintroduce the very overhead
# the gate exists to remove, it is meant to be SAMPLED (every Nth task, or only when
# the free detector is ambiguous), never run unconditionally.

def trajectory_summary(trajectory_path, max_cmds=25):
    """A compact, LEAKAGE-SAFE digest of an agent's PROCESS (not its solution): the
    bash commands it ran, what it searched for, which files it opened (basenames),
    and infrastructure-error snippets — deliberately EXCLUDING the patch/diff, edit
    contents, view contents, and the issue text, so it cannot encode the fix."""
    calls = load_trajectory(trajectory_path)
    lines, n_view, n_edit = [], 0, 0
    for name, args, res, ok in calls:
        if name == "bash":
            c = _norm_cmd(args.get("command", ""))
            err = ERROR_MARKER_RE.search(res or "")
            tag = f"  [err: {err.group(0)[:40]}]" if err else ""
            if c:
                lines.append(f"$ {c[:120]}{tag}")
        elif name in ("grep", "glob"):
            q = (args.get("pattern") or "").strip()
            if q:
                lines.append(f"search {name}: {q[:60]}")
        elif name == "view":
            n_view += 1
            p = _basename(args.get("path") or "")
            if p:
                lines.append(f"view {p}")
        elif name in ("edit", "str_replace", "create"):
            n_edit += 1
        elif name in ("web_fetch", "web_search") or (name or "").startswith("github"):
            lines.append(f"EXTERNAL {name}")
    # cap to keep the digest cheap and process-focused
    if len(lines) > max_cmds:
        lines = lines[:max_cmds] + [f"... (+{len(lines)-max_cmds} more actions)"]
    head = f"({len(calls)} tool calls; {n_view} views, {n_edit} edits)"
    return head + "\n" + "\n".join(lines)


LLM_DETECTOR_SYSTEM = (
    "You audit an AI coding agent's PROCESS trace for one task in a repository it "
    "will face MORE tasks in. Decide whether the trace shows a RECURRING, "
    "TASK-AGNOSTIC inefficiency that a short durable repo note would prevent next "
    "time — e.g. discovering the right build/test/run command the hard way, "
    "re-finding where code lives, or re-probing the environment.\n\n"
    "Do NOT infer or describe the task's solution or which lines to change. Judge "
    "only the agent's workflow efficiency.\n"
    "Return ONLY a JSON object: {\"worth_optimizing\": bool, \"kind\": \"<short "
    "label e.g. tooling-discovery|navigation|env|none>\", \"evidence\": [\"<short "
    "leakage-safe observations the optimizer can turn into a durable note>\"]}. "
    "If the workflow was already efficient, return worth_optimizing=false, kind="
    "\"none\", evidence=[]."
)


def llm_detect(trajectory_path, runner, model="agent"):
    """SAMPLED LLM detector. Returns (verdict_dict, tokens). verdict_dict has keys
    worth_optimizing(bool), kind(str), evidence(list[str]). On any failure returns a
    safe negative verdict. The evidence can be fed straight into the optimizer via
    signals['evidence_tooling']."""
    if runner is None:
        return {"worth_optimizing": False, "kind": "none", "evidence": []}, 0
    summary = trajectory_summary(trajectory_path)
    prompt = (LLM_DETECTOR_SYSTEM + "\n\nProcess trace:\n" + summary
              + "\n\nReturn the JSON verdict:")
    raw, real = runner(prompt, model)
    raw = raw or ""
    tokens = real if real is not None else _approx_tokens(prompt) + _approx_tokens(raw)
    verdict = {"worth_optimizing": False, "kind": "none", "evidence": []}
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            verdict["worth_optimizing"] = bool(obj.get("worth_optimizing"))
            verdict["kind"] = str(obj.get("kind", "none"))[:40]
            ev = obj.get("evidence", [])
            verdict["evidence"] = [str(x)[:160] for x in ev][:5] if isinstance(ev, list) else []
        except Exception:
            pass
    return verdict, tokens


# ---- v4.1 spiral trace-judge (live abort decision; same LLM-detector machinery) ----
# The Phase-1.5 abort decision was step-count-gated (resolved-turn envelope). This is the
# TRACE-CONTENT tier: an LLM reads a leakage-safe digest of the agent's RECENT actions
# (built by spiral.SpiralState.recent_digest — commands / searches / edited basenames /
# error SIGNATURES only, never fix content) and judges whether the trajectory is an
# unrecoverable loop or still making progress. It is the same sampled-LLM pattern as
# llm_detect (leakage-safe process digest -> open-ended verdict), here wired to the live
# spiral guard rather than the between-task optimizer. Meant to be called RARELY — only
# when the free heuristic (spiral.is_stuck) already flagged a candidate stuck state.
STUCK_JUDGE_SYSTEM = (
    "You monitor an AI coding agent that is partway through ONE task in a software repo. "
    "A cheap heuristic flagged its recent process as a possible UNPRODUCTIVE LOOP. Read "
    "the leakage-safe digest of its RECENT actions and judge whether it is genuinely "
    "STUCK — repeating the same kind of edit/test/failure with no progress toward green "
    "tests, unlikely to recover if it keeps going the same way — versus still making "
    "real progress (changing failures, narrowing the problem, exploring new hypotheses).\n\n"
    "Judge ONLY the workflow from the digest; do NOT infer the task's solution or which "
    "lines to change. Favor SPARING (stuck=false) when there is any sign of progress; "
    "only return stuck=true with high confidence for a clear, repetitive dead-end.\n"
    "Return ONLY a JSON object: {\"stuck\": bool, \"confidence\": <0.0-1.0>, "
    "\"reason\": \"<short leakage-safe justification>\"}."
)


def llm_stuck_judge(digest, runner, model="agent"):
    """SAMPLED LLM trace-judge for the live spiral guard. `digest` is a leakage-safe
    recent-process string (spiral.SpiralState.recent_digest()). Returns a verdict dict
    {stuck: bool|None, confidence: float, reason: str, _tokens: number}; stuck=None on any
    failure (the caller treats None/ambiguous by falling back to the envelope gate).
    `_tokens` is the REAL token cost of this paid judge call (input+output+cache, from the
    runner's usage block; a char-based approximation if the runner couldn't recover it) so
    the harness can fold mechanism-internal LLM cost into the amortized task cost. 0 when no
    call was made."""
    if runner is None or not digest:
        return {"stuck": None, "confidence": 0.0, "reason": "no-runner", "_tokens": 0}
    prompt = (STUCK_JUDGE_SYSTEM + "\n\nRecent process digest:\n" + digest
              + "\n\nReturn the JSON verdict:")
    try:
        raw, _real = runner(prompt, model)
    except Exception as e:
        return {"stuck": None, "confidence": 0.0,
                "reason": f"runner-error:{e}"[:60], "_tokens": 0}
    raw = raw or ""
    # EFFECTIVE (cache-aware) cost when the runner recovered usage; else approximate on the
    # SAME basis: an uncached one-shot -> prompt is fresh input (weight 1), reply is output
    # (weight 5). Keeps mechanism cost comparable to exec's cache-aware effective tokens.
    _tokens = _runner_effective_or_approx(runner, prompt, raw, _real)
    verdict = {"stuck": None, "confidence": 0.0, "reason": "", "_tokens": _tokens}
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            verdict["stuck"] = (bool(obj["stuck"]) if "stuck" in obj
                                and obj["stuck"] is not None else None)
            verdict["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0) or 0)))
            verdict["reason"] = str(obj.get("reason", ""))[:160]
        except Exception:
            pass
    return verdict


# ---- v6.1 LLM-targeted rung-1 NUDGE writer -----------------------------------
# The v4.3 rung-1 nudge (spiral.nudge_text) is leakage-safe but GENERIC: it only
# splices counts into a fixed template ("N edits across M files, same failure recurred
# K times -> try something different"). This writer makes the redirect TARGETED: a
# sampled LLM reads the SAME leakage-safe recent-process digest the stuck-judge sees
# (spiral.SpiralState.recent_digest — commands / searches / edited basenames / error
# SIGNATURES only) and names the specific repetitive pattern + a materially different
# next step. It carries NO external knowledge (no issue text, no fix content, no gold
# pointer), so it stays inside the effort-capping ban exactly like nudge_text: it may
# only REDIRECT effort, never stop it. Falls back to the template on any failure.
NUDGE_WRITER_SYSTEM = (
    "An AI coding agent is stuck in a REPETITIVE, UNPRODUCTIVE loop partway through one "
    "task in a software repo. You are given a leakage-safe digest of its RECENT ACTIONS "
    "only (tool names, shell commands, searched patterns, edited file BASENAMES, and "
    "error SIGNATURES — never file contents or the task description). Write a SHORT "
    "redirect (2-4 sentences) addressed to the agent that: (1) names the specific "
    "repetitive pattern you actually see in the digest (e.g. re-editing the same file, "
    "re-running the same test to the same failure); (2) proposes a MATERIALLY DIFFERENT "
    "next step (a different file/root-cause to consider, re-reading the failing test's "
    "expectation, verifying a new hypothesis before editing again).\n"
    "HARD CONSTRAINTS: Do NOT tell it to stop, give up, or reduce effort — only redirect. "
    "Do NOT infer the task's solution, name specific line numbers to change, or reference "
    "any code/identifier you cannot see in the digest. Output ONLY the redirect as plain "
    "text — no preamble, no JSON, no headings."
)


def llm_nudge_writer(digest, runner, model="agent"):
    """SAMPLED LLM writer for the v6.1 targeted rung-1 nudge. `digest` is the same
    leakage-safe recent-process string fed to llm_stuck_judge
    (spiral.SpiralState.recent_digest()). Returns {"text": str, "_tokens": number}: a
    targeted, leakage-safe redirect and the REAL cache-aware effective token cost of the
    call (folded into amortized task cost by the harness, same basis as the judge). On any
    failure returns {"text": "", "_tokens": 0} so the caller falls back to the generic
    spiral.nudge_text template (resolve-safe zero-LLM path)."""
    if runner is None or not digest:
        return {"text": "", "_tokens": 0}
    prompt = (NUDGE_WRITER_SYSTEM + "\n\nRecent process digest:\n" + digest
              + "\n\nWrite the redirect:")
    try:
        raw, _real = runner(prompt, model)
    except Exception:
        return {"text": "", "_tokens": 0}
    raw = (raw or "").strip()
    if not raw:
        return {"text": "", "_tokens": 0}
    # EFFECTIVE (cache-aware) cost when the runner recovered usage; else approximate on the
    # SAME model-priced basis as llm_stuck_judge.
    _tokens = _runner_effective_or_approx(runner, prompt, raw, _real)
    return {"text": raw[:1200], "_tokens": _tokens}
