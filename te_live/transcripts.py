"""Read a host's own session log in the shapes the mechanisms already consume.

The mechanisms read rollouts through one seam (`tokeneconomizer.trace`), which knows the three
streams the harness captured: Claude Code `stream-json`, the Copilot CLI's events, and
`codex exec --json`. A live session leaves a different file behind, and this module bridges it
rather than teaching every mechanism a fourth schema:

  * Claude Code's interactive transcript uses the same `message.content[]` envelope as
    `stream-json`, so `trace.load_trajectory` reads it unchanged. What differs is usage: the
    transcript writes ONE ENTRY PER CONTENT BLOCK, each repeating its message's `usage`, so
    summing entries over-counts by the number of blocks (measured locally: two thirds of
    assistant message ids repeat, up to 12 times). `claude_session_usage` keys by message id.
    Subagents write their own files under `<session>/subagents/`, and their tool calls and
    tokens belong to the session that spawned them.

  * Codex's rollout (`~/.codex/sessions/.../rollout-*.jsonl`) wraps the same item fields the
    exec stream carries (`command`, `aggregated_output`, `exit_code`, `changes`) in
    `event_msg/item_completed` records with PascalCase item types, an argv list for the command
    and a path-keyed map for changes. `codex_rollout_to_exec` rewrites it into exec-stream lines,
    after which `trace.load_trajectory`, the injection layers and the spiral parsers read it as
    they read a harness transcript. Usage comes from its `token_count` events and is converted
    exactly as `te_harness.codex.row_usage` converts the exec stream's, because Codex's
    `input_tokens` is the GRAND input total including both cache fields.

    A compressed rollout (`.jsonl.zst`) cannot be read with the standard library; it is reported
    as unreadable rather than half-read.
"""
import glob
import json
import os
import re
import shlex

_COPILOT_EXIT_RE = re.compile(r"<shellId: [^>]*?completed with exit code (-?\d+)>")
_CLAUDE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "output_tokens")


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line
    except OSError:
        return


def claude_subagent_files(transcript_path):
    """Subagent transcripts of a Claude Code session: `<dir>/<session-id>/subagents/*.jsonl`."""
    if not transcript_path:
        return []
    stem = os.path.splitext(transcript_path)[0]
    return sorted(glob.glob(os.path.join(stem, "subagents", "*.jsonl")))


def claude_session_usage(transcript_path, include_subagents=True):
    """Token breakdown of a Claude Code session, de-duplicated by assistant message id.

    Returns the four breakdown fields `pricing.effective_tokens` reads, plus `model` (the most
    used one), `requests` (distinct assistant messages carrying usage) and `files` read."""
    files = [transcript_path] + (claude_subagent_files(transcript_path)
                                 if include_subagents else [])
    by_id, models, anon = {}, {}, 0
    for path in files:
        for line in _lines(path):
            if '"usage"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message") or {}
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            mid = msg.get("id")
            if not mid:
                anon += 1
                mid = f"anon-{path}-{anon}"
            # Later entries of the same message carry the final counts; keep the last.
            by_id[mid] = {k: int(usage.get(k) or 0) for k in _CLAUDE_KEYS}
            model = msg.get("model")
            if model:
                models[model] = models.get(model, 0) + 1
    out = {k: sum(u[k] for u in by_id.values()) for k in _CLAUDE_KEYS}
    out["requests"] = len(by_id)
    out["model"] = max(models, key=models.get) if models else None
    out["files"] = len([f for f in files if os.path.exists(f)])
    return out


def claude_tool_usage(transcript_path, include_subagents=True):
    """{tool: calls} over a session and, by default, its subagents -- a tool a subagent used is
    in use, and denying it would take it from the subagent too."""
    from tokeneconomizer import trace
    counts = {}
    files = [transcript_path] + (claude_subagent_files(transcript_path)
                                 if include_subagents else [])
    for path in files:
        for name, n in trace.tool_usage(path).items():
            counts[name] = counts.get(name, 0) + n
    return counts


def claude_deferred_tools(transcript_path):
    """Tool names the session listed as deferred (`deferred_tools_delta` attachments).

    Deferred tools cost one listing line each, not a schema, so they matter little to the bill;
    they are still part of what the session could call, so the untool catalog includes them."""
    names = set()
    for line in _lines(transcript_path):
        if '"deferred_tools_delta"' not in line:
            continue
        try:
            att = json.loads(line).get("attachment") or {}
        except ValueError:
            continue
        names.update(n for n in (att.get("addedNames") or []) if isinstance(n, str))
        names.difference_update(n for n in (att.get("removedNames") or []) if isinstance(n, str))
    return names


# ---- Codex ---------------------------------------------------------------------------
_ROLLOUT_ITEM_TYPES = {
    "CommandExecution": "command_execution",
    "FileChange": "file_change",
    "AgentMessage": "agent_message",
    "ImageView": "view_image",
    "Extension": "web_search",
    "McpToolCall": "mcp_tool_call",
    "CollabToolCall": "collab_tool_call",
}


def _command_text(command):
    if isinstance(command, list):
        return shlex.join(str(c) for c in command)
    return str(command or "")


def _rollout_item_to_exec(item):
    """One rollout item -> the `codex exec --json` item shape, or None to skip it."""
    kind = _ROLLOUT_ITEM_TYPES.get(item.get("type"), None)
    if kind is None:
        return None
    out = {"type": kind, "id": item.get("id")}
    if kind == "command_execution":
        out.update(command=_command_text(item.get("command")),
                   aggregated_output=item.get("aggregated_output")
                   or ((item.get("stdout") or "") + (item.get("stderr") or "")),
                   exit_code=item.get("exit_code"),
                   status=str(item.get("status") or "").lower() or "completed")
    elif kind == "file_change":
        changes = item.get("changes")
        if isinstance(changes, dict):
            changes = [dict({"path": p}, **({"kind": (v or {}).get("type")}
                                            if isinstance(v, dict) else {}))
                       for p, v in changes.items()]
        out.update(changes=changes or [],
                   status=str(item.get("status") or "").lower() or "completed")
    elif kind == "agent_message":
        content = item.get("content")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        out.update(text=content or "")
    else:
        out.update({k: v for k, v in item.items() if k not in ("type", "id")})
    return out


def codex_rollout_to_exec(rollout_path, out_path):
    """Rewrite a Codex rollout into `codex exec --json` lines at `out_path`.

    Returns the number of items written, or None when the rollout cannot be read (missing, or
    compressed). The thread id rides on a leading `thread.started` line, as in the exec stream."""
    if not rollout_path or rollout_path.endswith(".zst") or not os.path.exists(rollout_path):
        return None
    n = 0
    thread = None
    with open(out_path, "w", encoding="utf-8") as out:
        for line in _lines(rollout_path):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if ev.get("type") == "session_meta" and thread is None:
                thread = payload.get("id") or payload.get("session_id")
                if thread:
                    out.write(json.dumps({"type": "thread.started", "thread_id": thread}) + "\n")
                continue
            if ev.get("type") != "event_msg" or payload.get("type") != "item_completed":
                continue
            item = _rollout_item_to_exec(payload.get("item") or {})
            if item is None:
                continue
            out.write(json.dumps({"type": "item.completed", "item": item}) + "\n")
            n += 1
    return n


def codex_capability_trace(rollout_path, out_path):
    """The exec-stream lines `mechanisms.codex_untool.inspect_usage` reads, for a rollout.

    `codex_rollout_to_exec` keeps the items the other mechanisms read; capability evidence
    also needs the model's own tool calls. In code mode every call is one `exec` custom tool
    whose input calls `tools.<name>(...)` -- `tools.view_image(`, `tools.spawn_agent(` -- and
    those ride in `response_item` records, which are added here as `custom_tool_call` /
    `function_call` items, the shape `codex_untool._item_capabilities` parses. Returns the number
    of items written, or None when the rollout cannot be read."""
    n = codex_rollout_to_exec(rollout_path, out_path)
    if n is None:
        return None
    with open(out_path, "a", encoding="utf-8") as out:
        for line in _lines(rollout_path):
            if '"response_item"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if ev.get("type") != "response_item" or p.get("type") not in (
                    "custom_tool_call", "function_call"):
                continue
            item = {"type": p.get("type"), "name": p.get("name"),
                    "id": p.get("call_id") or p.get("id"),
                    "status": str(p.get("status") or "completed").lower()}
            item["input" if p.get("type") == "custom_tool_call" else "arguments"] = (
                p.get("input") if p.get("type") == "custom_tool_call" else p.get("arguments"))
            out.write(json.dumps({"type": "item.completed", "item": item}) + "\n")
            n += 1
    return n


def codex_session_meta(rollout_path):
    """{cwd, model, cli_version} from a rollout's session_meta / turn_context records."""
    meta = {}
    for line in _lines(rollout_path):
        if '"session_meta"' not in line and '"turn_context"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if ev.get("type") == "session_meta":
            meta.setdefault("cwd", p.get("cwd"))
            meta.setdefault("cli_version", p.get("cli_version"))
        elif ev.get("type") == "turn_context" and p.get("model"):
            meta["model"] = p.get("model")
    return meta


def codex_session_usage(rollout_path):
    """Token breakdown of a Codex session from its last cumulative `token_count`.

    Converted as `te_harness.codex.row_usage` converts the exec stream: fresh input is the grand
    input total less both cache fields, and reasoning tokens are added to output."""
    total = None
    requests = 0
    for line in _lines(rollout_path):
        if '"token_count"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if p.get("type") != "token_count":
            continue
        info = p.get("info") or {}
        if info.get("total_token_usage"):
            total = info["total_token_usage"]
            requests += 1
    meta = codex_session_meta(rollout_path)
    if not total:
        return {"input_tokens": None, "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None, "output_tokens": None,
                "requests": 0, "model": meta.get("model")}
    inp = int(total.get("input_tokens") or 0)
    cached = int(total.get("cached_input_tokens") or 0)
    cw = int(total.get("cache_write_input_tokens") or 0)
    reasoning = int(total.get("reasoning_output_tokens") or 0)
    return {"input_tokens": max(0, inp - cached - cw),
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": cw,
            "output_tokens": int(total.get("output_tokens") or 0) + reasoning,
            "reasoning_output_tokens": reasoning,
            "requests": requests, "model": meta.get("model")}


# ---- Pi ------------------------------------------------------------------------------
# Pi (earendil-works/pi) stores a session as a TREE of JSONL entries linked by `id` /
# `parentId`; the conversation is the path from the current leaf back to the root, and an
# abandoned branch stays in the file. Assistant messages carry `toolCall` blocks
# ({id, name, arguments}) and their own usage; tool results are `toolResult` messages with
# `isError`. `pi_session_to_claude` writes the ACTIVE branch as the Claude Code envelope the
# trace seam already reads, so every mechanism reads a Pi session without a Pi branch.
#
# Usage, per pi-ai's own type: `input` excludes the cache fields, `output` already includes
# reasoning, and `cacheWrite1h` is a subset of `cacheWrite` -- so the mapping to the ledger's
# four fields is direct.

def _pi_entries(path):
    out = []
    for line in _lines(path):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def pi_session_header(path):
    """The `session` header line ({id, cwd, version, ...}), {} if absent."""
    for ev in _pi_entries(path)[:1]:
        if ev.get("type") == "session":
            return ev
    return {}


def pi_active_branch(entries):
    """Entries on the path from the last entry (the leaf) back to the root, in order."""
    by_id = {e.get("id"): e for e in entries if e.get("id")}
    tree = [e for e in entries if e.get("id")]
    if not tree:
        return []
    path, cur, seen = [], tree[-1], set()
    while cur is not None and cur.get("id") not in seen:
        seen.add(cur.get("id"))
        path.append(cur)
        cur = by_id.get(cur.get("parentId"))
    return list(reversed(path))


def _pi_usage(u):
    if not isinstance(u, dict):
        return None
    return {"input_tokens": int(u.get("input") or 0),
            "cache_read_input_tokens": int(u.get("cacheRead") or 0),
            "cache_creation_input_tokens": int(u.get("cacheWrite") or 0),
            "output_tokens": int(u.get("output") or 0),
            "cost_usd": float((u.get("cost") or {}).get("total") or 0)}


def _pi_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def pi_session_to_claude(path, out_path):
    """Write a Pi session's active branch as Claude Code transcript lines.

    Returns the number of tool calls written, or None when the file is missing."""
    if not path or not os.path.exists(path):
        return None
    n = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for e in pi_active_branch(_pi_entries(path)):
            if e.get("type") != "message":
                continue
            m = e.get("message") or {}
            role = m.get("role")
            if role == "user":
                out.write(json.dumps({"type": "user", "message": {
                    "role": "user", "content": _pi_text(m.get("content"))}}) + "\n")
            elif role == "assistant":
                blocks = []
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        blocks.append({"type": "tool_use", "id": b.get("id"),
                                       "name": b.get("name"), "input": b.get("arguments") or {}})
                        n += 1
                    elif isinstance(b, dict) and b.get("type") == "text":
                        blocks.append({"type": "text", "text": b.get("text", "")})
                msg = {"id": e.get("id"), "role": "assistant", "content": blocks,
                       "model": m.get("model")}
                usage = _pi_usage(m.get("usage"))
                if usage:
                    msg["usage"] = {k: v for k, v in usage.items() if k != "cost_usd"}
                out.write(json.dumps({"type": "assistant", "message": msg}) + "\n")
            elif role == "toolResult":
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": m.get("toolCallId"),
                    "content": _pi_text(m.get("content")),
                    "is_error": bool(m.get("isError"))}]}}) + "\n")
    return n


def pi_session_usage(path):
    """Token breakdown of a Pi session's active branch, plus Pi's own cost estimate."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    cost, requests, models = 0.0, 0, {}
    for e in pi_active_branch(_pi_entries(path)):
        u = None
        if e.get("type") == "message" and (e.get("message") or {}).get("role") == "assistant":
            m = e["message"]
            u = _pi_usage(m.get("usage"))
            if m.get("model"):
                models[m["model"]] = models.get(m["model"], 0) + 1
        elif e.get("type") in ("usage", "compaction", "branch_summary"):
            u = _pi_usage(e.get("usage"))
        if u:
            requests += 1
            cost += u.pop("cost_usd")
            for k in totals:
                totals[k] += u[k]
    totals.update(requests=requests, cost_usd_vendor=round(cost, 6),
                  model=max(models, key=models.get) if models else None)
    return totals


# ---- GitHub Copilot CLI ---------------------------------------------------------------
# Copilot writes each session to `<COPILOT_HOME>/session-state/<id>/events.jsonl`: the same
# `tool.execution_start` / `tool.execution_complete` event stream `copilot --output-format json`
# prints, which `trace.load_trajectory` already reads, so no rewrite is needed. Usage is in the
# `session.shutdown` event, per model:
#     modelMetrics[model].usage = {inputTokens, outputTokens, cacheReadTokens,
#                                  cacheWriteTokens, reasoningTokens}
# `inputTokens` is the GRAND input total: against a scripted endpoint that reported
# prompt_tokens=1000 with cached_tokens=600 on each of 4 requests, it recorded inputTokens=4000
# and cacheReadTokens=2400 (1.0.83). Fresh input is therefore input less both cache fields, as
# `sum_openai_usage` and the Codex reader convert theirs. `outputTokens` is the provider's
# completion count, which already includes reasoning, so reasoning is not added again.
# A session killed before it shut down has no usage; it is reported as unpriced.

def _copilot_events(path):
    for line in _lines(path):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            yield ev


def copilot_session_meta(path):
    """{cwd, model, cli_version, session_id} from a Copilot session's `session.start`."""
    for ev in _copilot_events(path):
        if ev.get("type") == "session.start":
            d = ev.get("data") or {}
            return {"cwd": (d.get("context") or {}).get("cwd"), "model": d.get("selectedModel"),
                    "cli_version": d.get("copilotVersion"), "session_id": d.get("sessionId")}
    return {}


def copilot_session_usage(path):
    """Token breakdown of a Copilot session from its `session.shutdown` events."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    requests, models, seen, tool_defs = 0, {}, False, None
    for ev in _copilot_events(path):
        if ev.get("type") != "session.shutdown":
            continue
        d = ev.get("data") or {}
        for model, m in (d.get("modelMetrics") or {}).items():
            u = (m or {}).get("usage") or {}
            inp = int(u.get("inputTokens") or 0)
            cr, cw = int(u.get("cacheReadTokens") or 0), int(u.get("cacheWriteTokens") or 0)
            totals["input_tokens"] += max(0, inp - cr - cw)
            totals["cache_read_input_tokens"] += cr
            totals["cache_creation_input_tokens"] += cw
            totals["output_tokens"] += int(u.get("outputTokens") or 0)
            n = int(((m or {}).get("requests") or {}).get("count") or 0)
            requests += n
            models[model] = models.get(model, 0) + n
            seen = True
        if d.get("toolDefinitionsTokens") is not None:
            tool_defs = int(d["toolDefinitionsTokens"])
    if not seen:
        out = {k: None for k in totals}
        out.update(requests=0, model=copilot_session_meta(path).get("model"))
        return out
    totals.update(requests=requests, model=max(models, key=models.get) if models else None,
                  tool_definitions_tokens=tool_defs)
    return totals


def copilot_turns(path):
    """Prompt-turns of a Copilot session: [[(name, args, result, exit_code)]], one list per
    user message, tool calls paired start-to-complete by call id."""
    turns, cur, pend = [], None, {}
    for ev in _copilot_events(path):
        t, d = ev.get("type"), ev.get("data") or {}
        if t == "user.message":
            if cur:
                turns.append(cur)
            cur, pend = [], {}
        elif t == "tool.execution_start" and cur is not None:
            pend[d.get("toolCallId")] = (str(d.get("toolName") or ""),
                                         d.get("arguments") if isinstance(d.get("arguments"),
                                                                          dict) else {})
        elif t == "tool.execution_complete" and cur is not None:
            name, args = pend.pop(d.get("toolCallId"), (None, None))
            if name is None:
                continue
            res = d.get("result") or {}
            text = res.get("content") if isinstance(res, dict) else str(res)
            text = text if isinstance(text, str) else json.dumps(text)
            if not d.get("success", True) and isinstance(d.get("error"), (str, dict)):
                text = (text or "") + "\n" + json.dumps(d.get("error"))
            m = _COPILOT_EXIT_RE.search((text or "")[-400:])
            cur.append((name, args, text or "", int(m.group(1)) if m else None))
    if cur:
        turns.append(cur)
    return turns


def copilot_mcp_servers(env=None):
    """MCP server names a Copilot session can load: the built-in GitHub server plus every
    server in the user's `mcp-config.json`. Tool names are `<server>-<tool>`."""
    env = os.environ if env is None else env
    home = env.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
    names = {"github-mcp-server"}
    try:
        with open(os.path.join(home, "mcp-config.json"), encoding="utf-8") as fh:
            names.update((json.load(fh).get("mcpServers") or {}).keys())
    except (OSError, ValueError, AttributeError):
        pass
    return names


def mcp_server_of(tool, servers):
    """The server a Copilot tool name belongs to (longest matching `<server>-` prefix)."""
    best = None
    for s in servers:
        if tool.startswith(s + "-") and (best is None or len(s) > len(best)):
            best = s
    return best
