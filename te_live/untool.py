"""Applying an untool proposal on each host, and measuring the request with and without it.

Evidence untooling decides WHICH tools to drop (`mechanisms.tooling.ToolingStage`, driven by
`live.fold`). On the benchmark the harness applied the decision at launch. A plugin launches
nothing, so on each host the lever is a per-repository settings file the agent reads at session
start, and the unit of removal is whatever that file can express:

  * Claude Code -- a TOOL. A bare-name `permissions.deny` rule removes the tool from the
    session's tool list, not merely refusing the call. Verified on 2.1.286: three deny rules
    took a session from 30 tools to 27, the same list `--disallowed-tools` produced, and the
    same rules in a project's `.claude/settings.local.json` took it to 23.
  * Codex -- a CAPABILITY package (`mechanisms.codex_untool`), written as config toggles into
    the project's `.codex/config.toml`. Verified on 0.153.1 with `codex debug prompt-input`:
    the collaboration, goals and image toggles take effect from a project-local file -- but
    ONLY in a trusted project; an untrusted project's `.codex/` is ignored, so `apply` checks
    trust and says so. The `skills` package is not
    offered: its toggle (`[[skills.config]] enabled = false` per bundled skill) takes effect in
    the user-level config only, and a per-repository decision does not belong there.
  * Copilot CLI -- an MCP SERVER, through `disabledMcpServers` in the repository's
    `.github/copilot/settings.local.json`. Verified on 1.0.83: listing the built-in
    `github-mcp-server` removes its tools from the request -- but, as on Codex, ONLY in a
    trusted folder (Copilot's `trustedFolders`, which its first-run prompt fills): the same
    file in an untrusted directory changes nothing.
    `apply` checks trust and confirms the effect with a local capture. Copilot has no persistent per-tool filter (`--excluded-tools` is a launch
    flag; `permissions.deny` refuses calls but leaves the schema in the request), so a server
    is proposed only when every one of its tools has gone unused.

Rules common to all three:

  * Only the user's approval (`te untool apply`), or `untool: auto`, writes the file, and only
    from the NEXT session: tools are fixed at session start, and changing them mid-session would
    re-write the cached prompt prefix.
  * The writer adds and removes exactly the entries it recorded adding (`untool_applied.json`);
    an entry the user wrote is never touched.
  * When the plugin creates the file, it adds the path to `.git/info/exclude`, so a file meant
    to be per-user can never be committed by accident.

Measuring: `measure` compares one request with and without the proposal. On Claude Code that is
two small `claude -p` requests on the cheapest model. On Codex and Copilot it costs nothing: Codex renders its prompt
input locally (`codex debug prompt-input`), and Copilot's request is captured by a scripted
endpoint on localhost (`copilot_capture`), so no model is ever called.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import adapter as live_adapter
from . import transcripts

SETTINGS_REL = os.path.join(".claude", "settings.local.json")
CODEX_CONFIG_REL = os.path.join(".codex", "config.toml")
COPILOT_SETTINGS_REL = os.path.join(".github", "copilot", "settings.local.json")

_SETTINGS = {"claude": SETTINGS_REL, "codex": CODEX_CONFIG_REL, "copilot": COPILOT_SETTINGS_REL}
_UNITS = {"claude": "tool", "codex": "capability", "copilot": "MCP server"}

# Codex capability -> (table or None for top level, key, TOML value). Each probed project-local
# on 0.153.1 (see the module docstring); `te_harness.codex` holds the launch-time superset.
CODEX_TOGGLES = {
    "collaboration": ((None, "include_collaboration_mode_instructions", "false"),
                      ("agents", "enabled", "false"),
                      ("features", "multi_agent", "false")),
    "goals": (("features", "goals", "false"),),
    "image_media": (("features", "view_image", "false"),),
}
# What the live Codex lever can reach. `web` and `request_user_input` are a person's tools and
# never proposed (as the web is not on Claude Code); `interactive_guidance` rewrites the base
# prompt's guidance for talking to a user, which an interactive session is; `skills` is
# user-level only.
CODEX_LIVE_CAPABILITIES = tuple(sorted(CODEX_TOGGLES))
MARK = "# tokeneconomizer"


def supports(host):
    return host in _SETTINGS


def unit(host, plural=False):
    u = _UNITS.get(host, "tool")
    return (u[:-1] + "ies" if u.endswith("y") else u + "s") if plural else u


def settings_path(repo_root, host="claude"):
    return os.path.join(repo_root, _SETTINGS.get(host, SETTINGS_REL))


def _read_settings(path):
    """The settings object at `path`: {} when there is no file. A file that is there but is not
    a JSON object is refused rather than written over -- it is the person's, and a parse error
    is no reason to replace what they had in it."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RuntimeError(f"cannot read {path}: {e}")
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"refusing to edit {path}: it is not valid JSON ({e}); fix it first")
    if not isinstance(obj, dict):
        raise RuntimeError(f"refusing to edit {path}: it is not a JSON object")
    return obj


def _guard_target(repo_root, path):
    """Refuse a write that a symlink would carry out of the repository.

    The settings paths are the repository's, so a checkout decides what they are: a `.claude`,
    `.codex` or `.github` that is a symlink, or a settings file that is one, would land the
    write somewhere else -- a user-level configuration, another project."""
    root = os.path.realpath(repo_root)
    parent = os.path.realpath(os.path.dirname(path))
    if os.path.islink(path) or os.path.commonpath([root, parent]) != root:
        raise RuntimeError(f"refusing to write {path}: it is, or goes through, a symlink that "
                           "leads out of the repository")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".te.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".te.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _exclude_from_git(repo_root, rel=SETTINGS_REL):
    """Append `rel` to `.git/info/exclude` (local-only ignore), idempotently."""
    from .paths import _git_common_dir
    gitdir = _git_common_dir(repo_root)
    if not gitdir:
        return False
    # A `.git` FILE names its git directory, so a checkout could point it anywhere: follow it
    # only to something that is a git directory, and never write through a symlinked `info` or
    # `exclude`.
    if (os.path.isfile(os.path.join(repo_root, ".git"))
            and not os.path.isfile(os.path.join(gitdir, "HEAD"))):
        return False
    path = os.path.join(gitdir, "info", "exclude")
    if os.path.islink(os.path.dirname(path)) or os.path.islink(path):
        return False
    entry = "/" + rel.replace(os.sep, "/")
    try:
        existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if entry in existing.split():
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(("" if existing.endswith("\n") or not existing else "\n")
                     + "# added by TokenEconomizer (per-user tool deny list)\n" + entry + "\n")
        return True
    except OSError:
        return False


def applied(store):
    return list((store.read("untool_applied.json") or {}).get("tools", []))


def _record(store, items, path):
    store.write("untool_applied.json", {"tools": sorted(items), "path": path,
                                        "updated": time.time()})


# ---- the JSON-list hosts: Claude Code's deny rules, Copilot's disabled MCP servers -------
def _json_list_keys(host):
    return ("permissions", "deny") if host == "claude" else (None, "disabledMcpServers")


def _apply_json_list(repo_root, items, store, host):
    path = settings_path(repo_root, host)
    _guard_target(repo_root, path)
    created = not os.path.exists(path)
    data = _read_settings(path)
    outer, key = _json_list_keys(host)
    holder = data.setdefault(outer, {}) if outer else data
    lst = holder.setdefault(key, [])
    added = [t for t in items if t not in lst]
    if not added:
        return []
    lst.extend(added)
    _write_json(path, data)
    if created:
        _exclude_from_git(repo_root, _SETTINGS[host])
    _record(store, set(applied(store)) | set(added), path)
    return added


def _restore_json_list(repo_root, store, items, host):
    mine = set(applied(store))
    target = mine if not items else mine & set(items)
    if not target:
        return []
    path = settings_path(repo_root, host)
    _guard_target(repo_root, path)
    data = _read_settings(path)
    outer, key = _json_list_keys(host)
    holder = (data.get(outer) or {}) if outer else data
    lst = holder.get(key) or []
    kept = [r for r in lst if r not in target]
    removed = [r for r in lst if r in target]
    if key in holder:
        holder[key] = kept
        if not kept:
            holder.pop(key)
        if outer and not holder:
            data.pop(outer, None)
        _write_json(path, data)
    _record(store, mine - target, path)
    return removed


# ---- Codex: marked lines in the project's .codex/config.toml ----------------------------
_HEADER_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-\"]+)\s*\]\s*(#.*)?$")
_ANY_HEADER_RE = re.compile(r"^\s*\[")


def _sections(lines):
    """[(table or None, start, end)] -- line ranges of the top level and each [table]."""
    out, cur, start = [], None, 0
    for i, line in enumerate(lines):
        if _ANY_HEADER_RE.match(line):
            out.append((cur, start, i))
            m = _HEADER_RE.match(line)
            cur, start = (m.group(1) if m else "[[array]]"), i + 1
    out.append((cur, start, len(lines)))
    return out


def _key_set(lines, table, key):
    """Is `key` already set in `table` -- as `key =` under [table], or as a top-level dotted
    `table.key =`? A key the user set is theirs, whatever its value."""
    for t, a, b in _sections(lines):
        for line in lines[a:b]:
            s = line.strip()
            if table is None and t is None and re.match(rf"{re.escape(key)}\s*=", s):
                return True
            if table is not None and t == table and re.match(rf"{re.escape(key)}\s*=", s):
                return True
            if table is not None and t is None and re.match(
                    rf"{re.escape(table)}\.{re.escape(key)}\s*=", s):
                return True
    return False


def _toml_insert(text, table, key, value, tag):
    """Insert `key = value` into `table` (None: top level), marked with `tag`."""
    lines = text.splitlines()
    if _key_set(lines, table, key):
        return text, False
    # `features = { ... }` -- an inline table cannot be extended, and adding a [features] table
    # beside it would make the file invalid; leave that table to the person who wrote it.
    if table is not None and any(re.match(rf"\s*{re.escape(table)}\s*=", ln) for ln in lines):
        return text, False
    line = f"{key} = {value}  {tag}"
    secs = _sections(lines)
    first_header = next((a - 1 for t, a, b in secs[1:]), len(lines))
    if table is None:
        lines.insert(first_header, line)
    else:
        hdr = next((a for t, a, b in secs if t == table), None)
        dotted = any(re.match(rf"\s*{re.escape(table)}\.\w+\s*=", ln)
                     for ln in lines[:first_header])
        if hdr is not None:
            lines.insert(hdr, line)
        elif dotted:
            lines.insert(first_header, f"{table}.{line}")
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{table}]  {tag}", line]
    return "\n".join(lines) + "\n", True


def _toml_remove(text, tags):
    """Drop every line carrying one of `tags`; a [table] header the plugin added goes too,
    unless someone has since put a key of their own under it."""
    lines = text.splitlines()
    marked = lambda ln: any(ln.rstrip().endswith(t) for t in tags)
    keep = []
    for i, ln in enumerate(lines):
        if not marked(ln):
            keep.append(ln)
            continue
        if _HEADER_RE.match(ln):
            j, foreign = i + 1, False
            while j < len(lines) and not _ANY_HEADER_RE.match(lines[j]):
                if lines[j].strip() and not marked(lines[j]):
                    foreign = True
                    break
                j += 1
            if foreign:
                keep.append(ln)
    while keep and not keep[-1].strip():
        keep.pop()
    return ("\n".join(keep) + "\n") if keep else ""


def _toml_valid(text):
    try:
        import tomllib
    except ImportError:           # Python 3.10: Codex itself is the check (`verify_codex`)
        return True
    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def _git_tracked(repo_root, rel):
    """Is `rel` committed in this repository? False when git cannot tell."""
    try:
        out = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=repo_root,
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def codex_config_error(repo_root, cli="codex", timeout=30):
    """Codex's own complaint about the configuration it would load here, or None.

    `codex features list` loads the whole layered configuration and calls no model, so a file
    that would break the person's next session fails here first. Silent (None) when the CLI is
    not installed, or the project is untrusted and its file is not loaded at all."""
    if not shutil.which(cli):
        return None
    try:
        out = subprocess.run([cli, "features", "list"], capture_output=True, text=True,
                             cwd=repo_root, timeout=timeout, env=dict(os.environ, TE_SIDECALL="1"))
    except (OSError, subprocess.SubprocessError):
        return None
    return None if out.returncode == 0 else ((out.stderr or out.stdout).strip()[:500]
                                             or f"exit {out.returncode}")


def _apply_codex(repo_root, capabilities, store):
    path = settings_path(repo_root, "codex")
    _guard_target(repo_root, path)
    created = not os.path.exists(path)
    if not created and _git_tracked(repo_root, CODEX_CONFIG_REL):
        # Claude Code's and Copilot's files are per-user by name; a project's .codex/config.toml
        # is often the TEAM's, committed. Marked lines in it would ride along in the next commit.
        raise RuntimeError(f"{path} is committed in this repository; the plugin will not edit a "
                           "shared file. Add the toggles by hand, or keep them in your own "
                           "copy of it.")
    original = open(path, encoding="utf-8").read() if not created else ""
    text = original
    added = []
    for cap in capabilities:
        if cap not in CODEX_TOGGLES or cap in applied(store):
            continue
        for table, key, value in CODEX_TOGGLES[cap]:
            text, _ = _toml_insert(text, table, key, value, f"{MARK}:{cap}")
        added.append(cap)
    if not added:
        return []
    if not _toml_valid(text):
        raise RuntimeError(f"refusing to write {path}: the result would not parse as TOML")
    _write_text(path, text)
    err = codex_config_error(repo_root)
    if err:
        if created:
            os.remove(path)
        else:
            _write_text(path, original)
        raise RuntimeError(f"Codex rejected the edited {path}, so it was put back: {err}")
    if created:
        _exclude_from_git(repo_root, CODEX_CONFIG_REL)
    _record(store, set(applied(store)) | set(added), path)
    return added


def _restore_codex(repo_root, store, capabilities):
    mine = set(applied(store))
    target = mine if not capabilities else mine & set(capabilities)
    if not target:
        return []
    path = settings_path(repo_root, "codex")
    _guard_target(repo_root, path)
    if os.path.exists(path):
        text = _toml_remove(open(path, encoding="utf-8").read(),
                            [f"{MARK}:{c}" for c in target])
        if text.strip():
            _write_text(path, text)
        else:
            os.remove(path)
    _record(store, mine - target, path)
    return sorted(target)


def copilot_trusted_folders(env=None):
    """Copilot's `trustedFolders` (its config.json starts with `//` comment lines), or None."""
    from .hosts import copilot_home
    try:
        text = open(os.path.join(copilot_home(env), "config.json"), encoding="utf-8").read()
        doc = json.loads("\n".join(ln for ln in text.splitlines()
                                   if not ln.lstrip().startswith("//")))
    except (OSError, ValueError):
        return None
    return [f for f in (doc.get("trustedFolders") or []) if isinstance(f, str)]


def copilot_folder_trusted(path, env=None):
    """Is `path` in or under a folder Copilot trusts? None when the config is unreadable."""
    folders = copilot_trusted_folders(env)
    if folders is None:
        return None
    real = os.path.realpath(path)
    for f in folders:
        f = os.path.realpath(f)
        if real == f or real.startswith(f.rstrip(os.sep) + os.sep):
            return True
    return False


def verify_copilot(repo_root, servers, cli="copilot"):
    """{server: tools still in the request} for a session started in `repo_root`, by the local
    capture (no model call); None if no request could be captured."""
    body = copilot_capture(cwd=repo_root, cli=cli)
    if not body:
        return None
    names = _request_tools(body)
    return {s: sum(1 for n in names if n and n.startswith(s + "-")) for s in servers}


def codex_home(env=None):
    env = os.environ if env is None else env
    return env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def codex_project_trusted(repo_root, env=None):
    """True/False from the user's Codex config: is this checkout a trusted project? A
    project-local `.codex/config.toml` is ignored until it is. None if the config is
    unreadable."""
    try:
        text = open(os.path.join(codex_home(env), "config.toml"), encoding="utf-8").read()
    except OSError:
        return None
    want = {os.path.abspath(repo_root), os.path.realpath(repo_root)}
    current = None
    for line in text.splitlines():
        m = re.match(r'^\s*\[projects\.(?:"([^"]+)"|\'([^\']+)\')\]\s*$', line)
        if m:
            current = m.group(1) or m.group(2)
            continue
        if _ANY_HEADER_RE.match(line):
            current = None
            continue
        if current in want and re.match(r'^\s*trust_level\s*=\s*"trusted"', line):
            return True
    return False


def verify_codex(repo_root, capabilities, cli="codex", timeout=30):
    """What `codex features list`, run in the repository, says about the applied toggles:
    {feature: enabled} for the features those capabilities switch off, or None when the CLI
    is unavailable. Free: it reads configuration and calls no model."""
    feats = [k for c in capabilities for t, k, v in CODEX_TOGGLES.get(c, ())
             if t == "features"]
    if not feats or not shutil.which(cli):
        return None
    try:
        out = subprocess.run([cli, "features", "list"], capture_output=True, text=True,
                             cwd=repo_root, timeout=timeout,
                             env=dict(os.environ, TE_SIDECALL="1"))
    except (OSError, subprocess.SubprocessError):
        return None
    state = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in feats:
            state[parts[0]] = parts[-1] == "true"
    return state or None


# ---- dispatch ------------------------------------------------------------------------------
def apply(repo_root, tools, store, host="claude"):
    """Write `tools` (tools, capabilities or MCP servers, by host). Returns those newly added."""
    if host == "codex":
        return _apply_codex(repo_root, tools, store)
    return _apply_json_list(repo_root, tools, store, host)


def restore(repo_root, store, tools=None, host="claude"):
    """Remove entries this plugin added (all of them, or just `tools`). Returns removed."""
    if host == "codex":
        return _restore_codex(repo_root, store, tools)
    return _restore_json_list(repo_root, store, tools, host)


def filter_proposal(proposal, streaks, evidence):
    """Hold back high-risk tools until they have HIGH_RISK_FACTOR x the usual evidence."""
    out = []
    for t in proposal:
        need = evidence * (live_adapter.HIGH_RISK_FACTOR if t in live_adapter.HIGH_RISK else 1)
        if int(streaks.get(t, 0)) >= need:
            out.append(t)
    return out


def copilot_server_proposal(raw, catalog, used, servers):
    """MCP servers every one of whose catalog tools is in the evidence proposal `raw`."""
    by_server = {}
    for t in catalog:
        s = transcripts.mcp_server_of(t, servers)
        if s:
            by_server.setdefault(s, set()).add(t)
    raw = set(raw)
    return sorted(s for s, tools in by_server.items()
                  if tools and tools <= raw and not (tools & set(used)))


# ---- the catalog probe and the measurement ------------------------------------------------
def _init_tools(extra_args, cli="claude", timeout=60):
    """Tool list from a `claude -p` session's init event; the process is killed right after it,
    before the model is asked anything."""
    env = dict(os.environ, TE_SIDECALL="1")
    cmd = [cli, "-p", "Reply with OK", "--output-format", "stream-json", "--verbose",
           "--model", "haiku", "--max-turns", "1"] + list(extra_args)
    with tempfile.TemporaryDirectory(prefix="te-probe-") as cwd:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, env=env, cwd=cwd)
        tools, version = None, None
        deadline = time.time() + timeout
        try:
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "system" and ev.get("subtype") == "init":
                    tools, version = ev.get("tools"), ev.get("claude_code_version")
                    break
                if time.time() > deadline:
                    break
        finally:
            p.kill()
            p.wait()
    return tools, version


class _Capture(BaseHTTPRequestHandler):
    """A chat-completions endpoint that records the request and answers "OK"."""
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(json.dumps({"object": "list", "data": [{"id": "gpt-5.4", "object": "model"}]}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        self.server.captured.append(body)
        msg = {"role": "assistant", "content": "OK"}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if body.get("stream"):
            chunks = [{"choices": [{"index": 0, "delta": msg, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": usage}]
            self._send("".join(f"data: {json.dumps(dict(c, object='chat.completion.chunk'))}"
                               "\n\n" for c in chunks) + "data: [DONE]\n\n",
                       "text/event-stream")
        else:
            self._send(json.dumps({"object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "message": msg, "finish_reason": "stop"}]}))


def copilot_capture(extra_args=(), cwd=None, cli="copilot", timeout=90):
    """The first request a fresh `copilot -p` session sends, captured on localhost.

    Copilot's BYOK mode is pointed at a scripted endpoint in this process, so the request --
    system prompt, tool schemas -- is recorded exactly as Copilot builds it, and no model,
    credential or quota is touched. Returns the request body, or None."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    srv.captured = []
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    # The person's own model, when the environment names one: Copilot shapes its built-in tools
    # by model family (`edit`/`create` for Claude models, `apply_patch` for GPT ones).
    from .sidecall import copilot_model
    # A router's `provider/` prefix (OpenRouter: `anthropic/claude-haiku-4.5`) is not part of
    # the id Copilot recognises.
    model = (copilot_model() or "gpt-5.4").split("/", 1)[-1]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COPILOT_PROVIDER_") and k != "COPILOT_MODEL"}
    env.update(TE_SIDECALL="1", COPILOT_PROVIDER_TYPE="openai",
               COPILOT_PROVIDER_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}/v1",
               COPILOT_PROVIDER_API_KEY="te-local-probe", COPILOT_MODEL=model,
               COPILOT_PROVIDER_WIRE_API="completions", NO_PROXY="127.0.0.1,localhost",
               no_proxy="127.0.0.1,localhost")
    tmp = None
    if cwd is None:
        tmp = tempfile.mkdtemp(prefix="te-probe-")
        cwd = tmp
    try:
        subprocess.run([cli, "-p", "Reply with OK", "--no-auto-update", "--no-color"]
                       + list(extra_args), cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return srv.captured[0] if srv.captured else None


def _request_tools(body):
    return [(t.get("function") or {}).get("name") or t.get("name") or t.get("type")
            for t in (body or {}).get("tools") or []]


def _copilot_version(cli="copilot"):
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30)
        return (out.stdout or "").strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def refresh_catalog(state_root, cli=None, host="claude"):
    """Record the tool list a fresh session loads on this machine."""
    if host == "copilot":
        body = copilot_capture(cli=cli or "copilot")
        tools = [t for t in _request_tools(body) if t]
        if not tools:
            raise RuntimeError("could not capture the tool list of a `copilot -p` request")
        path = live_adapter.catalog_path(state_root, "copilot")
        _write_json(path, {"tools": sorted(tools), "copilot_version": _copilot_version(),
                           "probed_at": time.time()})
        return path, sorted(tools)
    if host != "claude":
        raise RuntimeError(f"no catalog probe for {host}")
    tools, version = _init_tools([], cli=cli or "claude")
    if not tools:
        raise RuntimeError("could not read the tool list from a `claude -p` init event")
    path = live_adapter.catalog_path(state_root, "claude")
    _write_json(path, {"tools": sorted(tools), "claude_code_version": version,
                       "probed_at": time.time()})
    return path, sorted(tools)


def _prompt_tokens(extra_args, cli="claude", timeout=180):
    env = dict(os.environ, TE_SIDECALL="1")
    cmd = [cli, "-p", "Reply with exactly: OK", "--output-format", "json", "--model", "haiku",
           "--max-turns", "1"] + list(extra_args)
    with tempfile.TemporaryDirectory(prefix="te-measure-") as cwd:
        out = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd,
                             timeout=timeout)
    data = json.loads(out.stdout or "{}")
    u = data.get("usage") or {}
    return (sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                              "cache_creation_input_tokens")),
            data.get("total_cost_usd"))


def _codex_prompt_chars(overrides, cli="codex", timeout=60):
    cmd = [cli, "debug", "prompt-input"]
    for table, key, value in overrides:
        cmd += ["-c", f"{table}.{key}={value}" if table else f"{key}={value}"]
    with tempfile.TemporaryDirectory(prefix="te-measure-") as cwd:
        out = subprocess.run(cmd + ["OK"], capture_output=True, text=True, cwd=cwd,
                             timeout=timeout, env=dict(os.environ, TE_SIDECALL="1"))
    return len(out.stdout or "") if out.returncode == 0 else None


def measure(tools, cli=None, host="claude"):
    """Request size with and without `tools` removed.

    Claude Code: prompt tokens of two small requests. Codex and Copilot: characters of the
    rendered request, and a ~4-characters-per-token estimate, at no cost."""
    if not tools:
        return None
    if host == "codex":
        toggles = [t for c in tools for t in CODEX_TOGGLES.get(c, ())]
        full = _codex_prompt_chars([], cli=cli or "codex")
        lean = _codex_prompt_chars(toggles, cli=cli or "codex")
        if full is None or lean is None:
            raise RuntimeError("`codex debug prompt-input` failed")
        return {"capabilities": list(tools), "prompt_input_chars_full": full,
                "prompt_input_chars_lean": lean, "saved_chars_per_request": full - lean,
                "saved_tokens_per_request_est": (full - lean) // 4,
                "note": "prompt input only (developer/user items); tool schemas not included",
                "probe_cost_usd": 0.0}
    if host == "copilot":
        args = [a for s in tools for a in ("--disable-mcp-server", s)]
        full = copilot_capture(cli=cli or "copilot")
        lean = copilot_capture(args, cli=cli or "copilot")
        if not full or not lean:
            raise RuntimeError("could not capture a `copilot -p` request")
        size = lambda b: len(json.dumps(b.get("tools"))) + len(json.dumps(b.get("messages")))
        return {"mcp_servers": list(tools),
                "tools_full": len(_request_tools(full)), "tools_lean": len(_request_tools(lean)),
                "request_chars_full": size(full), "request_chars_lean": size(lean),
                "saved_chars_per_request": size(full) - size(lean),
                "saved_tokens_per_request_est": (size(full) - size(lean)) // 4,
                "probe_cost_usd": 0.0}
    cli = cli or "claude"
    full, c1 = _prompt_tokens([], cli=cli)
    lean, c2 = _prompt_tokens(["--disallowed-tools", ",".join(tools)], cli=cli)
    return {"tools": list(tools), "prompt_tokens_full": full, "prompt_tokens_lean": lean,
            "saved_per_request": full - lean,
            "probe_cost_usd": round((c1 or 0) + (c2 or 0), 4)}
