"""Shared GitHub Copilot CLI support for the te_harness runners.

Why a separate module: SWE-bench and Web-Bench need the SAME four Copilot-specific things,
and none of them are shared with Claude Code:

  1. **Host install resolution.** Copilot is a single native binary plus a package cache
     and an auth/config dir. SWE-bench bind-mounts all three into the task container;
     Web-Bench runs the host binary directly. Both resolve them the same way.
  2. **Different auth.** Copilot authenticates against GitHub (`~/.copilot`, or a token in
     COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN), NOT an Anthropic API key. A runner
     that demanded ANTHROPIC_API_KEY would refuse to start on a perfectly good box.
  3. **Token recovery from the debug log.** This is the one that silently corrupts results
     if you skip it. Copilot's `--output-format json` stream reports per-message OUTPUT
     tokens and a premium-request count, and its terminal `result` event carries no token
     fields at all -- so reading usage the way the Claude runner does yields a row of
     zeros, and every cost number downstream is wrong rather than missing. The real
     input/cache/output breakdown is only in the CLI's own debug log, which is why every
     Copilot invocation here runs with `--log-level all --log-dir ...` and mines the
     result. `sum_openai_usage` is ported verbatim from the AgentEO study's
     `swebench/agents.py`, which is the accounting the published Copilot rows used.
  4. **A Copilot-flavoured optimizer call.** The mechanisms' between-task proposer/judge/
     nudge-writer is a paid LLM call. On the native Copilot path there is no Anthropic key
     to make it with, so it goes through the same `copilot -p` binary -- tools denied and
     custom instructions off, so it is a pure text completion that cannot touch the repo.

The flag vocabulary also differs from Claude's and is easy to get subtly wrong:
`--excluded-tools` is REPEATED per tool (Claude takes one comma-joined `--disallowed-tools`),
and there is **no `--max-turns`** -- wall-clock timeout is the only cap Copilot offers. See
`build_argv` for both.

Stdlib only, like the rest of the harness.
"""
import glob
import json
import os
import shutil
import subprocess
import tempfile

from tokeneconomizer.vendor import pricing

# What `copilot --effort` accepts. Validated by the runners at construction so a typo fails
# before the run rather than silently falling back to the CLI default (effort is part of
# the cost basis, so a silent default makes the run unreproducible).
EFFORT_LEVELS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})

# Copilot's native tool names -- the analog of PROTECTED_TOOLS on the Claude path. Without
# a shell and an edit path there is no task, so the untooling mask may never drop these: a
# `resolved: false` caused by a missing `edit` is a harness artifact, not a mechanism result.
PROTECTED_TOOLS = frozenset({"bash", "edit", "create", "str_replace", "view"})

# Fetch affordances removed on every task invocation. The agent's own model traffic is not a
# tool call, so `--network none` is not an option; dropping these plus the built-in GitHub
# MCP server removes the easy contamination path to a gold patch. Mirrors the AgentEO
# study's CopilotDriver.invoke, so rows stay comparable with the published ones.
_ISOLATION_ARGS = ["--excluded-tools", "web_fetch", "--excluded-tools", "web_search",
                   "--disable-builtin-mcps"]

# Per-run debug-log dir inside the task container (SWE-bench). Web-Bench uses a dir under
# the run dir instead, since it has no container.
CONTAINER_LOG_DIR = "/home/agent/.copilot-runlog"

_INSTALL_HINT = (
    "\n  install:  curl -fsSL https://gh.io/copilot-install | bash"
    "\n  sign in:  copilot   (then /login), or export COPILOT_GITHUB_TOKEN"
    "\n  override: COPILOT_HOST_BIN / COPILOT_HOST_CACHE / COPILOT_HOME")


# --------------------------------------------------------------------------------------
# host install
# --------------------------------------------------------------------------------------
def copilot_bin():
    """Host path to the Copilot CLI binary.

    Order: explicit override, then the path `gh.io/copilot-install` writes, then PATH. We
    check $HOME/.local/bin before PATH because a detached launcher often runs with a PATH
    that omits it, and falling through to a bare "copilot" would fail deep inside a task.
    """
    p = os.environ.get("COPILOT_HOST_BIN")
    if p:
        return os.path.realpath(p)
    home = os.path.expanduser("~/.local/bin/copilot")
    if os.access(home, os.X_OK):
        return os.path.realpath(home)
    found = shutil.which("copilot")
    if not found:
        raise RuntimeError("no copilot binary found." + _INSTALL_HINT)
    return os.path.realpath(found)


def copilot_cache():
    """Host package cache (~170MB+). Bind-mounted read-only and symlinked in-container
    rather than copied -- it is the bulk of the seed, and copying it per task is minutes."""
    return os.environ.get("COPILOT_HOST_CACHE") or os.path.expanduser("~/.cache/copilot")


def copilot_config():
    """Host auth/config dir. COPILOT_HOME is the CLI's own override for it."""
    return os.environ.get("COPILOT_HOME") or os.path.expanduser("~/.copilot")


def cli_version(binary):
    """`copilot --version` output, recorded on every row: the CLI self-updates by default,
    and a version that moved mid-sweep changes the cost basis."""
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=60)
        return (out.stdout or out.stderr or "").strip().splitlines()[0][:120]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown"


def require_install(*, need_container_seed=False):
    """Fail at construction if the host install is incomplete.

    Docker silently invents an empty directory for a `-v` source that does not exist, so a
    missing cache or config would otherwise surface only as every rollout dying ~30s in
    with an empty patch -- hundreds of paid failures instead of one startup error.
    """
    paths = [copilot_bin()]
    if need_container_seed:
        paths += [copilot_cache(), copilot_config()]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise RuntimeError("copilot host install incomplete: "
                           + ", ".join(missing) + _INSTALL_HINT)
    return paths[0]


def auth_env():
    """Auth vars to forward, in the CLI's own documented precedence order
    (`copilot help environment`). Empty is normal and fine: the usual path is the on-disk
    credential in ~/.copilot, which SWE-bench seeds into the container and Web-Bench reads
    directly. Only a headless/token setup populates these."""
    out = {}
    for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        v = os.environ.get(k)
        if v:
            out[k] = v
    return out


# BYOK -- "bring your own key" (`copilot help providers`), the Copilot CLI's own built-in
# provider support. The CLI can be pointed at a non-GitHub provider, in which case it bills
# that provider and GitHub auth is not needed at all. COPILOT_PROVIDER_BASE_URL is the
# activation switch. The sweep's DEFAULT remains the Copilot subscription; this path exists
# for a host without one (or with its quota spent).
#
# The two credential vars are listed separately because they are SECRETS: they are
# forwarded to a task container BY NAME only, so the value never reaches argv on a shared
# host -- the same treatment ANTHROPIC_API_KEY gets on the Claude lane.
PROVIDER_VARS = (
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_PROVIDER_WIRE_API",
    "COPILOT_PROVIDER_TRANSPORT",
    "COPILOT_PROVIDER_AZURE_API_VERSION",
    "COPILOT_PROVIDER_HEADERS",
    "COPILOT_PROVIDER_MODEL_ID",
    "COPILOT_PROVIDER_WIRE_MODEL",
    "COPILOT_PROVIDER_MAX_PROMPT_TOKENS",
    "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS",
)
PROVIDER_SECRET_VARS = ("COPILOT_PROVIDER_API_KEY", "COPILOT_PROVIDER_BEARER_TOKEN")

# ---- the between-task optimizer call's tool surface ----------------------------------
# TWO flags, because they do different things and only one of them costs tokens.
#
# `--deny-tool` withholds PERMISSION: the tool stays in the model's schema and the CLI
# simply refuses to run it. `--excluded-tools` removes it from what is offered to the model
# at all. The optimizer call is a pure text completion that needs no tool, so it wants the
# second -- and for a long time it passed only the first.
#
# Measured on this host (2026-09-07), one real judge-shaped prompt of 2,423 chars:
#   --deny-tool only        22,460 billed prompt tokens -> 29,040 effective
#   + --excluded-tools       5,550 billed prompt tokens ->  7,832 effective
# The 16,910-token difference is the tool schema, re-sent on EVERY mechanism call. On the
# 60-task sklearn trimech cell that was ~12,500 of the 17,577 tokens/task of mechanism
# overhead the ledger charged the arm -- overhead that bought nothing, since the call is
# forbidden to use a tool either way.
#
# Both are kept: permission AND schema. Denying a tool that is not offered is harmless, and
# leaving the deny list in place means a CLI that ignores one flag still refuses the tool.
OPTIMIZER_DENY_TOOLS = ("bash", "view", "grep", "glob", "edit", "create", "str_replace",
                        "web_fetch", "web_search")
# Everything the CLI offers, so the completion is offered nothing. Supersets the deny list;
# a name the installed CLI does not know is ignored rather than an error.
OPTIMIZER_EXCLUDE_TOOLS = OPTIMIZER_DENY_TOOLS + (
    "ask_user", "session_store_sql", "sql", "task", "skill",
    "list_agents", "read_agent", "write_agent", "fetch_copilot_cli_documentation",
)


def byok_active():
    """True when the CLI is configured to bill a custom provider rather than GitHub."""
    return bool(os.environ.get("COPILOT_PROVIDER_BASE_URL"))


def provider_base():
    """What this run bills, for `run_config.json` -> routing.base_url.

    A BYOK run and a subscription run are DIFFERENT COST BASES and must not be pooled, so
    the row has to say which one it was -- the tree name cannot, since both are `copilot_*`.
    """
    return os.environ.get("COPILOT_PROVIDER_BASE_URL") or "github-copilot"


# ---- the mechanism call's TRANSPORT --------------------------------------------------
def anthropic_optimizer_endpoint(env=None):
    """Where a mechanism call should go as a DIRECT Messages request -- or None for the CLI.

    Returns (base_url, api_key, bearer), or None to keep spawning `copilot -p`.

    WHY there is a choice at all. `copilot -p` is an AGENT session: even with every tool
    excluded it still carries the CLI's own system prompt, and the mechanisms' calls are
    one-shot text completions that need none of it. Measured on this host, the same
    2,4xx-char judge prompt costs ~1,730 effective tokens over the Messages API and ~4,700
    through the trimmed CLI -- and the CLAUDE harness has always used the API for exactly
    these calls (swebench.ClaudeSwebenchRunner.optimizer_call). So the CLI transport was
    also an ASYMMETRY between the two agents' mechanism overhead, one-directional and in
    the same class as the cache-write split fixed on 2026-09-05: it inflated Copilot's
    overhead against Claude's on identical work, biasing the comparison the sweep exists
    to make.

    WHAT IT MUST NOT DO is change the cost basis. `ledger.overhead` charges these calls to
    the same basis as the task rows, so the endpoint has to bill whatever the TASKS bill.
    Exactly one configuration has such an endpoint: BYOK with an Anthropic-typed provider
    and a credential, where the call goes to the provider URL and credential the CLI itself
    is using -- same key, same meter, only the wire changes. Everything else is None and
    keeps `copilot -p` (`optimizer_call`): the GitHub subscription is reachable only through
    the CLI, a non-Anthropic BYOK provider does not speak this wire, and a missing
    credential cannot be guessed.
    """
    env = os.environ if env is None else env
    base = env.get("COPILOT_PROVIDER_BASE_URL")
    if not base or (env.get("COPILOT_PROVIDER_TYPE") or "").lower() != "anthropic":
        return None
    key = env.get("COPILOT_PROVIDER_API_KEY") or ""
    bearer = env.get("COPILOT_PROVIDER_BEARER_TOKEN") or ""
    if not (key or bearer):
        return None
    return (base, key, bearer)


def provider_env():
    """Non-secret BYOK vars to forward. Empty unless BYOK is active."""
    if not byok_active():
        return {}
    return {k: os.environ[k] for k in PROVIDER_VARS if os.environ.get(k)}


def provider_secret_names():
    """Names of the BYOK credential vars that are set -- forwarded by name, never by value."""
    if not byok_active():
        return []
    return [k for k in PROVIDER_SECRET_VARS if os.environ.get(k)]


def container_seed_setup():
    """Bash that turns the three read-only mounts into a working $HOME for the agent.

    The package cache is SYMLINKED, not copied (it is the bulk of the seed); only the tiny
    writable exp-cache.json is copied. The config is copied excluding logs/ and
    session-state/, which are host interactive-session junk and can run to ~1GB. Ported
    from the AgentEO study's CopilotDriver.seed_setup.
    """
    return ("mkdir -p /home/agent/.cache/copilot /home/agent/.copilot; "
            "ln -sfn /seed/copilot-cache/pkg /home/agent/.cache/copilot/pkg; "
            "cp -a /seed/copilot-cache/exp-cache.json /home/agent/.cache/copilot/ "
            "2>/dev/null || true; "
            "( cd /seed/copilot-config && tar -cf - --exclude=./logs "
            "--exclude=./session-state . ) | ( cd /home/agent/.copilot && tar -xf - ); ")


def container_mounts(binary):
    """host->container `-v` specs for the three pieces of the install."""
    return ["-v", f"{binary}:/usr/local/bin/copilot:ro",
            "-v", f"{copilot_cache()}:/seed/copilot-cache:ro",
            "-v", f"{copilot_config()}:/seed/copilot-config:ro"]


# --------------------------------------------------------------------------------------
# invocation
# --------------------------------------------------------------------------------------
def normalize_model(model):
    """The value to record as this run's BILLED model id, or None if there isn't one.

    `auto` is a routing DIRECTIVE, not a model: the CLI picks a different model per request
    (we have observed claude-haiku-4.5 and mai-code-1.1-flash inside one session), so there
    is no single id that was billed. Recording the literal string "auto" makes
    `pricing.weights_for` treat it as an unknown model and hard-fail -- correctly, since it
    refuses to silently misprice an id it does not know. None instead lands in pricing's
    documented "no resolvable id" branch, which is the Claude curve and the historical
    behaviour for Copilot rows.

    Omitting `--model` from the argv is equivalent to `--model auto` (the CLI's default is
    the router), so nothing is lost by normalising it away.
    """
    m = (model or "").strip()
    return None if (not m or m.lower() == "auto") else m


def validate_priceable(model, agent):
    """Fail at construction if a PINNED model has no pricing entry.

    Otherwise `pricing.effective_tokens` raises at the very END of run_task -- after the
    rollout has been paid for and graded. The check is the same call the row will make.
    """
    pricing.weights_for({"model": model, "agent": agent})


def effort_applies(model):
    """Whether `--effort` may be passed alongside this model.

    Copilot's router model (`--model auto`, and the unset default) REJECTS the flag
    outright -- "Model \"auto\" does not support reasoning effort configuration" -- and the
    CLI exits without running, so passing it anyway turns every task into a 0-turn failure.
    Only a pinned model id takes an effort level.

    The consequence is a real limitation, not a detail: on `auto` the reasoning effort is
    whatever the router picks, so it is NOT a controlled part of the cost basis the way it
    is for Claude. Runs made on `auto` are internally comparable but should not be compared
    against effort-pinned Claude numbers as if effort were held equal. Pin a model your
    subscription serves if you need effort fixed.
    """
    if os.environ.get("TE_EFFORT_APPLIES") == "0":
        # The launcher probed this exact model and the CLI rejected the flag (see
        # te_check_copilot_model in scripts/launch/_env.sh). Passing it anyway would make
        # every task exit 0-turn, so we drop it and the row records effort: null.
        return False
    return bool(model) and str(model).lower() != "auto"


def build_argv(prompt, *, log_dir, model=None, effort=None, exclude_tools=(),
               mcp_config=None):
    """The `copilot -p` argv for one sealed-agent segment.

    Two differences from the Claude path that are easy to get wrong:
      * `--excluded-tools` is REPEATED once per tool. Claude takes a single comma-joined
        `--disallowed-tools`; passing a comma-joined list here silently excludes nothing,
        so the untooling arm would measure a no-op as a null result.
      * there is **no `--max-turns`**. Copilot offers no turn cap at all, so the caller's
        wall-clock timeout (and the spiral guard) are the only bounds. Callers must not
        assume a turn cap is in force.

    Debug logging is always on: `usage_from_log` is the only honest source of input/cache
    tokens (see the module docstring).
    """
    argv = ["copilot", "-p", prompt, "--output-format", "json",
            "--allow-all-tools", "--no-color", "--bash-env=on",
            "--log-level", "all", "--log-dir", log_dir]
    argv += _ISOLATION_ARGS
    for t in (exclude_tools or ()):
        if t in PROTECTED_TOOLS:
            continue
        argv += ["--excluded-tools", t]
    if model:
        argv += ["--model", str(model)]
    # Only for a pinned model -- see effort_applies(); on `auto` the flag is fatal.
    if effort and effort_applies(model):
        argv += ["--effort", str(effort)]
    if mcp_config:
        argv += ["--additional-mcp-config", "@" + mcp_config]
    return argv


# CLI errors that mean "no task will ever run with this configuration". They surface on
# stderr with the process exiting before a single turn, so without an explicit check a
# misconfigured sweep writes a whole tree of plausible 0-turn failures instead of stopping.
_FATAL_CONFIG_ERRORS = (
    "is not available",                       # --model <id> not in this subscription
    "does not support reasoning effort",      # --effort against a model without the knob
)


def fatal_config_error(stderr_text):
    """The config-error line in `stderr_text`, or None. Callers should RAISE on a hit."""
    for line in (stderr_text or "").splitlines():
        low = line.lower()
        if any(m in low for m in _FATAL_CONFIG_ERRORS):
            return line.strip()[:300]
    return None


def run_env(extra=None):
    """Environment additions for any Copilot invocation.

    COPILOT_AUTO_UPDATE=false because the CLI self-updates by default outside CI. In the
    container the binary is a read-only mount so an update can only add startup latency and
    noise; on the host it would change the CLI version mid-sweep, which changes the cost
    basis. Either way the version must be pinned for the duration of a run.
    """
    e = {"COPILOT_AUTO_UPDATE": "false"}
    e.update(auth_env())
    e.update(provider_env())
    e.update(extra or {})
    return e


# --------------------------------------------------------------------------------------
# accounting
# --------------------------------------------------------------------------------------
def sum_openai_usage(log_text):
    """Sum the OpenAI-style `usage` blocks the debug log records per upstream API call,
    mapped onto our (Claude-shaped) token schema.

    Copilot serves models behind a chat-completions API and logs one
    `"usage": {prompt_tokens, completion_tokens, total_tokens,
    prompt_tokens_details:{cached_tokens}, ...}` object per call. We extract each via
    balanced-brace scanning (the log is pretty-printed, not one-JSON-per-line) and
    accumulate:
        input_tokens            = sum(prompt_tokens - cached_tokens - cache_creation_tokens)
        cache_read_input_tokens = sum(cached_tokens)
        cache_creation_input_tokens = sum(cache_creation_tokens)
        output_tokens           = sum(completion_tokens)
        tokens_total            = sum(total_tokens)                    # prompt+completion
    Returns None when no usage blocks are present, so callers can leave the token fields
    null rather than record a confident zero.

    CACHE WRITES (2026-09-05). This was ported verbatim from the AgentEO study's
    swebench/agents.py, which read only `cached_tokens` and left cache_creation None on the
    grounds that the API did not expose it. It does now --
    `prompt_tokens_details.cache_creation_tokens` -- and because `prompt_tokens` counts
    cache reads, cache writes and fresh input alike, folding the writes into
    `input_tokens` priced them at 1.0x when the vendor bills 1.25x.

    Before the fix nearly all of what was booked as fresh input was cache writes, so
    effective tokens were understated. That error was ONE-DIRECTIONAL and CLAUDE-vs-COPILOT ASYMMETRIC -- the Claude path records the split
    correctly from the CLI's own result event -- so it made Copilot rows look cheaper than
    Claude rows on identical work, biasing the exact comparison the sweep exists to make.

    `tokens_total` is untouched (it is prompt+completion either way); only the split moves.
    A log with no `cache_creation_tokens` yields 0 and the old arithmetic exactly, so older
    logs re-parse to their previous values. Stored rows written before this change still
    carry the old split and are NOT comparable on cost to rows written after it -- they can
    be corrected in place, because every run dir keeps the `copilot_debug.log` this parses.
    """
    key = '"usage": {'
    i, n = 0, len(log_text)
    n_blocks = 0
    prompt = cached = created = completion = total = 0
    while True:
        j = log_text.find(key, i)
        if j < 0:
            break
        s = j + len(key) - 1                       # index of the opening '{'
        depth, k = 0, s
        while k < n:
            ch = log_text[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        i = k + 1
        try:
            u = json.loads(log_text[s:k + 1])
        except Exception:
            continue
        if "prompt_tokens" not in u and "completion_tokens" not in u:
            continue
        n_blocks += 1
        prompt += u.get("prompt_tokens", 0) or 0
        completion += u.get("completion_tokens", 0) or 0
        total += u.get("total_tokens", 0) or 0
        cached += ((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        created += ((u.get("prompt_tokens_details") or {}).get("cache_creation_tokens", 0) or 0)
    if not n_blocks:
        return None
    return {
        # `prompt_tokens` covers cache reads, cache WRITES and fresh input alike, so both
        # detail fields have to come out of it or the remainder is not fresh input.
        "input_tokens": max(prompt - cached - created, 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": created,
        "output_tokens": completion,
        "tokens_total": total if total else (prompt + completion),
        "n_api_calls": n_blocks,
    }


def read_log_dir(log_dir):
    """Concatenate every *.log under `log_dir` (a segment can write several)."""
    text = ""
    try:
        for fn in sorted(glob.glob(os.path.join(log_dir, "*.log"))):
            with open(fn, encoding="utf-8", errors="replace") as fh:
                text += fh.read()
    except OSError:
        pass
    return text


def turns_from_stream(stream_text):
    """Turn count = `assistant.turn_start` events, which is what the AgentEO study reports
    as `turns` for Copilot. The spiral envelope is learned in these units, so it must be
    counted the same way here."""
    turns = 0
    for ln in (stream_text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            if json.loads(ln).get("type") == "assistant.turn_start":
                turns += 1
        except Exception:
            continue
    return turns


def stream_output_tokens(stream_text):
    """Per-message output tokens summed off the JSON stream. Kept only as a cross-check
    against the debug-log figure -- it is NOT a cost basis (no input/cache)."""
    tot = 0
    for ln in (stream_text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        d = ev.get("data") or {}
        for k in ("outputTokens", "output_tokens"):
            if isinstance(d.get(k), int):
                tot += d[k]
    return tot


def row_usage(stream_text, log_text):
    """Token-breakdown fields for a row, in the names `pricing.effective_tokens` reads.

    Falls back to the stream's output-token estimate when the debug log yielded nothing
    (a crashed segment, or a log dir that could not be read) and marks which source was
    used, so a row that is cheap because accounting failed is distinguishable from a row
    that is cheap because the task was easy.
    """
    usage = sum_openai_usage(log_text)
    turns = turns_from_stream(stream_text)
    if usage:
        n_calls = usage.pop("n_api_calls", None)
        row = dict(usage)
        row["num_turns"] = turns
        row["tokens_source"] = "debug_log"
        row["n_api_calls"] = n_calls
        row["stream_output_tokens"] = stream_output_tokens(stream_text)
        return row
    return {
        "input_tokens": None, "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "output_tokens": stream_output_tokens(stream_text) or None,
        "tokens_total": None,
        "num_turns": turns,
        "tokens_source": "stream_output_only",
        "n_api_calls": None,
    }


def merge_usage(a, b):
    """Add b's token counts into a's -- the spiral path runs several segments per task and
    the row must carry their sum. None + number keeps the number; None + None stays None,
    so an unaccounted segment does not silently become a zero."""
    if not a:
        return dict(b or {})
    if not b:
        return dict(a)
    out = dict(a)
    for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
              "output_tokens", "tokens_total", "num_turns", "n_api_calls",
              "stream_output_tokens"):
        x, y = a.get(k), b.get(k)
        out[k] = y if x is None else (x if y is None else x + y)
    # "debug_log" only if BOTH segments were accounted that way; otherwise the row is mixed
    # and must say so rather than claim the stronger basis.
    out["tokens_source"] = ("debug_log"
                            if a.get("tokens_source") == b.get("tokens_source") == "debug_log"
                            else "mixed")
    return out


# --------------------------------------------------------------------------------------
# the paid optimizer call
# --------------------------------------------------------------------------------------
def optimizer_call(prompt, *, binary=None, model=None, timeout=180):
    """One between-task proposer / judge / nudge-writer call through `copilot -p`.

    Returns (text, effective_tokens) -- the same contract as the Anthropic path, so the
    mechanisms and the ledger need no Copilot special-casing. `effective_tokens` is
    cache-aware and priced on the SAME weight table the task rows use, so mechanism
    overhead lands in the ledger on the task cost's own scale.

    Run as a PURE TEXT completion: a neutral temp cwd, `--no-custom-instructions`, and
    every tool both DENIED and EXCLUDED (see OPTIMIZER_EXCLUDE_TOOLS -- excluding is what
    keeps the tool schema out of the prompt, and so out of the ledger). Two reasons for the
    isolation -- it must not be contaminated by whatever repo the orchestrator happens to
    be sitting in, and a proposer that could read the testbed would leak task content into
    the between-task manual.

    Never raises: a transport failure or hang returns ("", None), which the mechanisms read
    as "no verdict" and fall back to their LLM-free path, so one blip cannot kill a sweep.
    """
    binary = binary or copilot_bin()
    neutral = tempfile.mkdtemp(prefix="te_optimizer_cwd_")
    logdir = tempfile.mkdtemp(prefix="te_optimizer_log_")
    argv = [binary, "-p", prompt, "-C", neutral,
            "--output-format", "json", "--no-color",
            "--log-level", "all", "--log-dir", logdir,
            "--no-custom-instructions", "--disable-builtin-mcps"]
    for t in OPTIMIZER_DENY_TOOLS:
        argv += ["--deny-tool", t]
    for t in OPTIMIZER_EXCLUDE_TOOLS:
        argv += ["--excluded-tools", t]
    if model and str(model).lower() not in ("agent", "copilot"):
        argv += ["--model", str(model)]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                             env=dict(os.environ, **run_env())).stdout or ""
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return "", None
    finally:
        shutil.rmtree(neutral, ignore_errors=True)

    text = ""
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "assistant.message":
            text = (ev.get("data") or {}).get("content", "") or text
    usage = sum_openai_usage(read_log_dir(logdir))
    shutil.rmtree(logdir, ignore_errors=True)
    # Record the call the way the Anthropic path does, so between-task overhead is
    # attributable to a cell on BOTH agents rather than only on Claude. Imported here
    # rather than at module scope because swebench imports this module, not the reverse.
    try:
        from .swebench import _log_optimizer_call
        _log_optimizer_call(model, prompt, text, usage or {})
    except Exception:                       # observability must never break a run
        pass
    return text, (pricing.effective_tokens(usage) if usage else None)
