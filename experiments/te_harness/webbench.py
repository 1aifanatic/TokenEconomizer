"""Web-Bench task executor for TokenEconomizer: real Claude Code, real Playwright grade.

Web-Bench inverts SWE-bench's control flow. Its own Node evaluator owns the task loop --
copy the previous task's workspace forward, collect the current file set, ask the agent for
files, write them back, run the project's init/build, screenshot, then grade with the
project's real Playwright suite (cumulatively: `init` + `task-1..task-N`). The
economizer's driver owns ITS loop: per-task injection prefix, tool mask, spiral params, one
``run_task`` call per task.

This runner joins the two loops at the evaluator's own HTTP agent seam, so neither side is
reimplemented here. Per project it starts

  * a serialized HTTP server speaking the evaluator's agent contract
    (POST -> ``{type, task, files, error}``; reply -> ``{files, inputTokens, outputTokens}``), and
  * ``@web-bench/eval`` restricted to that one project, pointed at that server,

and then ``run_task(task)`` blocks on a rendezvous: it takes the evaluator's request for
that task, runs Claude Code over the request's files in a scratch workspace, answers with
the files that changed, and waits for the evaluator's verdict line for that task before
returning the row. The agent therefore sees exactly the files Web-Bench shows any agent,
and ``resolved`` is Web-Bench's own graded verdict.

Two consequences of the benchmark's contract are worth stating plainly, because they shape
what the economizer's mechanisms can observe:

  * The agent gets files, not an installed project. Web-Bench's own reference agent is a
    single files-in/files-out LLM call; there is no node_modules in the request and no test
    suite (the tests are the grader, and shipping them to the agent would leak the gold).
    Claude Code here is strictly more capable than the reference agent -- it can read, grep
    and write across the whole file set -- but it cannot build or run the project, so
    trajectories are shorter than SWE-bench's edit-test-fail loops. That is the benchmark,
    not a harness limitation, and it is why the `trimech-web` arm exists.
  * A Web-Bench stream is sequentially dependent and graded cumulatively, so in the
    evaluator's `sequential` task mode the project STOPS at the first task that fails every
    attempt. The driver still asks for the remaining tasks; those rows come back
    `resolved: false` with `stream_broken: true` and zero tokens -- unreached, not cheap.

Environment (all optional except the key; see the repo's .env / .env.example):
  ANTHROPIC_API_KEY      Anthropic API key. REQUIRED: the Claude lane bills
                         api.anthropic.com directly and nothing else. ANTHROPIC_BASE_URL
                         and ANTHROPIC_AUTH_TOKEN are not inputs, and are removed from the
                         agent's environment (swebench.resolve_anthropic_key,
                         swebench.agent_credential_env -- shared with that domain)
  ANTHROPIC_MODEL        agent model            (default claude-opus-5)
  (no optimizer-model knob: the between-task proposer/judge/nudge-writer runs on the
   AGENT'S OWN model, against the agent's own endpoint. TE_OPTIMIZER_MODEL is refused.)
  TE_CLAUDE_BIN          host claude binary     (default: resolved from `which claude`)
  TE_EFFORT_LEVEL        reasoning effort: low|medium|high|xhigh|max      (default high)
  TE_WEBBENCH_REPO       Web-Bench checkout supplying the evaluator + projects
                         (default: the parent of $TOKENECONOMIZER_WEBBENCH_PROJECTS)
  TE_NODE_BIN            node binary to run the evaluator with  (default `which node`)
  TE_WEBBENCH_RETRY      attempts per task, the evaluator's `retry`        (default 2)
                         Rows carry resolved_p1 (first attempt) AND resolved_p2 (any
                         attempt); Web-Bench's headline metric is pass@1.
  TE_WEBBENCH_MAX_WORKERS  Playwright workers per grade, MAX_TEST_WORKERS (default 4)
  TE_TASK_TIMEOUT        per-segment agent wall-clock cap, seconds  (default 280).
                         MUST stay under the evaluator's ~300s undici header timeout:
                         past it the evaluator has hung up, the answer is discarded and
                         a retry is burned -- and burning the last one ends the stream.
  TE_WEBBENCH_VERDICT_TIMEOUT  cap on one grade (build + tests), seconds  (default 1800)
  TE_WEBBENCH_START_TIMEOUT    cap on the evaluator reaching the next task (default 1800)
  TE_OPTIMIZER_MAX_TOKENS  output cap for optimizer calls (default 16384)
  TE_OPTIMIZER_LOG       where between-task optimizer calls are recorded
  TE_KEEP_WORKSPACES     '1' keeps the per-attempt agent workspaces for post-mortem
  TE_VERBOSE             '1' prints per-task progress
"""
import hashlib
import glob
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from te_bench import baselines as scaffolds_pkg
from tokeneconomizer import pricing
from te_bench.nudge_guard import NudgeGuard

from . import copilot
from . import codex
from te_bench import codex_spiral

from .guards import net_guard
from .swebench import (EFFORT_LEVELS, PROTECTED_TOOLS, _ANTHROPIC_DIRECT, _log,
                       answer_key_attempts,
                       _merge_usage, _row_from_result, anthropic_optimizer_call,
                       preflight_optimizer_endpoint, resolve_anthropic_key,
                       claude_bin, cli_version, guard_accepts,
                       set_optimizer_context, _cell_of, _UsageRecovery,
                       agent_credential_env)

# External tool baselines this adapter can host. Web-Bench runs the agent on the HOST with
# no per-task repo container and no Python source, so the SWE-bench set does not carry over
# wholesale:
#   rtk     -- YES. A static binary plus a PreToolUse hook; it rewrites `ls`/`find`/`grep`
#              and compresses their output before it reaches context, which needs neither a
#              container nor a language. The agent does issue those commands here.
#   repomap -- NO. Its generator walks `*.py` (tokeneconomizer/plugin/repomap_gen.py) and a Web-Bench
#              project is TypeScript/JavaScript, so the map would come back empty.
#   serena  -- NO. An MCP navigation server over a codebase; Web-Bench hands the agent a
#              file set with no installed project, and the projects are 1-140 files.
# scripts/sweep_plan.py SWEEP_ARMS_WEBBENCH_UNAVAILABLE must agree with this set;
# tests/test_sweep_arms.py asserts it.
WEBBENCH_SCAFFOLDS = frozenset({"rtk"})

# Directories and files that are never part of a Web-Bench answer: build output, installed
# dependencies, the evaluator's own scratch, and the assets tree the evaluator copies in
# itself (echoing a binary asset back as text would corrupt it).
_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".next", ".nuxt", ".svelte-kit", ".parcel-cache", ".turbo",
    "dist", "build", "out", "coverage", "test-results", "playwright-report", "eval",
    "assets", "__pycache__", ".pytest_cache", ".claude", ".copilot",
})
_SKIP_FILES = frozenset({
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", ".DS_Store",
})
# A Web-Bench source file is small. The cap is a guard against an agent dropping a build
# artifact or a database into the workspace, not a limit on legitimate answers.
_MAX_FILE_BYTES = 512 * 1024

# `[table] task-3 2nd Attempt ❌: Task 41.2s Request 33.1s Test 8.1s`
# The evaluator logs this line once per attempt, from a `finally`-equivalent position, so
# every attempt produces exactly one Started line and exactly one verdict line.
_ATTEMPT_RE = re.compile(
    r"^\[(?P<project>[^\]]+)\]\s+(?P<task>init|task-\d+)\s+(?P<ord>\d+)(?:st|nd|rd|th)\s+"
    r"Attempt\s+(?P<mark>Started|✅|❌)")

_MARK_OK = "✅"

# Evaluator sessions this process started and has not yet shut down. A detached launcher
# stops the run with `kill -TERM -<pid>` on the process group, but the node evaluator
# spawns Playwright and `serve` children of its own, so the handler below closes the group
# explicitly rather than leaving web servers bound to ports.
_LIVE_SESSIONS = set()


def _cleanup_sessions(*_a):
    for sess in list(_LIVE_SESSIONS):
        try:
            sess.close()
        except Exception:
            pass


def _install_signal_cleanup():
    """Shut evaluator sessions down on TERM/INT/HUP, then die of that same signal so the
    exit status stays honest. No-op off the main thread."""
    def handler(signum, frame):
        _cleanup_sessions()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _session_log_text_host(home):
    """Claude Code's session log under a HOST-side per-task `$HOME`, or '' if unreadable.

    The Web-Bench twin of `swebench._session_log_text`: same file, same purpose, no
    container in the way. Globbed rather than composed from a slug -- the CLI derives the
    project directory from cwd and that derivation is its own business. Several session
    files can exist (the guard re-invokes the CLI per attempt); all are concatenated and
    `session_output_tokens` deduplicates by message id, so the overlap cannot double-count.

    Never raises: an unreadable log means the row keeps `output_tokens=0` and stays flagged,
    which is the pre-existing, honest behaviour.
    """
    try:
        parts = []
        for p in sorted(glob.glob(os.path.join(home, ".claude", "projects", "*", "*.jsonl"))):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    parts.append(fh.read())
            except OSError:
                continue
        return "\n".join(parts)
    except Exception as e:
        _log(f"session log unreadable ({type(e).__name__}: {e})")
        return ""


def webbench_repo():
    """The Web-Bench checkout root: TE_WEBBENCH_REPO, else the parent of the projects tree
    the economizer's own adapter is already pointed at (so one env var configures both).

    Returned REALPATH-resolved, and that matters. Every path the evaluator derives comes
    from the entry script's `__dirname`, and it hands the project root to the project's own
    dev server through EVAL_PROJECT_ROOT -- while node reports `process.cwd()` already
    resolved. Reached through a symlinked home (`/home/x` -> `/srv/home/x`), the two
    disagree, and Vite then fails to resolve the entry module of the `react` project
    against a root that is not the root it thinks it is: every test fails with an app that
    never rendered, which reads as an agent that wrote nothing. Canonicalizing here keeps
    the whole chain on one spelling of the path.
    """
    explicit = os.environ.get("TE_WEBBENCH_REPO")
    if explicit:
        return os.path.realpath(explicit)
    projects = os.environ.get("TOKENECONOMIZER_WEBBENCH_PROJECTS")
    if projects:
        return os.path.realpath(os.path.dirname(projects.rstrip("/")))
    raise RuntimeError(
        "no Web-Bench checkout: set TE_WEBBENCH_REPO or "
        "TOKENECONOMIZER_WEBBENCH_PROJECTS (see the repo's .env.example)")


def node_bin():
    explicit = os.environ.get("TE_NODE_BIN")
    if explicit:
        return explicit
    found = shutil.which("node")
    if not found:
        raise RuntimeError("no `node` on PATH and TE_NODE_BIN is unset; the Web-Bench "
                           "evaluator is a Node program")
    return found


def _load_ignore_globs(repo, project_dir):
    """The ignore patterns the evaluator itself applies when it collects task context.

    Read from the same four files its IgnoreGetter reads, so the set of files we hand back
    matches the set it would have shown the agent -- an answer file the evaluator ignores is
    an answer that silently does nothing.
    """
    globs = []
    for path in (os.path.join(project_dir, ".gitignore"),
                 os.path.join(project_dir, ".npmignore"),
                 os.path.join(repo, ".gitignore"),
                 os.path.join(project_dir, ".evalignore")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        globs.append(line.rstrip("/"))
        except OSError:
            continue
    return globs


def _ancestors(rel):
    """Every directory prefix of a relative path, as forward-slash strings."""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]


def _ignored(rel, globs):
    """A deliberately small gitignore subset: bare names and simple suffix globs, matched
    against every path segment. The hard-coded _SKIP_DIRS carry the cases that matter; this
    only adds whatever the project itself declared."""
    import fnmatch
    parts = rel.split("/")
    for pat in globs:
        pat = pat.lstrip("/")
        if not pat:
            continue
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
        if fnmatch.fnmatch(rel, pat):
            return True
    return False


# Tools that reach the public internet. Both benchmarks' answers are ON the public
# internet -- SWE-bench instances are GitHub issues whose fixing PR and gold patch are a
# fetch away, and Web-Bench ships every project's grading spec in `bytedance/web-bench`.
# An agent that can browse is therefore not solving the task, it is retrieving the answer,
# and the resolve verdict measures nothing.
#
# This is NOT hypothetical and NOT historical. Measured 2026-09-07: across the live
# claude-opus-5 / claude-sonnet-5 trees, 32 tasks reached the network and 14 pulled
# something patch-shaped -- `sympy/pulls/16597/files`, `django/commit/2cbd3967.patch`,
# `sphinx-doc/commit/0eff5818.patch`, `svg-chart/test/task-4.spec.js` -- and 13 of those
# scored RESOLVED. The published claude-opus-4.8 trees are clean (0 of 800 transcripts),
# so this is a model-behaviour change, not a harness regression: the older model simply
# never reached for the network, and nothing ever stopped it.
#
# Blocked for EVERY arm, deliberately. Leaving it to the untool mechanism would be worse
# than useless: untool drops a tool only after observing DISUSE, so precisely the agents
# that browse keep the ability, and the arms would differ in whether they can cheat.
# `TE_ALLOW_NETWORK_TOOLS=1` lifts it for a run that genuinely wants a browsing agent.
# The evaluator's `fetch` aborts a request after undici's default 300s headersTimeout
# (measured 2026-09-07 on node v22.23.2: UND_ERR_HEADERS_TIMEOUT at 300.9s). Past that the
# evaluator has already hung up, so our answer lands on a closed socket, the attempt is LOST
# rather than merely slow, a retry is burned, and burning the last one ends the whole stream.
# There is therefore NO Web-Bench configuration above ~300s that can help: a larger cap does
# not buy the agent time, it only converts a slow task into a destroyed one.
#
# So this CLAMPS rather than trusting the environment. `TE_TASK_TIMEOUT` is shared with
# SWE-bench, where 3600 is correct and necessary (sympy's slowest instances pinned 1800 dead
# on), and that value silently truncated two Web-Bench streams at task-14 before the clamp
# existed. `TE_WEBBENCH_TASK_TIMEOUT` overrides it for this domain only.
#
# The cap is COOPERATIVE -- checked between lines of the CLI's stream, so a turn already in
# flight runs to completion. The ceiling is therefore `cap + longest single turn`, measured
# at +16s on claude-opus-4.8 and +21s on claude-opus-5. So the ceiling must sit well below
# 300, not just under it: the default leaves ~60s for the overshoot and the hard max ~40s.
# A 290 max would have allowed 290+21 = 311s, i.e. straight back over the wall.
# 2026-09-21: the "no configuration above ~300s can help" above is true of WEB-BENCH's
# config but NOT of Node's. `HttpAgent.request` (tools/http-agent) calls bare `fetch`, so it
# uses undici's GLOBAL dispatcher, and that dispatcher's headersTimeout is settable from
# outside the vendored benchmark via `--import` (webbench_undici_preload.mjs). Raising it is
# opt-in through `TE_WEBBENCH_UNDICI_ABORT`; unset, everything below is byte-identical to
# the old fixed 300/240/260. Rows taken at different ceilings are NOT comparable -- a run
# that raised it completes tasks a 300s run would have destroyed.
WEBBENCH_UNDICI_ABORT_DEFAULT_S = 300
WEBBENCH_TASK_TIMEOUT_HEADROOM_S = 40      # cooperative-cap overshoot (+21s worst) + margin
WEBBENCH_TASK_TIMEOUT_DEFAULT = 240
# Back-compat names for the STOCK ceiling: the invariant they encode (cap < abort) is
# still the contract, it is now just evaluated per-run by the two helpers below.
WEBBENCH_UNDICI_ABORT_S = WEBBENCH_UNDICI_ABORT_DEFAULT_S
WEBBENCH_TASK_TIMEOUT_MAX = (WEBBENCH_UNDICI_ABORT_DEFAULT_S
                             - WEBBENCH_TASK_TIMEOUT_HEADROOM_S)
UNDICI_PRELOAD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "webbench_undici_preload.mjs")


def webbench_undici_abort(env=None):
    """The evaluator's request-abort ceiling in seconds (undici headersTimeout)."""
    env = os.environ if env is None else env
    try:
        value = int(env.get("TE_WEBBENCH_UNDICI_ABORT")
                    or WEBBENCH_UNDICI_ABORT_DEFAULT_S)
    except (TypeError, ValueError):
        value = WEBBENCH_UNDICI_ABORT_DEFAULT_S
    return max(60, value)


def webbench_undici_env(env=None, repo=None):
    """Env additions that make the evaluator honour a RAISED abort ceiling.

    Empty when the ceiling is the stock 300s, so the default spawn is unchanged.
    """
    env = os.environ if env is None else env
    abort = webbench_undici_abort(env)
    if abort <= WEBBENCH_UNDICI_ABORT_DEFAULT_S:
        return {}
    opt = "--import " + Path(UNDICI_PRELOAD).as_uri()
    prev = env.get("NODE_OPTIONS", "")
    return {"NODE_OPTIONS": (prev + " " + opt).strip() if prev else opt,
            "TE_WEBBENCH_UNDICI_ABORT_MS": str(abort * 1000),
            "TE_WEBBENCH_REPO": str(repo or env.get("TOKENECONOMIZER_WEBBENCH_REPO", ""))}


def _shq(s):
    """POSIX shell quoting for the one path the namespace wrapper interpolates."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def webbench_task_timeout(env=None):
    """Per-segment cap for Web-Bench, clamped under the evaluator's abort. Never raises."""
    env = os.environ if env is None else env
    raw = env.get("TE_WEBBENCH_TASK_TIMEOUT") or env.get("TE_TASK_TIMEOUT")
    try:
        value = int(raw) if raw else WEBBENCH_TASK_TIMEOUT_DEFAULT
    except (TypeError, ValueError):
        value = WEBBENCH_TASK_TIMEOUT_DEFAULT
    abort = webbench_undici_abort(env)
    cap = abort - WEBBENCH_TASK_TIMEOUT_HEADROOM_S
    if value > cap:
        _log(f"webbench: TE_TASK_TIMEOUT={value}s is above the evaluator's "
             f"{abort}s request abort, which would lose whole attempts and "
             f"truncate the stream -- clamping to {cap}s. Set "
             f"TE_WEBBENCH_TASK_TIMEOUT to choose a lower value for this domain.")
        value = cap
    return max(1, value)


NETWORK_TOOLS = ("WebSearch", "WebFetch")


def network_tool_block():
    """The network tools to add to `--disallowed-tools`, or () when explicitly allowed."""
    if os.environ.get("TE_ALLOW_NETWORK_TOOLS", "").strip() in ("1", "true", "yes"):
        return ()
    return NETWORK_TOOLS


class _AgentHandler(BaseHTTPRequestHandler):
    """The evaluator's agent endpoint. One request in flight at a time: the evaluator is
    configured `sequential` with `httpLimit: 1`, and the economizer's driver is a single
    thread, so serializing here turns a concurrency bug into a visible timeout."""

    session = None                      # set by the factory below

    def do_POST(self):
        try:
            n = int(self.headers.get("content-length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            self.send_error(400, f"bad agent request: {e}")
            return
        reply_q = queue.Queue(maxsize=1)
        self.session.requests.put((req, reply_q))
        try:
            payload = reply_q.get(timeout=self.session.reply_timeout)
        except queue.Empty:
            # The driver never answered. Returning an empty file set lets the evaluator
            # fail this task honestly instead of hanging the whole stream.
            _log("webbench: no answer for an agent request within "
                 f"{self.session.reply_timeout}s -- replying with no files")
            payload = {"files": {}}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # The evaluator hung up before we answered -- it waits ~300s (undici header
            # timeout) and this segment outran it. Say so in one line: the bare socketserver
            # traceback names neither the cause nor the consequence, and the consequence is
            # severe (answer discarded, retry burned, stream possibly ended). Re-raising
            # would only reprint that traceback, so swallow it after logging.
            _log("webbench: the evaluator hung up before this answer was written "
                 "(BrokenPipeError) -- the segment outran its ~300s request budget, so this "
                 "attempt is LOST, not merely slow. Lower TE_TASK_TIMEOUT (default 280) so "
                 "the segment is cut before the evaluator gives up.")

    def log_message(self, *_a):                  # keep the evaluator's stdout readable
        pass


class _EvalSession:
    """One project's evaluator run: the agent endpoint plus the `@web-bench/eval` process.

    The session is the shared state the rendezvous needs -- a request queue fed by the HTTP
    handler and an event queue fed by a reader thread parsing the evaluator's stdout for
    its per-attempt Started/verdict lines.
    """

    def __init__(self, project, repo, session_dir, *, retry, tasks, node,
                 reply_timeout, log_level="info", max_workers=4):
        self.project = project
        self.repo = repo
        self.dir = session_dir
        self.retry = retry
        self.reply_timeout = reply_timeout
        self.requests = queue.Queue()
        self.events = queue.Queue()
        self.finished = False
        self.exit_code = None
        self.hash = time.strftime("%Y%m%d-%H%M%S")
        os.makedirs(self.dir, exist_ok=True)
        self.log_path = os.path.join(self.dir, "evaluator.log")

        # 1. the agent endpoint
        handler = type("_BoundAgentHandler", (_AgentHandler,), {"session": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self._http_thread = threading.Thread(target=self.httpd.serve_forever,
                                             kwargs={"poll_interval": 0.2}, daemon=True)
        self._http_thread.start()

        # 2. the evaluator config. `--without-local-config` keeps apps/eval/src/config.json5
        #    out of it, so the run is fully described by this file.
        full = self._full_task_ids()
        cfg = {
            "models": [],
            "projects": [f"@web-bench/{project}"],
            "agentMode": "http",
            "agentEndPoint": f"http://127.0.0.1:{self.port}/message",
            "httpLimit": 1,
            "maxdop": 1,
            "taskMode": "sequential",
            "retry": retry,
            "logLevel": log_level,
            "production": True,
            "hash": self.hash,
            "agentDir": self.dir,
        }
        # Restricting the task range flips the evaluator out of production mode, which
        # tightens Playwright's timeouts from 30s to 2s and would fail tasks for being slow
        # rather than wrong. Only narrow the range when the driver really asked for a
        # subset, and say so.
        ids = [t.get("instance_id") for t in tasks]
        if ids and full and ids != full:
            cfg["startTask"], cfg["endTask"] = ids[0], ids[-1]
            _log(f"webbench {project}: task range {ids[0]}..{ids[-1]} is a SUBSET of the "
                 f"{len(full)}-task stream -- the evaluator drops to development mode "
                 f"(Playwright timeout 2s, not 30s), so grades are stricter than a full run")
        self.config_path = os.path.join(self.dir, "evaluator_config.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

        # 3. the evaluator process, in its own group so its Playwright/serve children die
        #    with it. NO_COLOR keeps the verdict lines parseable.
        entry = os.path.join(repo, "apps", "eval", "dist", "index.js")
        if not os.path.exists(entry):
            raise RuntimeError(
                f"the Web-Bench evaluator is not built: {entry} missing. Run "
                f"`rush build --to @web-bench/eval` in {repo}.")
        env = dict(os.environ)
        env.update({"NO_COLOR": "1", "FORCE_COLOR": "0",
                    "MAX_TEST_WORKERS": str(max_workers)})
        env.update(webbench_undici_env(env, repo=repo))
        self._log_fh = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [node, entry, "--auto-run", "--config", self.config_path,
             "--without-local-config"],
            cwd=os.path.join(repo, "apps", "eval"), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True)
        _LIVE_SESSIONS.add(self)
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        _log(f"webbench {project}: evaluator pid={self.proc.pid} endpoint=:{self.port} "
             f"retry={retry} log={self.log_path}")

    def _full_task_ids(self):
        """The project's whole stream, in order, as the evaluator reads it."""
        path = os.path.join(self.repo, "projects", self.project, "tasks.jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                out = []
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line).get("id"))
                return out
        except OSError:
            return []

    def _read_stdout(self):
        """Tee the evaluator's output to disk and lift its per-attempt lines onto the
        event queue. This is the grade channel: the evaluator prints exactly one
        `<task> <n>th Attempt ✅/❌` line per attempt."""
        for line in self.proc.stdout:
            self._log_fh.write(line)
            self._log_fh.flush()
            m = _ATTEMPT_RE.match(line.strip())
            if m:
                kind = "started" if m.group("mark") == "Started" else "verdict"
                self.events.put({
                    "kind": kind, "task": m.group("task"),
                    "attempt": int(m.group("ord")),
                    "ok": m.group("mark") == _MARK_OK,
                    "line": line.rstrip(),
                })
        self.proc.wait()
        self.exit_code = self.proc.returncode
        self.finished = True
        self.events.put({"kind": "exit", "code": self.exit_code})
        _log(f"webbench {self.project}: evaluator exited rc={self.exit_code}")

    def next_event(self, kind, timeout):
        """The next `kind` event, skipping the other kinds. Returns None when the evaluator
        exits first (a broken stream) or nothing arrives in time."""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            try:
                ev = self.events.get(timeout=min(left, 5.0))
            except queue.Empty:
                if self.finished:
                    return None
                continue
            if ev["kind"] == "exit":
                return None
            if ev["kind"] == kind:
                return ev
            # A verdict we were not waiting for means the loops have desynchronized;
            # surface it rather than silently pairing the next request with it.
            _log(f"webbench {self.project}: unexpected {ev['kind']} event "
                 f"{ev.get('task')} #{ev.get('attempt')} while waiting for {kind}")

    def next_request(self, timeout):
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None, None
            try:
                return self.requests.get(timeout=min(left, 5.0))
            except queue.Empty:
                if self.finished:
                    return None, None

    def close(self):
        _LIVE_SESSIONS.discard(self)
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        if self.proc.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(self.proc.pid), sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    self.proc.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            self._log_fh.close()
        except Exception:
            pass
        self.finished = True


class WebBenchRunner:
    """AgentRunner: Claude Code against Web-Bench, graded by Web-Bench's own evaluator."""

    def __init__(self, agent="claude", model=None, repo=None, scaffold=None):
        self.agent = agent
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
        # The Anthropic API directly, on ANTHROPIC_API_KEY -- one resolver, shared with the
        # SWE-bench runner so the two domains cannot drift apart about what they bill.
        self.api_key = resolve_anthropic_key()
        # The mechanisms' between-task LLM calls (proposer, tool judge, spiral judge, nudge
        # writer) run on THE AGENT'S OWN MODEL, always. Their cost is charged to the same
        # ledger as the task rows and compared against a baseline that made no such calls,
        # so a cheaper proposer would understate the overhead the arm actually incurs --
        # the economized arm would look better for having billed its own thinking at a
        # discount. One model per cell is also what the cell name claims. There is no knob:
        # TE_OPTIMIZER_MODEL used to be one and is now refused rather than half-honoured.
        if os.environ.get("TE_OPTIMIZER_MODEL"):
            raise RuntimeError(
                "TE_OPTIMIZER_MODEL is no longer honoured: the between-task optimizer "
                "runs on the agent's own model so mechanism overhead is priced on the "
                "same basis as the task rows. Unset it.")
        self.optimizer_model = self.model
        self.effort = (os.environ.get("TE_EFFORT_LEVEL")
                       or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL") or "high")
        if self.effort not in EFFORT_LEVELS:
            raise RuntimeError(f"TE_EFFORT_LEVEL={self.effort!r} is not one of "
                               f"{sorted(EFFORT_LEVELS)}")
        self.claude = claude_bin()
        # Same early pricing check the Copilot runners make: an unpriced model id otherwise
        # raises from pricing.effective_tokens at the END of run_task, after the rollout has
        # been paid for and graded. Claude ids resolve to the Claude curve, so this only
        # ever fires on a genuinely unknown id.
        pricing.weights_for({"model": self.model, "agent": self.agent})
        # Generic slots the shared lifecycle reads, so an agent subclass overrides only
        # launch + accounting rather than the whole evaluator round-trip.
        self.agent_bin = self.claude
        self.protected_tools = PROTECTED_TOOLS
        self.routing_base = _ANTHROPIC_DIRECT
        self.cli_version = cli_version(self.claude)
        self.repo = os.path.realpath(repo) if repo else webbench_repo()
        # An external tool baseline (rtk today). Resolved and preflighted HERE so a
        # misconfigured one kills the lane before task 0, exactly as on SWE-bench --
        # a lane of flagged rows is diagnosable, a lane of silently-native rows is not.
        self.scaffold_name = scaffold or None
        self.scaffold = scaffolds_pkg.get(self.scaffold_name)
        if self.scaffold is not None:
            self.scaffold.preflight(self.scaffold_profile_for("/nonexistent"))
            _log(f"scaffold: {self.scaffold.name} (external baseline)")
        self.node = node_bin()
        # Output cap for the between-task proposer / judge / nudge-writer call, same
        # default as the SWE-bench harness so mechanism overhead is comparable across
        # domains. This is the CLAUDE base class and `optimizer_call` below reads it, so
        # it must be set here: the Copilot subclass overrides it (that CLI has no output
        # cap flag of its own), and the stale comment this replaces claimed the Copilot
        # rationale for the base, leaving `self.optimizer_max_tokens` undefined. The
        # effect was an AttributeError on the FIRST proposer tick -- task 5 of every
        # stream -- so `inject-only` and `trimech-web` died early in each Claude Web-Bench
        # cell, and no LIMIT<5 pilot could reach it.
        self.optimizer_max_tokens = int(os.environ.get("TE_OPTIMIZER_MAX_TOKENS", "16384"))
        # Stated, not inferred: this harness has always used the Messages API for
        # mechanism calls, which is the transport the Copilot harness now matches.
        self.optimizer_transport = "api"
        # MUST stay under the evaluator's ~300s undici header timeout -- the same bound
        # CopilotWebBenchRunner documents, and it binds here for the identical reason.
        # A segment that outlives it is not merely slow: the evaluator has already hung up,
        # so our reply lands on a closed socket (BrokenPipeError out of do_POST), the answer
        # is DISCARDED, and the task is scored a pass@1 miss for a plumbing reason. Worse,
        # it burns a retry -- and when it burns the LAST retry the evaluator ends the whole
        # stream, so every later task comes back UNREACHED. Measured 2026-09-07 on
        # claude-opus-5: threejs/task-14 2nd attempt clipped at
        # Request 300.858s and took tasks 15-20 down with it; svg-chart hit the same wall
        # from task-6 on. The old 1800s default predated a model slow enough to reach it.
        self.task_timeout = webbench_task_timeout()
        self.retry = max(1, int(os.environ.get("TE_WEBBENCH_RETRY", "2")))
        self.max_workers = int(os.environ.get("TE_WEBBENCH_MAX_WORKERS", "4"))
        self.verdict_timeout = int(os.environ.get("TE_WEBBENCH_VERDICT_TIMEOUT", "1800"))
        self._init_stream_state()
        self._preflight_optimizer_endpoint()

    def _preflight_optimizer_endpoint(self):
        """Refuse the lane if the mechanism endpoint will not answer. Same check and same
        reasoning as the SWE-bench harness -- see the shared helper."""
        preflight_optimizer_endpoint(
            api_key=self.api_key, configured_model=self.optimizer_model, label="claude",
            remedy="Check that ANTHROPIC_API_KEY is valid and can call MODEL.")

    def _init_stream_state(self):
        """Evaluator-session state, shared by every agent subclass.

        Kept in one place deliberately: a subclass that reimplements __init__ and forgets
        one of these fails later with an AttributeError deep in begin_stream, so new stream
        state must be added HERE rather than inline above.
        """
        self.start_timeout = int(os.environ.get("TE_WEBBENCH_START_TIMEOUT", "1800"))
        self.keep_workspaces = os.environ.get("TE_KEEP_WORKSPACES") == "1"
        self._session = None
        self._session_project = None
        self._stream = []
        self._stream_broken = False
        self._current_task = None

    # ---- the paid optimizer call the mechanisms make between tasks ---------------------
    def optimizer_call(self, prompt, model="agent"):
        """One small Messages-API request. Returns (text, effective_tokens). Same call and
        same pricing basis as the SWE-bench harness, so mechanism overhead is comparable
        across domains."""
        return anthropic_optimizer_call(
            prompt, model=model, configured_model=self.optimizer_model,
            max_tokens=self.optimizer_max_tokens, api_key=self.api_key)

    # ---- stream lifecycle (the driver's optional begin_stream/end_stream seam) ---------
    def begin_stream(self, tasks, *, results_root=None, repo=None, **_info):
        """Start the project's evaluator before the first task.

        Web-Bench's evaluator owns the task loop, so it must be started once for the whole
        stream -- starting it per task would reset the workspace chain the benchmark is
        built on. Doing it here rather than lazily also means a misconfigured checkout
        fails before any task has been billed.
        """
        self._stream = list(tasks or [])
        project = ((repo or "").split("/")[-1]
                   or (self._stream[0].get("repo", "") if self._stream else ""))
        project = project.split("/")[-1]
        if not project:
            raise RuntimeError("webbench: no project to run; pass --repo <project>")
        if not os.path.isdir(os.path.join(self.repo, "projects", project)):
            raise RuntimeError(f"webbench: no such project "
                               f"{os.path.join(self.repo, 'projects', project)}")
        self.close_session()
        self._stream_broken = False
        session_dir = os.path.join(results_root or os.getcwd(), f"_webbench_{project}")
        self._session = _EvalSession(
            project, self.repo, session_dir, retry=self.retry, tasks=self._stream,
            node=self.node, reply_timeout=self.task_timeout * (self.retry + 2),
            max_workers=self.max_workers)
        self._session_project = project

    def end_stream(self):
        self.close_session()

    def session_for(self, project):
        """The live session, checked against the project the task claims to belong to."""
        if self._session is None:
            raise RuntimeError(
                "webbench: no evaluator session. The driver calls begin_stream() before "
                "the first task; a custom caller must do the same.")
        if self._session_project != project:
            raise RuntimeError(f"webbench: session is running {self._session_project!r} "
                               f"but the task belongs to {project!r}")
        return self._session

    def close_session(self):
        if self._session is not None:
            self._session.close()
            self._session = None
            self._session_project = None

    # ---- external tool scaffold (baseline arms only) ----------------------------------
    #
    # Web-Bench has NO per-task container: the agent runs on the host, in a scratch
    # workspace, under a per-task HOME. So the Docker-shaped handles the SWE-bench runner
    # hands a scaffold (`container`, `docker exec`, `docker cp`) become plain subprocess
    # and file-copy here. Everything else about a scaffold is unchanged, which is the point
    # of the seam: rtk installs the same pinned binary and registers the same PreToolUse
    # hook whether the "machine" is a container or this host.
    #
    # `read_bytes` is deliberately NOT provided. Only the repo-map baseline needs it (it
    # streams the repo out as a tar) and that baseline is Python-only, so a Web-Bench arm
    # asking for it should fail loudly rather than half-work -- ScaffoldContext.read_bytes
    # already raises a ScaffoldError saying exactly that.
    def agent_home(self, run_dir_path):
        """$HOME for the agent on THIS task-run. Per task, and never inside the workspace:
        everything under the workspace is a candidate answer file."""
        return os.path.join(run_dir_path, ".agent-home")

    def scaffold_profile_for(self, home):
        """Claude Code's profile, with the per-task HOME filled in.

        On SWE-bench this is a class attribute because the container's HOME is a fixed
        path; here it is per task-run, so it is built per call.

        `instructions_file` stays CLAUDE.md for parity, and `patch_excludes` carries it
        plus RTK.md -- on Web-Bench those names are what `_collect` must refuse to ship to
        the evaluator rather than what `git diff` must subtract. With `rtk init -g` both
        actually land under $HOME/.claude/ and never reach the workspace at all; the
        exclusion is the belt-and-braces half.
        """
        return scaffolds_pkg.AgentProfile(
            kind="claude",
            home=home,
            instructions_file="CLAUDE.md",
            nav_tools=("Read", "Grep", "Glob"),
            mcp_args=lambda cfg: ["--mcp-config", cfg],
            patch_excludes=("CLAUDE.md",),
            # No writable /usr/local on the host, so a scaffold installs into the agent's
            # own bin dir -- which _segment puts on the agent's PATH.
            bin_dir=self.scaffold_bin_dir(home))

    def _scaffold_context(self, workspace, home, task, run_dir_path):
        """The handles a scaffold may use. The host is reached only through these."""
        def _host(argv, timeout=300):
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

        def exec_(script, timeout=300, env=None):
            """Run a script AS THE AGENT WOULD: in the workspace, under the agent's HOME."""
            e = dict(os.environ)
            e["HOME"] = home
            e["PATH"] = os.pathsep.join([self.scaffold_bin_dir(home), e.get("PATH", "")])
            # Per-agent additions the install must see -- Copilot redirects COPILOT_HOME so
            # `copilot plugin install` cannot write into the operator's real config.
            e.update(self.scaffold_exec_env(run_dir_path))
            e.update({k: str(v) for k, v in (env or {}).items()})
            return subprocess.run(["bash", "-lc", script], cwd=workspace, env=e,
                                  capture_output=True, text=True, timeout=timeout)

        def _place(src, dst):
            """Hardlink if we can, else copy.

            A scaffold binary is staged once on the host and installed per task-run; rtk's
            is 10MB, which copied into every run dir is tens of GB across one arm and buys
            nothing, since nothing writes to it. A hardlink is the same inode, so it costs
            no space and cannot dangle the way a symlink would if the cache moved. Falls
            back to a copy across filesystems, which is the only case that can fail.
            """
            if os.path.lexists(dst):
                os.unlink(dst)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        def copy_in(host_path, dest_path):
            """`docker cp`'s host twin.

            Scaffolds address the install directory through `profile.bin_dir`, which is
            already a writable per-run path here, so the destination normally needs no
            change. A destination outside it would be a scaffold hardcoding a container
            path: re-root rather than fail, since the alternative is an install that
            silently no-ops and a row that reads `<tool>_installed: false`.
            """
            bin_dir = self.scaffold_bin_dir(home)
            try:
                # Honour the destination as given. On the host most absolute paths a
                # scaffold names (a /tmp helper, anything under the agent's own HOME) are
                # perfectly writable, and rewriting them would break the very next command,
                # which refers to the path it asked for.
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                _place(host_path, dest_path)
                dest = dest_path
            except OSError:
                # Not writable off-container -- /usr/local/bin on an unprivileged host is
                # the case this exists for. Re-root into the agent's own bin dir, which is
                # on its PATH, rather than letting the install silently no-op.
                dest = os.path.join(bin_dir, os.path.basename(dest_path))
                _log(f"scaffold copy_in: {dest_path} not writable; using {dest}")
                os.makedirs(bin_dir, exist_ok=True)
                _place(host_path, dest)
            os.chmod(dest, 0o755)
            return subprocess.CompletedProcess([], 0, "", "")

        return scaffolds_pkg.ScaffoldContext(
            profile=self.scaffold_profile_for(home), container=None, task=task,
            run_dir=run_dir_path, exec_=exec_, copy_in=copy_in, host_run=_host,
            read_bytes=None, log=_log)

    @staticmethod
    def scaffold_bin_dir(home):
        """Where copy_in lands executables, and what exec_/the agent get on PATH."""
        return os.path.join(home, ".local", "bin")

    def scaffold_exec_env(self, run_dir_path):
        """Extra environment a scaffold's install commands need. Empty for Claude."""
        return {}

    def prepare_agent_home(self, home):
        """Seed the per-task agent HOME before a scaffold installs into it. No-op here."""
        return

    @staticmethod
    def _net_guard_log(home):
        """Where the guard appends its refusals for this task."""
        return os.path.join(home, "net_guard_blocks.jsonl")

    def _net_guard_blocks(self, run_dir_path):
        """Count the guard's refusals for this task. Absent log = it never fired."""
        path = self._net_guard_log(self.agent_home(run_dir_path))
        try:
            with open(path, encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0

    def net_guard_settings_path(self, home, run_dir_path):
        """Where THIS agent reads PreToolUse hooks from. Claude: `$HOME/.claude`."""
        return os.path.join(home, ".claude", "settings.json")

    def net_guard_hook_entry(self, command):
        """Claude Code's PreToolUse entry shape: nested `hooks`, matcher `Bash`."""
        return {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}

    # Claude nests its list under `PreToolUse`; Copilot under `preToolUse`.
    NET_GUARD_HOOK_KEY = "PreToolUse"

    def _install_net_guard(self, home, run_dir_path=None):
        """Register the PreToolUse guard for this task. True when it is actually armed.

        Written per task into the SAME settings file a scaffold would use, so rtk's hook
        and this one coexist -- both land in the hook list rather than overwriting each
        other. Applies to EVERY arm: an agent that can read the grading spec makes the
        resolve verdict meaningless, which is not an arm-level concern.

        **Returns a bool now, and the row records it.** It used to return a log path and
        never raise, so the Copilot subclass -- which inherited it unchanged and therefore
        wrote a Claude-shaped `.claude/settings.json` the Copilot CLI does not read --
        failed *silently*: `os.makedirs` and the write both succeeded, nothing was armed,
        and the row carried only `net_guard_blocks: 0`, which is indistinguishable from
        "armed and never fired". Confirmed on a live copilot/calculator run 2026-09-08.
        """
        path = self.net_guard_settings_path(home, run_dir_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as fh:
                settings = json.load(fh)
        except Exception:
            settings = {}
        # An ABSOLUTE script path, not `-m te_harness.guards.net_guard`: the hook is executed
        # with the agent's workspace as cwd and nothing puts this repo on sys.path there, so
        # the module spelling would die with ImportError on every call. A hook that always
        # errors is worse than none -- the CLI reports a failing hook while the agent keeps
        # going, so the run looks guarded and is not. `net_guard` imports only the stdlib
        # precisely so it can be run as a loose file.
        entry = self.net_guard_hook_entry(f"{sys.executable} {net_guard.__file__}")
        hooks = settings.setdefault("hooks", {})
        pre = hooks.setdefault(self.NET_GUARD_HOOK_KEY, [])
        if not any("net_guard" in json.dumps(h) for h in pre):
            pre.append(entry)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
        return self._verify_net_guard(home)

    # One the guard MUST refuse, and which reaches no network even unguarded: `decide`
    # matches the substring `github` alone, so `echo` exercises the whole path.
    NET_GUARD_PROBE = "echo https://github.com/probe"

    def _verify_net_guard(self, home):
        """Feed the guard a payload of THIS agent's shape; True only if it refuses.

        Writing the settings file is not evidence of anything -- it succeeds on an agent
        that never reads it. What can still be wrong after a successful write is the
        payload spelling (`tool_input` vs `toolArgs`), the interpreter, or the script
        path, and all three are exercised here. What this does NOT prove is that the CLI
        invokes the hook at all; the file location, the hook key and the matcher name are
        what carry that, and each is now set per agent rather than inherited.
        """
        probe = json.dumps({"tool_name": "Bash", "toolName": "bash",
                            "tool_input": {"command": self.NET_GUARD_PROBE},
                            "toolArgs": {"command": self.NET_GUARD_PROBE}})
        env = dict(os.environ)
        env[net_guard.LOG_ENV] = self._net_guard_log(home)
        try:
            r = subprocess.run([sys.executable, net_guard.__file__], input=probe,
                               capture_output=True, text=True, timeout=60, env=env)
        except Exception as e:
            _log(f"net guard verify FAILED ({type(e).__name__}: {e}) -- row runs UNGUARDED")
            return False
        if r.returncode != 2:
            _log(f"net guard installed but did NOT refuse its probe (rc={r.returncode}) "
                 "-- row runs UNGUARDED")
            return False
        # The probe's own block record is telemetry about the harness, not the agent, so
        # it must not inflate the row's `net_guard_blocks`.
        try:
            os.remove(self._net_guard_log(home))
        except OSError:
            pass
        return True

    def _apply_scaffold(self, workspace, home, task, run_dir_path):
        """Install the arm's scaffold for this task. Returns a notes dict.

        A scaffold that raises is recorded and the task runs NATIVE, exactly as on
        SWE-bench: one broken install must not cost the row, because a row flagged
        `scaffold_error` is diagnosable while a missing row is not.
        """
        if self.scaffold is None:
            return {}
        os.makedirs(self.scaffold_bin_dir(home), exist_ok=True)
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        self.prepare_agent_home(home)
        try:
            return self.scaffold.setup(
                self._scaffold_context(workspace, home, task, run_dir_path))
        except Exception as e:                                    # noqa: BLE001
            _log(f"{task['instance_id']}: scaffold {self.scaffold.name} FAILED "
                 f"({type(e).__name__}: {e}) -- task runs native, row flagged")
            return {"arm_setup": self.scaffold.name,
                    "scaffold_error": f"{type(e).__name__}: {e}"[:300]}

    def scaffold_answer_excludes(self):
        """Names `_collect` must never ship to the evaluator.

        Web-Bench's analogue of SWE-bench's `patch_excludes`: there the scaffold's files
        are subtracted from the graded diff, here they must be kept out of the answer the
        evaluator's Writer applies. With no scaffold this is empty and `_collect` behaves
        byte-identically to the pre-scaffold form.
        """
        if self.scaffold is None:
            return frozenset()
        names = set(getattr(self.scaffold, "patch_excludes", ()) or ())
        names |= set(self.scaffold_profile_for("/nonexistent").patch_excludes)
        return frozenset(n for n in names if "*" not in n)

    # ---- one streamed agent segment ---------------------------------------------------
    def _segment(self, workspace, prompt, transcript_fh, exclude_tools, max_turns, guard):
        """Run `claude -p` once in `workspace`, streaming stream-json. Every raw line is
        appended to the transcript (the schema `tokeneconomizer.trace` re-reads) and, when
        the spiral guard is active, folded into it live; a guard stop kills the agent,
        leaving its edits in the workspace for the next segment."""
        cmd = [self.claude, "-p", "--output-format", "stream-json", "--verbose",
               "--dangerously-skip-permissions",
               "--model", self.model, "--max-turns", str(max_turns),
               "--effort", self.effort]
        drop = [t for t in (exclude_tools or []) if t not in self.protected_tools]
        # Network tools are dropped for every arm, on top of whatever untool decided --
        # see NETWORK_TOOLS: the answers to both benchmarks are a fetch away.
        drop += [t for t in network_tool_block() if t not in drop]
        if drop:
            cmd += ["--disallowed-tools", ",".join(drop)]

        env = dict(os.environ)
        # Keep the CLI's own state OUT of the workspace: everything under the workspace is
        # a candidate answer file, and the agent's home is not part of the project. This is
        # the SAME path `agent_home` hands the scaffold, so an installed hook and the agent
        # that must fire it agree on where settings.json lives.
        home = self.agent_home(os.path.dirname(workspace))
        env.update({"HOME": home,
                    net_guard.LOG_ENV: self._net_guard_log(home),
                    "CLAUDE_CODE_EFFORT_LEVEL": self.effort,
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1"})
        # The agent runs on the HOST and inherits the operator's whole shell, so it gets
        # exactly one Anthropic credential and nothing that could point it at another
        # endpoint or model catalog -- see swebench.agent_credential_env.
        agent_credential_env(env, api_key=self.api_key)
        os.makedirs(env["HOME"], exist_ok=True)
        # A scaffold copies its binary here (copy_in re-roots absolute paths into it), so
        # the agent's own shell must find it. Harmless and empty when no scaffold ran.
        env["PATH"] = os.pathsep.join([self.scaffold_bin_dir(home), env.get("PATH", "")])

        proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, start_new_session=True)
        proc.stdin.write(prompt)
        proc.stdin.close()

        result, stopped, deadline = {}, False, time.time() + self.task_timeout
        # Same accounting hazard as the SWE-bench runner, and the same fix: the break paths
        # below kill the CLI before its terminal `result` event. See
        # swebench._UsageRecovery. It bites harder here -- the guard is re-made per ATTEMPT
        # (`guard_per_attempt`), so there are more chances to stop mid-stream per task.
        recovery, stop_reason = _UsageRecovery(), None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            transcript_fh.write(line + "\n")
            transcript_fh.flush()
            try:
                ev = json.loads(line)
            except Exception:
                ev = {}
            recovery.observe(ev)
            if ev.get("type") == "result":
                result = ev
            if guard is not None and guard.monitor(line):
                stopped, stop_reason = True, "spiral_guard_stop"
                break
            if time.time() > deadline:
                _log("webbench segment hit TE_TASK_TIMEOUT; stopping the agent")
                stopped, stop_reason = True, "task_timeout"
                break

        if stopped:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(2)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.kill()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = (proc.stderr.read() or "")[-600:]
        if err.strip() and not result:
            _log(f"webbench agent stderr: {err.strip()[:300]}")
        if not result:
            # Same fix as the SWE-bench runner: `output_tokens` survives the kill in the
            # CLI's own session log. Here the agent runs on the HOST with $HOME relocated
            # per task, so it is a plain read rather than a `docker exec` (swebench
            # `_session_log_text`). It bites harder on this runner -- the guard is re-made
            # per ATTEMPT -- so more rows per task stood to be flagged.
            result = recovery.synthesize(stop_reason or "no_result_event",
                                         _session_log_text_host(home))
        return result

    # ---- workspace <-> file-map plumbing ----------------------------------------------
    def _materialize(self, workspace, files):
        """Write the evaluator's file map into a fresh scratch workspace."""
        if os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)
        os.makedirs(workspace, exist_ok=True)
        for rel, content in (files or {}).items():
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue                       # never let a task's file map escape the ws
            path = os.path.join(workspace, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content if isinstance(content, str) else "")

    def _collect(self, workspace, request_files, project_dir):
        """The answer: every file whose content differs from what the request supplied.

        Only changed and new files are returned, which is what the evaluator's Writer
        applies. Deletion is not expressible in Web-Bench's contract (an empty answer
        leaves an existing file alone), so a file the agent removed is reported in the row
        rather than silently dropped.
        """
        globs = _load_ignore_globs(self.repo, project_dir)
        supplied = set(request_files or {})
        # A scaffold's own artifacts are never part of the agent's answer. Empty with no
        # scaffold, so the collected set is byte-identical to the pre-scaffold form. Only
        # files the REQUEST did not supply are dropped: if a project genuinely ships a file
        # by one of these names, it stays the agent's to edit.
        scaffold_files = self.scaffold_answer_excludes()
        # Directories the request itself populated are legitimate no matter what they are
        # called, so a project that really ships a dot-directory is not silently pruned.
        supplied_dirs = {d for rel in supplied
                         for d in _ancestors(rel)}
        out, seen, skipped = {}, set(), []
        for root, dirs, names in os.walk(workspace):
            base = os.path.relpath(root, workspace).replace(os.sep, "/")
            base = "" if base == "." else base
            def _keep_dir(d, base=base):
                rel = f"{base}/{d}" if base else d
                if rel in supplied_dirs:
                    return True
                return d not in _SKIP_DIRS and not d.startswith(".")
            dirs[:] = [d for d in dirs if _keep_dir(d)]
            for name in names:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, workspace).replace(os.sep, "/")
                if rel not in supplied:
                    # New files are held to the skip rules; a dotfile the agent invented is
                    # scratch, while one the request supplied is project source.
                    if name in _SKIP_FILES or name.startswith("."):
                        continue
                    if rel in scaffold_files or name in scaffold_files:
                        skipped.append(rel)
                        continue
                    if _ignored(rel, globs):
                        # The evaluator's own ignore set would discard this answer, so
                        # sending it would be a no-op. Record it instead of dropping it
                        # silently: a project that hides its entry point from the agent
                        # (react's .evalignore hides index.html) makes an agent recreate it,
                        # and that wasted work should be visible in the row.
                        skipped.append(f"{rel} (evaluator ignores it)")
                        continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size > _MAX_FILE_BYTES:
                    skipped.append(f"{rel} ({size}B > cap)")
                    continue
                try:
                    with open(path, encoding="utf-8") as fh:
                        content = fh.read()
                except (OSError, UnicodeDecodeError):
                    skipped.append(f"{rel} (unreadable/binary)")
                    continue
                seen.add(rel)
                if (request_files or {}).get(rel) != content:
                    out[rel] = content
        deleted = sorted(set(request_files or {}) - seen)
        return out, deleted, skipped

    # ---- the AgentRunner entry point --------------------------------------------------
    def run_task(self, task, *, arm, injected, exclude_tools, run_dir_path, **run_kwargs):
        task_id = self._current_task = task["instance_id"]
        set_optimizer_context(arm=arm, instance_id=task_id, model=self.model,
                              agent=self.agent, cell=_cell_of(run_dir_path),
                              run_dir=os.path.basename(run_dir_path))
        project = (task.get("repo") or "").split("/")[-1]
        if not project:
            raise RuntimeError(f"webbench task {task_id!r} carries no project in `repo`; "
                               f"the driver's --repo selects the Web-Bench project")
        os.makedirs(run_dir_path, exist_ok=True)
        max_turns = int(run_kwargs.get("max_turns") or os.environ.get("TE_MAX_TURNS", 80))
        t0 = time.time()
        project_dir = os.path.join(self.repo, "projects", project)

        row = {"instance": task_id, "repo": task.get("repo", ""),
               "model": self.model, "agent": self.agent, "arm": arm,
               "effort": self.effort, "cli_version": self.cli_version,
               "domain": "webbench", "webbench_project": project}

        sess = self.session_for(project)

        # A Web-Bench stream is sequentially dependent: once a task has failed every
        # attempt the evaluator stops, and the tasks after it were never attempted. Return
        # that fact rather than blocking on a Started line that will never come -- and
        # rather than billing a zero-cost "failure" that reads as a cheap success.
        if self._stream_broken or sess.finished:
            with open(os.path.join(run_dir_path, "transcript.jsonl"), "w",
                      encoding="utf-8"):
                pass                    # an empty transcript, so trace-readers find a file
            row.update(self._row_from_segment({}))
            row.update({"resolved": False, "stream_broken": True, "unreached": True,
                        # None, not False: an unreached task was never attempted, so it has
                        # no pass@1 or pass@N verdict to report.
                        "resolved_p1": None, "resolved_p2": None,
                        "webbench_attempt_results": [],
                        "webbench_attempts": 0,
                        "wall_s": round(time.time() - t0, 1),
                        "untooled_excluded": list(exclude_tools or [])})
            _log(f"{task_id} arm={arm} UNREACHED (stream stopped earlier)")
            return row, run_dir_path

        self._write_run_config(run_dir_path, task, arm, injected, exclude_tools,
                               max_turns, run_kwargs, sess, project)
        # Installed per TASK rather than per segment: a subclass may override `_segment`
        # (webbench_smoke does), and a guard that a runner can opt out of by overriding one
        # method is not a guard.
        net_guard_installed = self._install_net_guard(
            self.agent_home(run_dir_path), run_dir_path)

        transcript = os.path.join(run_dir_path, "transcript.jsonl")
        merged, resolved, attempts = {}, False, 0
        attempt_results = []   # per-attempt verdict, for pass@1 vs pass@N
        deleted_all, skipped_all, answered = [], [], []
        scaffold_notes = {}
        # One guard per ATTEMPT, not per task: each retry is a fresh rollout with its own
        # turn envelope, and a guard that spent its nudge budget on attempt 1 would leave
        # attempt 2 unguarded -- understating the mechanism it is there to measure.
        guards = []

        with open(transcript, "w", encoding="utf-8") as tfh:
            for attempt in range(self.retry):
                # 1. the evaluator announces the attempt it is starting ...
                started = sess.next_event("started", self.start_timeout)
                if started is None:
                    _log(f"{task_id}: evaluator produced no attempt "
                         f"(exited={sess.finished}) -- stream broken")
                    row["stream_broken"] = self._stream_broken = True
                    break
                if started["task"] != task_id:
                    raise RuntimeError(
                        f"webbench loop desync: the driver is on {task_id!r} but the "
                        f"evaluator started {started['task']!r}. The driver's task order "
                        f"must match projects/{project}/tasks.jsonl.")

                # 2. ... then asks the agent for files.
                req, reply_q = sess.next_request(self.start_timeout)
                if req is None:
                    _log(f"{task_id}: no agent request after the Started line")
                    row["stream_broken"] = self._stream_broken = True
                    break
                attempts += 1
                req_files = req.get("files") or {}
                guard = self._make_guard(run_kwargs)
                if guard is not None:
                    guards.append(guard)

                workspace = os.path.join(run_dir_path, f"ws-attempt{attempt + 1}")
                self._materialize(workspace, req_files)
                # Install the arm's external tool baseline ONCE per task-run, before the
                # first segment. The agent's HOME is per task-run and shared across
                # attempts and spiral segments, so re-installing per attempt would only
                # repeat the work; the notes from the first install describe the whole row.
                if self.scaffold is not None and not scaffold_notes:
                    scaffold_notes = self._apply_scaffold(
                        workspace, self.agent_home(run_dir_path), task, run_dir_path)
                prompt = build_prompt(task, injected, req, project,
                                      workspace=workspace)
                with open(os.path.join(run_dir_path,
                                       f"agent_request_{attempt + 1}.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump({"type": req.get("type"), "task": req.get("task"),
                               "error": req.get("error"),
                               "files": sorted(req_files)}, fh, indent=2)

                result = self._segment(workspace, prompt, tfh, exclude_tools,
                                       max_turns, guard)
                # Spiral rung 1: re-invoke in the SAME workspace on top of the agent's own
                # edits, with the leakage-safe redirect appended.
                while guard is not None:
                    nxt = guard.continuation(prompt)
                    if not nxt:
                        break
                    _log(f"{task_id}: spiral nudge -> segment {guard.segment}")
                    seg = self._segment(workspace, nxt, tfh, exclude_tools,
                                        max_turns, guard)
                    result = self._merge_segments(result, seg)
                merged = self._merge_segments(merged, result) if merged else result

                # 3. answer with the files that changed, and let it grade.
                files, deleted, skipped = self._collect(workspace, req_files, project_dir)
                deleted_all += deleted
                skipped_all += skipped
                answered.append(sorted(files))
                usage = self._segment_usage(result)
                reply_q.put({
                    "files": files,
                    "inputTokens": (usage.get("input_tokens") or 0)
                                   + (usage.get("cache_read_input_tokens") or 0)
                                   + (usage.get("cache_creation_input_tokens") or 0),
                    "outputTokens": usage.get("output_tokens") or 0,
                    "trajectory": os.path.join(run_dir_path, "transcript.jsonl"),
                })

                verdict = sess.next_event("verdict", self.verdict_timeout)
                if verdict is None:
                    _log(f"{task_id}: no verdict within {self.verdict_timeout}s "
                         f"(exited={sess.finished})")
                    row["stream_broken"] = self._stream_broken = True
                    break
                if verdict["task"] != task_id:
                    raise RuntimeError(
                        f"webbench loop desync: verdict for {verdict['task']!r} while the "
                        f"driver is on {task_id!r}")
                if not self.keep_workspaces:
                    shutil.rmtree(workspace, ignore_errors=True)
                attempt_results.append(bool(verdict["ok"]))
                if verdict["ok"]:
                    resolved = True
                    break

        # An `init` task is the one exception: the evaluator's InitTaskRunner reports success
        # regardless and falls back to src-init, so a failed init does NOT end the stream.
        if not resolved and task_id != "init" and attempts >= self.retry:
            self._stream_broken = True
            _log(f"{task_id}: failed all {attempts} attempts -- the Web-Bench stream ends "
                 f"here, later tasks are unreached")

        row.update(self._row_from_segment(merged))
        # Web-Bench grades each task with up to TE_WEBBENCH_RETRY attempts, so a task has
        # TWO defensible resolve metrics and they differ materially (measured on prisma:
        # 1-4 tasks per arm). Record both rather than picking one:
        #   resolved_p1  the FIRST attempt passed -- Web-Bench's own headline metric
        #   resolved_p2  ANY attempt passed (pass@N at the configured retry)
        # `resolved` stays pass@N: it is what the evaluator itself acts on (a task that
        # fails every attempt ends the stream), so changing it would change the run, not
        # just the report. Quote which one you mean -- they are not interchangeable.
        row["resolved"] = resolved
        row["resolved_p1"] = attempt_results[0] if attempt_results else None
        row["resolved_p2"] = any(attempt_results) if attempt_results else None
        row["webbench_attempt_results"] = attempt_results
        row["webbench_attempts"] = attempts
        row["webbench_answered_files"] = answered
        if deleted_all:
            row["webbench_unexpressible_deletions"] = sorted(set(deleted_all))
        if skipped_all:
            row["webbench_skipped_files"] = sorted(set(skipped_all))
        row["wall_s"] = round(time.time() - t0, 1)
        row["untooled_excluded"] = list(exclude_tools or [])
        # How many times the PreToolUse guard refused a Bash command that reached for the
        # benchmark's own answers (see te_harness.guards.net_guard). Recorded on EVERY row,
        # zero included: a guard is only trustworthy if you can see it firing, and a model
        # that starts routing around it shows up here as a count that falls to zero while
        # the traces still contain fetches.
        row["net_guard_blocks"] = self._net_guard_blocks(run_dir_path)
        # Whether the guard was actually ARMED, which the block count cannot tell you: 0
        # blocks on an armed guard and 0 blocks on a guard that was never registered are
        # the same number. Recorded here for the same reason SWE-bench records it -- an
        # unguarded row must be filterable rather than silently pooled with guarded ones.
        row["net_guard_installed"] = net_guard_installed
        # The agent-independent half of the same question, counted off the trace rather
        # than off the hook: `net_guard_blocks: 0` reads identically on an armed guard that
        # never fired and on a guard that was never registered, so a row needs both. It
        # matters most on the Codex lane, where the hook is not honoured at all and
        # `answer_key_hosts_blocked` is what does the guarding.
        row["answer_key_attempts"] = answer_key_attempts(run_dir_path)
        # Scaffold provenance (rtk_installed / rtk_hook_registered / scaffold_error / ...).
        # A failed install ran the task NATIVE, so these fields are what tells a real
        # scaffold row from a baseline row wearing a scaffold's name -- filter on them
        # before quoting a scaffold arm's mean (docs/HARNESS.md, "External tool
        # scaffolds").
        if scaffold_notes:
            row.update(scaffold_notes)
            row["scaffold"] = self.scaffold.name
        if guards:
            # Summed over the task's attempts, so the ledger sees the total the mechanism
            # actually spent rather than only the last rollout's share.
            row["aborted"] = any(g.aborted for g in guards)
            row["spiral_abort"] = row["aborted"]
            row["spiral_nudges"] = sum(g.nudges_done for g in guards)
            row["spiral_abort_suppressed"] = any(
                getattr(g, "abort_suppressed", False) for g in guards)
            row["spiral_events"] = [e for g in guards for e in (g.events or [])]
            row["spiral_judge_tokens"] = sum(g.judge_tokens for g in guards)
            row["spiral_nudge_writer_tokens"] = sum(g.nudge_writer_tokens for g in guards)
        _log(f"{task_id} arm={arm} resolved={resolved} attempts={attempts} "
             f"turns={row.get('num_turns')} eff_tok={pricing.effective_tokens(row):.0f} "
             f"{row['wall_s']}s")
        return row, run_dir_path

    # ---- accounting hooks (overridden per agent) ---------------------------------------
    # Claude Code reports its token breakdown in the stream's terminal `result` event.
    # Copilot reports NO tokens there and must mine its debug log instead, so these three
    # are hooks: everything else -- the evaluator session, the workspace round-trip, the
    # spiral segments -- is identical across the two agents.
    def _merge_segments(self, a, b):
        return _merge_usage(a, b)

    def _row_from_segment(self, result):
        return _row_from_result(result)

    def _segment_usage(self, result):
        """The {input_tokens, cache_read_input_tokens, output_tokens, ...} dict reported
        back to the Web-Bench evaluator for this segment."""
        return (result or {}).get("usage") or {}

    def _make_guard(self, run_kwargs):
        """The spiral guard for one rollout, or None when the arm has it switched off."""
        if not run_kwargs.get("spiral_guard"):
            return None
        guard_kwargs = dict(
            envelope=run_kwargs.get("abort_envelope"),
            judge=run_kwargs.get("spiral_judge"),
            thr=run_kwargs.get("spiral_thr"),
            nudge_budget=int(run_kwargs.get(
                "spiral_nudge_budget",
                1 if run_kwargs.get("spiral_nudge") else 0)),
            nudge_writer=run_kwargs.get("spiral_nudge_writer"),
            verbose=os.environ.get("TE_VERBOSE", "1") == "1")
        # `never_abort` is CONSERVATIVE-MODE-ONLY and absent from the vendored guard on
        # branches without trimech-safe -- see swebench.guard_accepts() for why it is
        # probed. This used to pass it unconditionally, which made every guarded Web-Bench
        # run on such a branch die with a TypeError at task 0.
        if guard_accepts("never_abort"):
            guard_kwargs["never_abort"] = bool(run_kwargs.get("spiral_never_abort"))
        return NudgeGuard(**guard_kwargs)

    def _write_run_config(self, run_dir_path, task, arm, injected, exclude_tools,
                          max_turns, run_kwargs, sess, project):
        """Record everything that shapes this run into `run_config.json`.

        Same contract as the SWE-bench harness: anything that moves cost or resolve is
        written down, because the CLI records neither the effort level nor the tool mask in
        its transcript, and the evaluator's grading settings live in a config file the run
        would otherwise not remember.
        """
        # The SAME two-step the CLI invocation performs (see `_segment`): recomputing only
        # the untool half here recorded `excluded_applied: []` on every row and made a
        # guarded run byte-identical to an unguarded one in its own provenance.
        drop = [t for t in (exclude_tools or []) if t not in self.protected_tools]
        drop = drop + [t for t in network_tool_block() if t not in drop]
        cfg = {
            "instance_id": task.get("instance_id"), "repo": task.get("repo"), "arm": arm,
            "domain": "webbench",
            "agent": self.agent, "model": self.model, "effort": self.effort,
            "max_turns": max_turns,
            "cli_version": self.cli_version, "agent_bin": self.agent_bin,
            "routing": {
                "base_url": self.routing_base,
                "optimizer_model": self.optimizer_model,
                "optimizer_transport": getattr(self, "optimizer_transport", "api"),
            },
            "tools": {
                "excluded_requested": list(exclude_tools or []),
                "excluded_applied": drop,
                "protected": sorted(self.protected_tools),
            },
            # What shaped the run externally. `answer_excludes` is Web-Bench's analogue of
            # SWE-bench's `scaffold.patch_excludes`: names `_collect` refuses to ship to
            # the evaluator, so a scaffold's own files cannot be graded as the agent's fix.
            "scaffold": (None if self.scaffold is None else {
                "name": self.scaffold.name,
                "answer_excludes": sorted(self.scaffold_answer_excludes()),
            }),
            "injection": {
                "present": bool(injected), "chars": len(injected or ""),
                "sha256": (hashlib.sha256(injected.encode()).hexdigest()[:16]
                           if injected else None),
            },
            "spiral": {
                "guard": bool(run_kwargs.get("spiral_guard")),
                "envelope": run_kwargs.get("abort_envelope"),
                "thresholds": run_kwargs.get("spiral_thr"),
                "judge": run_kwargs.get("spiral_judge") is not None,
                "nudge_writer_llm": run_kwargs.get("spiral_nudge_writer") is not None,
                "nudge_budget": run_kwargs.get(
                    "spiral_nudge_budget",
                    1 if run_kwargs.get("spiral_nudge") else 0),
                # applied, not requested: on a guard without the parameter the request
                # is silently inert, and recording it as true would misdescribe the run.
                "never_abort": (bool(run_kwargs.get("spiral_never_abort"))
                                if guard_accepts("never_abort") else False),
                "guard_per_attempt": True,
            },
            "webbench": {
                "project": project,
                "evaluator_repo": self.repo,
                "evaluator_config": sess.config_path,
                "evaluator_log": sess.log_path,
                "evaluator_hash": sess.hash,
                "retry": self.retry,
                "playwright_workers": self.max_workers,
                "node": self.node,
            },
            "timeouts": {"task_s": self.task_timeout,
                         "verdict_s": self.verdict_timeout,
                         "start_s": self.start_timeout},
            "grading_source": f"@web-bench/{project} playwright suite (cumulative)",
        }
        with open(os.path.join(run_dir_path, "run_config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, default=str)


TASK_TEMPLATE = """You are a web developer working in the project directory {cwd}.

The project is built up over a sequence of tasks; every file already present is the result
of the previous tasks and is your starting point. Implement ONLY the task below.

<task>
{description}
</task>
{error_block}
Rules:
- Work only inside {cwd}. Edit the existing files and add new ones as the task requires.
- The project's dependencies are NOT installed here and its test suite is not available,
  so do not try to install packages, start a dev server, or run tests -- read the existing
  code to infer the conventions, framework and file layout, and write code that follows
  them.
- Keep the previous tasks working: a hidden suite re-checks every earlier task as well as
  this one.
- Do not create test files, notes, or scratch files; only the project's own source.
- When the task is implemented, stop.
"""

ERROR_TEMPLATE = """
Your previous attempt at this task FAILED its checks with:

<failure>
{error}
</failure>

Fix the cause and implement the task correctly.
"""


# The evaluator appends two response-FORMAT instructions to an `init` task, because its own
# reference agent answers with a markdown code block and has no other way to name the file
# it is creating. This harness answers with a file map -- the agent writes real files -- so
# the instructions describe a transport that is not in use, and following them would put a
# filename line inside the file's contents.
_INIT_FORMAT_LINES = (
    "Add filename after code block.",
    "The filename should appear on the line following the language specifier in your code "
    "block.",
)


def strip_transport_instructions(description):
    """Remove the evaluator's code-block formatting protocol from a task description."""
    kept = [ln for ln in description.splitlines()
            if ln.strip() not in _INIT_FORMAT_LINES]
    return "\n".join(kept).strip()


def build_prompt(task, injected, request, project, workspace="."):
    """Task prompt = the economizer's learned manual (if any) + the Web-Bench task.

    The description comes from the EVALUATOR's request, not the driver's task file: the
    evaluator is the authority on what it asked for, it decorates an `init` task with the
    project's existing-file list, and on a retry it supplies the previous attempt's
    failure -- all part of Web-Bench's contract.
    """
    description = strip_transport_instructions(
        (request.get("task") or task.get("problem_statement") or "").strip())
    err = (request.get("error") or "").strip()
    body = TASK_TEMPLATE.format(
        cwd=workspace, description=description,
        error_block=ERROR_TEMPLATE.format(error=err[:8000]) if err else "")
    return (injected.rstrip() + "\n\n" + body) if injected else body


class CopilotWebBenchRunner(WebBenchRunner):
    """AgentRunner: the GitHub Copilot CLI against Web-Bench, graded by Web-Bench's own
    evaluator.

    Web-Bench runs the agent on the HOST in a scratch workspace (no container), so this
    subclass is smaller than the SWE-bench one: only construction, the launch argv, and the
    accounting change. The evaluator session, workspace round-trip, spiral segments and
    file collection are inherited unchanged.

    Three things to know before comparing arms with the Claude runner:

      * **No turn cap.** Copilot has no `--max-turns`; `max_turns` is recorded but not
        enforced. The wall-clock `timeout` is the real bound -- and it must stay UNDER the
        evaluator's ~300s undici header timeout, or the whole stream's chain breaks, not
        just this task. That is why the default here is tighter than SWE-bench's.
      * **Tokens come from the debug log**, not the JSON stream (see te_harness.copilot).
        The per-segment figure reported back to the evaluator is the mined one.
      * **Auth is on-disk GitHub auth** (or the CLI's own BYOK provider), so there is no
        ANTHROPIC_API_KEY requirement.
    """

    def __init__(self, agent="copilot", model=None, repo=None, scaffold=None):
        self.agent = agent
        # 'auto' normalises to None: it is a routing directive, not a billable model id,
        # and recording it as one makes pricing hard-fail at the END of a paid rollout.
        self.model = copilot.normalize_model(model or os.environ.get("COPILOT_MODEL"))
        copilot.validate_priceable(self.model, self.agent)
        # Same rule as the Claude path: the optimizer is the agent's own model. It was
        # already CALLED that way (optimizer_call substitutes self.model for the "agent"
        # label), but it was RECORDED as "agent" -- so run_config.json described a cost
        # basis the run did not use.
        if os.environ.get("TE_OPTIMIZER_MODEL"):
            raise RuntimeError(
                "TE_OPTIMIZER_MODEL is no longer honoured: the between-task optimizer "
                "runs on the agent's own model so mechanism overhead is priced on the "
                "same basis as the task rows. Unset it.")
        self.optimizer_model = self.model or "auto"
        self.effort_requested = (os.environ.get("TE_EFFORT_LEVEL")
                                 or os.environ.get("COPILOT_EFFORT_LEVEL") or "high")
        if self.effort_requested not in copilot.EFFORT_LEVELS:
            raise RuntimeError(f"TE_EFFORT_LEVEL={self.effort_requested!r} is not one of "
                               f"{sorted(copilot.EFFORT_LEVELS)}")
        # The router model rejects --effort and exits, so effort is only APPLIED when a
        # model is pinned. Recorded as null otherwise, because on `auto` the effort is
        # whatever the router chose -- not a controlled part of the cost basis.
        self.effort = (self.effort_requested
                       if copilot.effort_applies(self.model) else None)
        if self.effort is None:
            why = ("the launcher probed this model and the CLI rejected --effort"
                   if os.environ.get("TE_EFFORT_APPLIES") == "0"
                   else "the 'auto' router takes no effort level; pin COPILOT_MODEL "
                        "to a served model id to control it")
            _log(f"copilot: TE_EFFORT_LEVEL={self.effort_requested} NOT applied "
                 f"(model={self.model or 'auto'}, recorded as null) -- {why}.")
        self.copilot = copilot.require_install()      # host-only: no container seed here
        self.agent_bin = self.copilot
        self.protected_tools = copilot.PROTECTED_TOOLS
        # Copilot's own BYOK bills a custom provider instead of the subscription (see
        # copilot.provider_base) -- a different cost basis, so run_config.json names it.
        self.routing_base = copilot.provider_base()
        # Mechanism-call transport, same rule and same reason as the SWE-bench harness
        # (copilot.anthropic_optimizer_endpoint): the Messages API when the lane's BYOK
        # provider speaks it and so bills what its tasks bill, else the CLI. Kept in step
        # with that harness deliberately -- two transports for the same call would make the
        # two domains' overhead incomparable.
        self.optimizer_endpoint = copilot.anthropic_optimizer_endpoint()
        self.optimizer_transport = "api" if self.optimizer_endpoint else "cli"
        if self.optimizer_endpoint:
            _log(f"copilot: mechanism calls -> {self.optimizer_endpoint[0]} "
                 "(the BYOK provider, direct Messages API)")
            self._preflight_optimizer_endpoint()
        self.cli_version = copilot.cli_version(self.copilot)
        self.repo = os.path.realpath(repo) if repo else webbench_repo()
        self.node = node_bin()
        # Same seam as the Claude runner; see WEBBENCH_SCAFFOLDS.
        self.scaffold_name = scaffold or None
        self.scaffold = scaffolds_pkg.get(self.scaffold_name)
        if self.scaffold is not None:
            self.scaffold.preflight(self.scaffold_profile_for("/nonexistent"))
            _log(f"scaffold: {self.scaffold.name} (external baseline)")
        # 16384, matching every other runner: one cap for the mechanisms' calls across
        # both agents and both domains. It was 8192 here alone, which is a real hazard
        # rather than a tidiness point -- a cap too low truncates a judge's per-tool JSON
        # into an unparseable answer, which reads downstream as "no verdict" while still
        # billing the call. Raising it can only remove truncations, never add them.
        self.optimizer_max_tokens = int(os.environ.get("TE_OPTIMIZER_MAX_TOKENS", "16384"))
        # Under the evaluator's ~300s undici header timeout, for the chain-integrity reason
        # documented on the Claude agent: a segment that outlives it breaks the stream.
        self.task_timeout = webbench_task_timeout()
        self.retry = max(1, int(os.environ.get("TE_WEBBENCH_RETRY", "2")))
        self.max_workers = int(os.environ.get("TE_WEBBENCH_MAX_WORKERS", "4"))
        self.verdict_timeout = int(os.environ.get("TE_WEBBENCH_VERDICT_TIMEOUT", "1800"))
        self._init_stream_state()

    def _preflight_optimizer_endpoint(self):
        """Refuse the lane if its BYOK provider will not answer a mechanism call -- the
        SWE-bench harness's check, through the same shared helper and the same
        TE_OPTIMIZER_PREFLIGHT gate. Only reached on the API transport."""
        base, key, bearer = self.optimizer_endpoint
        preflight_optimizer_endpoint(
            base_url=base, api_key=key, bearer=bearer,
            configured_model=self.optimizer_model, label="copilot",
            remedy="Check COPILOT_PROVIDER_BASE_URL and the credential that goes with it.")

    def optimizer_call(self, prompt, model="agent"):
        if self.optimizer_endpoint:
            base, key, bearer = self.optimizer_endpoint
            return anthropic_optimizer_call(
                prompt, model=model, configured_model=self.optimizer_model,
                max_tokens=self.optimizer_max_tokens,
                api_key=key, bearer=bearer, base_url=base)
        return copilot.optimizer_call(
            prompt, binary=self.copilot,
            model=(model if model not in ("agent", "copilot") else self.model))

    # ---- one streamed agent segment ----------------------------------------------------
    def scaffold_profile_for(self, home):
        """Copilot's profile. Instructions and the hook spelling differ from Claude's, and
        the scaffold branches on `kind` for exactly that reason."""
        return scaffolds_pkg.AgentProfile(
            kind="copilot",
            home=home,
            instructions_file=".github/copilot-instructions.md",
            nav_tools=("view", "grep", "glob"),
            mcp_args=lambda cfg: [],
            patch_excludes=(".github/copilot-instructions.md", ".github/hooks"),
            bin_dir=self.scaffold_bin_dir(home))

    def scaffold_exec_env(self, run_dir_path):
        """Point `copilot plugin install` at the isolated config dir, not ~/.copilot."""
        return {"COPILOT_HOME": self.scaffold_copilot_home(run_dir_path)}

    # Copilot reads hooks from `$COPILOT_HOME/settings.json` -- NOT `$HOME/.claude/`, and
    # not `hooks.json` (probed on CLI 1.0.83, 2026-09-08: a `hooks.json` at that level is
    # ignored, a `settings.json` fires). Inheriting the Claude path wrote a file this CLI
    # never reads, which is why every Copilot Web-Bench row was unguarded while reporting
    # nothing wrong.
    NET_GUARD_HOOK_KEY = "preToolUse"

    def net_guard_settings_path(self, home, run_dir_path):
        # The isolated COPILOT_HOME, which is also what `_segment` exports, so the file is
        # in the dir the agent will actually read. `home` is unused on this path.
        return os.path.join(self.scaffold_copilot_home(run_dir_path), "settings.json")

    def net_guard_hook_entry(self, command):
        """Copilot's entry shape: FLAT, matcher `bash` lower-case."""
        return {"matcher": "bash", "type": "command", "command": command}

    def prepare_agent_home(self, home):
        """Link the per-task HOME's package cache to the real one before installing.

        The scaffold's install runs `copilot plugin install` under the per-task `$HOME`
        (rtk passes it explicitly), and the CLI keeps a ~143MB package cache at
        `$HOME/.cache/copilot`. Without this the cache is re-downloaded into every
        task-run's throwaway home -- measured 143MB per run dir, which over one Web-Bench
        arm is hundreds of GB and one package download per task, and fails outright on a
        host with no network to the package host. Symlinking reuses the real cache exactly
        as an unscaffolded run does, while everything else about the home stays per-task.
        """
        real = os.path.join(os.path.expanduser("~"), ".cache", "copilot")
        if not os.path.isdir(real):
            return
        cache = os.path.join(home, ".cache")
        os.makedirs(cache, exist_ok=True)
        link = os.path.join(cache, "copilot")
        if not os.path.exists(link):
            try:
                os.symlink(real, link)
            except OSError as e:                       # non-fatal: costs disk, not the run
                _log(f"could not link the copilot package cache ({e}); "
                     f"it will be re-downloaded into {cache}")

    def scaffold_copilot_home(self, run_dir_path):
        """An ISOLATED `COPILOT_HOME` for a task-run, seeded with the real auth.

        This is the one place a Web-Bench run could damage the operator's machine.
        Unlike the Claude segment, this runner deliberately does NOT relocate `$HOME` --
        Copilot keeps a ~170MB package cache under `$HOME/.cache/copilot` that a fresh HOME
        would orphan every task. But both things that register a hook write into
        `COPILOT_HOME` (default `~/.copilot`): `copilot plugin install` for rtk, and the
        net guard's `settings.json`. Run as-is, either would land in the operator's REAL
        config and stay there after the sweep.

        `COPILOT_HOME` is a separate knob from `$HOME`, so the fix is to point only that at
        a per-task dir and copy in the small auth/config files (the 29MB of `session-state`
        is disposable). The package cache stays where it is.

        Used on **every** arm, not only scaffolded ones -- the net guard is unconditional,
        so there is no arm that leaves this dir untouched. (The name is historical: it was
        scaffold-only when scaffolds were the only writer.)
        """
        dest = os.path.join(run_dir_path, ".copilot-home")
        if os.path.isdir(dest):
            return dest
        os.makedirs(dest, exist_ok=True)
        src = copilot.copilot_config()
        for name in ("config.json", "settings.json", "mcp-config.json"):
            path = os.path.join(src, name)
            if os.path.isfile(path):
                shutil.copy2(path, os.path.join(dest, name))
        return dest

    def _segment(self, workspace, prompt, transcript_fh, exclude_tools, max_turns, guard):
        """Run `copilot -p` once in `workspace`. Returns a usage dict (not a Claude
        `result` event). `max_turns` is accepted and ignored -- Copilot has no turn cap."""
        # Per-segment log dir: the miner sums every *.log it finds, so segments must not
        # share one or the second segment would re-count the first's usage.
        seg = getattr(guard, "segment", 0)
        logdir = os.path.join(os.path.dirname(workspace),
                              f".copilot-runlog-{os.path.basename(workspace)}-{seg}")
        os.makedirs(logdir, exist_ok=True)

        argv = copilot.build_argv(prompt, log_dir=logdir, model=self.model,
                                  effort=self.effort, exclude_tools=exclude_tools)
        argv[0] = self.copilot
        env = dict(os.environ)
        # HOME is deliberately NOT relocated the way the Claude segment relocates it.
        # Copilot keeps its ~170MB package cache under $HOME/.cache/copilot and its auth
        # under $HOME/.copilot; a fresh HOME per task would orphan both -- re-downloading
        # the cache every task, and failing outright on a box with no network to the
        # package host. Nothing is written into the workspace either way (the CLI keeps its
        # state in $HOME, not cwd), and `.copilot` is in _SKIP_DIRS as a belt-and-braces
        # guard in case a future version drops something there.
        env.update(copilot.run_env())
        # COPILOT_HOME is redirected to a per-task dir on EVERY arm, and it must be the
        # SAME dir the installs wrote to (scaffold_exec_env, net_guard_settings_path) or
        # the agent would look for its hooks somewhere they were never registered. $HOME is
        # deliberately left alone so the ~170MB package cache under $HOME/.cache/copilot is
        # not orphaned per task.
        #
        # This used to be scaffold-only, on the reasoning that a no-scaffold arm writes
        # nothing into the config dir and should stay byte-identical to the pre-scaffold
        # form. The net guard ended that: it applies to every arm, it needs a hook in this
        # file, and writing one into the operator's REAL ~/.copilot -- then leaving it
        # there after the sweep -- is exactly the damage the scaffold branch existed to
        # prevent. So the isolation is now unconditional, which is also one fewer
        # behavioural difference between the arms.
        env["COPILOT_HOME"] = self.scaffold_copilot_home(os.path.dirname(workspace))
        # A scaffold installs its binary into the per-task bin dir, and on this lane the
        # AGENT's own shell has to find it -- not just the install step. rtk's hook rewrites
        # `ls -la` into `rtk ls -la` and hands that back to the CLI to run, so a bin dir
        # missing from PATH means every rewritten call dies `rtk: command not found`. That is
        # not a silent no-op: reproduced 2026-09-13, the agent flails against the failing
        # command (19 shell calls for one `ls`), which is the same shape as the 12x-cost
        # Web-Bench incident in baselines/rtk._hook_binary. Harmless and empty with no
        # scaffold. $HOME is still deliberately left alone (the package cache above).
        env["PATH"] = os.pathsep.join(
            [self.scaffold_bin_dir(self.agent_home(os.path.dirname(workspace))),
             env.get("PATH", "")])

        proc = subprocess.Popen(argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, start_new_session=True)
        stream_lines, stopped = [], False
        deadline = time.time() + self.task_timeout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            transcript_fh.write(line + "\n")
            transcript_fh.flush()
            stream_lines.append(line)
            if guard is not None and guard.monitor(line):
                stopped = True
                break
            if time.time() > deadline:
                _log("webbench copilot segment hit TE_TASK_TIMEOUT; stopping the agent")
                stopped = True
                break

        if stopped:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(2)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.kill()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = (proc.stderr.read() or "")[-600:]

        # Same reasoning as the SWE-bench runner: a refused configuration is not a task
        # failure, it is a launch error that happens to surface late.
        bad = copilot.fatal_config_error(err)
        if bad:
            raise RuntimeError(
                f"copilot refused this configuration: {bad}\n"
                f"  model={self.model!r} effort={self.effort!r}\n"
                "  fix COPILOT_MODEL / TE_EFFORT_LEVEL (see docs/COPILOT.md step 2).")

        log_text = copilot.read_log_dir(logdir)
        usage = copilot.row_usage("\n".join(stream_lines), log_text)
        if err.strip() and not usage.get("num_turns"):
            _log(f"webbench copilot stderr: {err.strip()[:300]}")
        usage["_debug_log"] = log_text
        return usage

    # ---- accounting hooks ---------------------------------------------------------------
    def _merge_segments(self, a, b):
        merged = copilot.merge_usage(a, b)
        merged["_debug_log"] = (a or {}).get("_debug_log", "") + (b or {}).get("_debug_log", "")
        return merged

    def _segment_usage(self, result):
        return {k: (result or {}).get(k) or 0
                for k in ("input_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens", "output_tokens")}

    def _row_from_segment(self, result):
        row = dict(result or {})
        row.setdefault("is_error", False)
        row.setdefault("cost_usd_vendor", None)
        # Same degradation predicate the SWE-bench Copilot runner sets, so one filter
        # covers every row this harness writes. Copilot needs no _UsageRecovery: it mines
        # its log dir AFTER the kill, so a guard stop costs it nothing.
        src = row.get("tokens_source")
        row["usage_incomplete"] = src is not None and src != "debug_log"
        row["output_tokens_missing"] = row.get("output_tokens") is None
        row["num_turns_recovered"] = False
        if row["usage_incomplete"]:
            row.setdefault("usage_incomplete_reason", f"tokens_source={src}")
        return row

    def run_task(self, task, *, arm, injected, exclude_tools, run_dir_path, **run_kwargs):
        row, out_dir = super().run_task(
            task, arm=arm, injected=injected, exclude_tools=exclude_tools,
            run_dir_path=run_dir_path, **run_kwargs)
        # Same filename SWE-bench writes, so trace.parse_catalog(run_dir, 'copilot') works
        # identically on both domains -- without it the untooling judge sees empty
        # descriptions, drops nothing, and still bills for the call.
        log_text = row.pop("_debug_log", "")
        if log_text:
            try:
                with open(os.path.join(out_dir, "copilot_debug.log"), "w",
                          encoding="utf-8") as fh:
                    fh.write(log_text)
            except OSError as e:
                _log(f"could not save copilot_debug.log: {e}")
        return row, out_dir




class CodexWebBenchRunner(WebBenchRunner):
    """AgentRunner: Codex against Web-Bench, graded by Web-Bench's own evaluator.

    The Codex twin of `CopilotWebBenchRunner`, and it overrides the same seams: construction,
    the launch invocation, the accounting, and the guard. The evaluator session, workspace
    round-trip, spiral segments and file collection are inherited unchanged, so a Codex row
    and a Claude row describe the same lifecycle.

    It bills the OpenAI API directly on OPENAI_API_KEY, exactly as on SWE-bench: one resolver
    (`codex.resolve_codex_routing`) serves both domains, so the two cannot drift about which
    endpoint an environment means.

    **The answer-key guard is the part that needed new machinery, and it is the reason this
    class exists rather than a two-line registration.** Web-Bench runs the agent on the HOST
    with no per-task container, so the other two agents are guarded by a PreToolUse hook --
    which Codex provably ignores (measured on 0.153.1 with two live probes; see
    `swebench.CodexSwebenchRunner._install_net_guard`). On SWE-bench the fallback is the
    container's `/etc/hosts`; there is no container here. Two candidates were measured:

      * **Codex's own sandbox** (`codex sandbox`) denies network exactly as wanted -- but on
        0.153.1 it also denies every write, and `sandbox_workspace_write.writable_roots` /
        `network_access` are rejected as unknown configuration both in `config.toml` under
        `--strict-config` and as `-c` overrides. An agent that cannot write cannot do a
        Web-Bench task, so this is unusable on this build.
      * **An unprivileged user+mount namespace** carrying the same `/etc/hosts` block the
        SWE-bench container gets. Measured here: `github.com` resolves to 127.0.0.1 and curl
        fails, `registry.npmjs.org` still resolves, writes still work, and files created
        inside are owned by the real user outside. This is what
        the lane uses, so the Codex Web-Bench row is guarded by the same DNS layer as the
        Codex SWE-bench row -- and needs no cooperation from the CLI, which is the property
        the hook lacked.

    `net_guard_installed` is therefore False on this lane, honestly, and
    `answer_key_hosts_blocked` is the field that says the lane is guarded.
    """

    # Built once: `unshare` must exist and unprivileged user namespaces must be permitted,
    # or the lane cannot be guarded and must say so rather than run unguarded.
    _NS_PROBE = "echo https://github.com/probe"

    def __init__(self, agent="codex", model=None, repo=None, scaffold=None):
        self.agent = agent
        # Checked FIRST, before any credential or catalog work: what this lane can host is
        # not a function of its endpoint. `rtk` is hostable -- in its Codex mode it is
        # instruction files under CODEX_HOME, not the hook this CLI ignores (baselines/rtk.py)
        # -- and it is the only scaffold Web-Bench hosts for any agent (WEBBENCH_SCAFFOLDS).
        if scaffold and scaffold != "rtk":
            raise RuntimeError(
                f"the {scaffold!r} baseline is not wired for the Codex Web-Bench lane "
                f"(Web-Bench hosts {sorted(WEBBENCH_SCAFFOLDS)}; on Codex that is the "
                f"instruction-mode rtk). A scaffold that installs and never takes effect "
                f"would produce native rows under the scaffold's name.")
        # The OpenAI API directly, on OPENAI_API_KEY -- see codex.resolve_codex_routing.
        self.routing = codex.resolve_codex_routing()
        self.api_key = self.routing["api_key"]
        self.model = model or os.environ.get("CODEX_MODEL", "gpt-5.6-sol")
        codex.validate_priceable(self.model, self.agent)
        codex.verify_model(self.model, self.routing)
        if os.environ.get("TE_OPTIMIZER_MODEL"):
            raise RuntimeError(
                "TE_OPTIMIZER_MODEL is no longer honoured: the between-task optimizer runs "
                "on the agent's own model so mechanism overhead is priced on the same basis "
                "as the task rows. Unset it.")
        self.optimizer_model = self.model
        self.effort = os.environ.get("TE_EFFORT_LEVEL", "high")
        if self.effort not in EFFORT_LEVELS:
            raise RuntimeError(f"TE_EFFORT_LEVEL={self.effort!r} is not one of "
                               f"{sorted(EFFORT_LEVELS)}")
        codex.require_install()
        self.codex_vendor = codex.codex_vendor()
        self.agent_bin = codex.codex_bin()
        self.cli_version = codex.cli_version(self.agent_bin)
        self.protected_tools = PROTECTED_TOOLS
        # The captured base prompt the lean-instructions package rewrites. Absent on a CLI
        # build with no capture: the package is then unavailable, stays loaded, and says so
        # -- a stale prompt is never applied to a newer CLI.
        self.base_prompt = codex.base_prompt_for(self.cli_version)
        self.system_skills = codex.bundled_system_skills(self.agent_bin) or None
        self.scaffold_name = scaffold or None
        self.scaffold = scaffolds_pkg.get(self.scaffold_name)
        if self.scaffold is not None:
            self.scaffold.preflight(self.scaffold_profile_for("/nonexistent"))
            _log(f"scaffold: {self.scaffold.name} (external baseline, instruction mode)")
        self.optimizer_max_tokens = int(os.environ.get("TE_OPTIMIZER_MAX_TOKENS", "16384"))
        self.repo = os.path.realpath(repo) if repo else webbench_repo()
        self.node = node_bin()
        self.task_timeout = webbench_task_timeout()
        self.retry = max(1, int(os.environ.get("TE_WEBBENCH_RETRY", "2")))
        self.max_workers = int(os.environ.get("TE_WEBBENCH_MAX_WORKERS", "4"))
        self.verdict_timeout = int(os.environ.get("TE_WEBBENCH_VERDICT_TIMEOUT", "1800"))
        # Fields the INHERITED lifecycle reads. They are set here rather than left to the
        # base __init__ (which this class does not call, because it resolves the Anthropic
        # key) and a test pins the whole set, since the failure mode is an AttributeError
        # raised after the evaluator has started and the first task has been paid for --
        # which is exactly how the missing one was found, on a live run.
        self.routing_base = self.routing["base_url"]
        self.optimizer_transport = "api"    # codex.optimizer_call, always over /v1/responses
        self.claude = None                  # no Claude binary on this lane
        self.keep_workspaces = os.environ.get("TE_KEEP_WORKSPACES") == "1"
        self.start_timeout = int(os.environ.get("TE_WEBBENCH_START_TIMEOUT", "1800"))
        self._capabilities = ()
        self._lean_instructions_tokens = 0
        _log(f"codex: model={self.model} endpoint={self.routing_base}")
        self.answer_key_ns = self._probe_namespace_guard()
        self._preflight_optimizer_endpoint()
        self._init_stream_state()

    # ---- the answer-key layer ----------------------------------------------------------
    def _probe_namespace_guard(self):
        """Prove an unprivileged user+mount namespace can blackhole the answer-key hosts.

        Probed at CONSTRUCTION, before task 0, because the alternative is a lane that runs
        unguarded while every row reports a guard. Returns True only when the namespace was
        actually created AND the block took effect inside it.
        """
        try:
            p = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--mount", "bash", "-c",
                 "getent hosts github.com | head -1"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            _log(f"codex webbench: cannot create a user+mount namespace ({type(e).__name__})"
                 f" -- the answer-key hosts block is NOT available on this host")
            return False
        if p.returncode != 0:
            _log("codex webbench: unprivileged user namespaces are refused on this host "
                 "(check /proc/sys/kernel/unprivileged_userns_clone) -- the answer-key "
                 "hosts block is NOT available")
            return False
        _log("codex webbench: answer-key hosts block armed through a user+mount namespace "
             "(there is no container here; the PreToolUse hook is the second layer)")
        return True

    def _hosts_file(self, run_dir_path):
        """This task's doctored /etc/hosts: the real one plus the answer-key block."""
        path = os.path.join(run_dir_path, ".hosts-block")
        try:
            with open("/etc/hosts", encoding="utf-8") as fh:
                base = fh.read()
        except OSError:
            base = "127.0.0.1 localhost\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(base.rstrip("\n") + "\n" + net_guard.hosts_block() + "\n")
        return path

    def net_guard_settings_path(self, home, run_dir_path):
        """Codex reads hooks from `$CODEX_HOME/hooks.json` -- and `home` IS the per-task
        CODEX_HOME here -- in Claude Code's shape: `{"hooks": {"PreToolUse": [...]}}`."""
        return os.path.join(home, "hooks.json")

    # Set per task by `_install_net_guard`; `_segment` passes the trust bypass only when it
    # is True, so an unarmed task runs exactly the argv it always did.
    _net_guard_armed = False

    def _install_net_guard(self, home, run_dir_path=None):
        """The shared installer, pointed at Codex's hook file -- and Codex honours it.

        Verified live on codex-cli 0.153.1 (2026-09-30): with this file and
        `--dangerously-bypass-hook-trust` the guard refused a GitHub-bound command before it
        ran and logged it, while a control command ran. Without the bypass Codex skips every
        hook nobody has approved in /hooks, which an unattended run cannot do -- so arming
        here is what turns the bypass on in `_segment`. The namespace hosts block stays the
        primary layer (docs/HARNESS.md, "The answer-key guard").
        """
        self._net_guard_armed = bool(super()._install_net_guard(home, run_dir_path))
        return self._net_guard_armed

    # ---- construction-time hooks -------------------------------------------------------
    def _preflight_optimizer_endpoint(self):
        codex.preflight_optimizer(routing=self.routing, configured_model=self.model)

    def optimizer_call(self, prompt, model="agent"):
        """One mechanism call against THIS lane's endpoint, billed like the task rows."""
        return codex.optimizer_call(
            prompt, configured_model=self.model, routing=self.routing,
            max_tokens=self.optimizer_max_tokens)

    def agent_home(self, run_dir_path):
        """`CODEX_HOME` for this task-run. Never inside the workspace (everything there is a
        candidate answer file) and never under /tmp -- the CLI refuses to create its PATH
        helper binaries under a temporary dir and says so on every invocation."""
        return os.path.join(run_dir_path, ".codex-home")

    def scaffold_profile_for(self, home):
        """Codex's profile, per task-run. `home` IS the CODEX_HOME here, so the env a
        scaffold installs under points at it; instructions land in `$CODEX_HOME/AGENTS.md`,
        which the CLI loads, rather than in the workspace, which the evaluator would grade."""
        return scaffolds_pkg.AgentProfile(
            kind="codex",
            home=home,
            instructions_file="AGENTS.md",
            nav_tools=(),
            mcp_args=lambda cfg: [],
            patch_excludes=("AGENTS.md", "RTK.md"),
            bin_dir=self.scaffold_bin_dir(home),
            env={"CODEX_HOME": home})

    def scaffold_exec_env(self, run_dir_path):
        return {"CODEX_HOME": self.agent_home(run_dir_path)}

    def _write_config(self, home, workspace):
        """Write this task's request contract into the per-task CODEX_HOME.

        The same `config.toml` the SWE-bench lane installs in its container, with two host
        differences: the workspace, not `/testbed`, is the trusted project, and the lean
        instructions file is named by its host path in the per-task CODEX_HOME. Written per
        SEGMENT because the capability mask is frozen per task and the workspace path is
        only known here.
        """
        os.makedirs(home, exist_ok=True)
        lean = codex.lean_instructions(getattr(self, "base_prompt", None),
                                       self._capabilities)
        self._lean_instructions_tokens = len(lean) // 4 if lean else 0
        instr_path = os.path.join(home, "instructions.md")
        cfg = codex.config_toml(
            routing=self.routing, model=self.model, effort=self.effort,
            project=workspace, capabilities=self._capabilities,
            system_skills=self.system_skills, instructions=lean,
            instructions_path=instr_path if lean else None)
        with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(cfg)
        if lean:
            with open(instr_path, "w", encoding="utf-8") as fh:
                fh.write(lean)
        return cfg

    # ---- one rollout segment ------------------------------------------------------------
    def _segment(self, workspace, prompt, transcript_fh, exclude_tools, max_turns, guard):
        """Run `codex exec --json` once in `workspace` (or `codex exec resume` for a spiral
        segment), inside a user+mount namespace carrying the answer-key hosts block.

        `prompt` is a string for a fresh segment, or the resume REQUEST dict the Codex guard
        returned. `max_turns` is accepted and ignored: Codex has no turn-cap flag, exactly as
        on SWE-bench and on the Copilot lane. Returns a usage dict, not a Claude `result`
        event -- see the accounting hooks.
        """
        run_dir = os.path.dirname(workspace)
        home = self.agent_home(run_dir)
        self._write_config(home, workspace)

        resume = prompt if isinstance(prompt, dict) else None
        text = resume["prompt"] if resume else prompt
        hooked = bool(self._net_guard_armed)
        if resume:
            argv = codex.build_resume_argv(resume["session_id"],
                                           effort=resume.get("effort"), hook=hooked)
        else:
            argv = codex.build_argv(hook=hooked)
        # `build_argv` ends with `-C /testbed -`; on the host the workspace is the project.
        argv = [a if a != "/testbed" else workspace for a in argv]
        argv[0] = self.agent_bin

        env = dict(os.environ)
        env.update(codex.run_env())
        env["CODEX_HOME"] = home
        env[net_guard.LOG_ENV] = self._net_guard_log(home)    # where the hook logs refusals
        env["HOME"] = os.path.join(run_dir, ".host-home")
        os.makedirs(env["HOME"], exist_ok=True)
        # A scaffold copies its binary here (copy_in re-roots into bin_dir), so the agent's
        # own shell must find it. Harmless and empty when no scaffold ran.
        env["PATH"] = os.pathsep.join([self.scaffold_bin_dir(home), env.get("PATH", "")])
        # The key the provider block names, under that name.
        env[codex.KEY_ENV] = self.api_key

        # The answer-key layer. Everything the agent spawns inherits the namespace, so this
        # needs no cooperation from the CLI -- which is the whole point, since the CLI
        # ignores the hook the other two agents are guarded by.
        if self.answer_key_ns:
            hosts = self._hosts_file(run_dir)
            inner = "mount --bind %s /etc/hosts && exec \"$@\"" % _shq(hosts)
            cmd = ["unshare", "--user", "--map-root-user", "--mount",
                   "bash", "-c", inner, "_"] + argv
        else:
            cmd = argv

        proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, start_new_session=True)
        proc.stdin.write(text)
        proc.stdin.close()

        lines, stopped, deadline = [], False, time.time() + self.task_timeout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            transcript_fh.write(line + "\n")
            transcript_fh.flush()
            lines.append(line)
            if guard is not None and guard.monitor(line):
                stopped = True
                break
            if time.time() > deadline:
                _log("webbench codex segment hit TE_TASK_TIMEOUT; stopping the agent")
                stopped = True
                break
        if stopped:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(2)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.kill()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = (proc.stderr.read() or "")[-1200:]
        # A refused configuration is a launch error that surfaces late, not a task failure:
        # `--strict-config` rejects an unknown toggle, which is exactly what a CLI upgrade
        # that renamed one looks like. Raising keeps a renamed toggle from silently becoming
        # a no-op on every remaining task of the stream.
        if "unknown configuration" in err.lower() or "error loading config.toml" in err.lower():
            raise RuntimeError("codex refused its config at run time:\n" + err.strip()[:600])
        usage = codex.row_usage("\n".join(lines))
        if err.strip() and not usage.get("num_turns"):
            _log(f"agent stderr: {err.strip()[:300]}")
        return usage

    # ---- accounting ---------------------------------------------------------------------
    def _merge_segments(self, a, b):
        return codex.merge_usage(a, b)

    def _segment_usage(self, result):
        """What is reported back to the Web-Bench evaluator for this segment."""
        return {k: (result or {}).get(k) or 0
                for k in ("input_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens", "output_tokens")}

    def _row_from_segment(self, result):
        row = dict(result or {})
        row.setdefault("is_error", False)
        row.setdefault("cost_usd_vendor", None)
        # Same degradation predicate as the SWE-bench Codex runner, so one filter covers
        # every row either domain writes: a stream killed before `turn.completed` takes the
        # usage block with it, and row_usage reports None (never 0) in every token field.
        src = row.get("tokens_source")
        row["usage_incomplete"] = src is not None and src != "stream_usage"
        row["output_tokens_missing"] = row.get("output_tokens") is None
        row["num_turns_recovered"] = False
        if row["usage_incomplete"]:
            row.setdefault("usage_incomplete_reason", f"tokens_source={src}")
        return row

    # ---- the spiral guard ----------------------------------------------------------------
    def _make_guard(self, run_kwargs):
        """The Codex-native resume controller, when the arm carries one.

        The generic `NudgeGuard` the base class builds reads a Claude stream and reprompts;
        the Codex controller reads `codex exec --json` and resumes the same thread. Selecting
        on `codex_spiral_policy` keeps a Codex lane from silently running the generic guard
        against a stream it cannot parse.
        """
        policy = run_kwargs.get("codex_spiral_policy")
        if policy is None:
            return None
        return codex_spiral.CodexResumeGuard(
            policy=policy, verbose=os.environ.get("TE_VERBOSE", "1") == "1")

    # ---- entry point ---------------------------------------------------------------------
    def run_task(self, task, *, arm, injected, exclude_tools, run_dir_path, **run_kwargs):
        """Same lifecycle, with the capability mask frozen before the agent starts.

        The mask reaches the runner two ways and both are needed, exactly as on SWE-bench:
        `exclude_tools` carries the relay capability ids, and `codex_config_capabilities`
        carries the logical packages that have a native config toggle. The second is not
        derivable from the first -- `collaboration` expands to no relay id at all.
        """
        self._capabilities = tuple(run_kwargs.get("codex_config_capabilities") or ())
        row, out_dir = super().run_task(
            task, arm=arm, injected=injected, exclude_tools=exclude_tools,
            run_dir_path=run_dir_path, **run_kwargs)
        row["turns_basis"] = "codex_steps"
        row["codex_config_capabilities"] = list(self._capabilities)
        row["codex_lean_instructions_tokens"] = int(self._lean_instructions_tokens or 0)
        # This lane's honest guard fields: the hook is not honoured, the hosts block is.
        row["net_guard_installed"] = False
        row["answer_key_hosts_blocked"] = bool(self.answer_key_ns)
        if self.scaffold_name == "rtk":
            row.update(codex.rtk_compliance(os.path.join(out_dir, "transcript.jsonl")))
        self._prune_agent_home(run_dir_path)
        return row, out_dir

    # Kept: the contract this task ran under, which is provenance and is ~10 KB. Dropped:
    # the CLI's own state -- sqlite dbs and their WALs, the bundled skills copy, shell
    # snapshots, session rollouts -- which is ~38 MB PER TASK-RUN and would put ~19 GB of
    # throwaway CLI state in the results tree over a five-project, five-seed sweep. The
    # SWE-bench lane has no equivalent problem because its CODEX_HOME lives inside the task
    # container and dies with it; this restores that parity. TE_KEEP_WORKSPACES=1 keeps
    # everything, the same knob that keeps the workspace itself.
    _AGENT_HOME_KEEP = ("config.toml", "instructions.md", "AGENTS.md", "RTK.md")

    def _prune_agent_home(self, run_dir_path):
        home = self.agent_home(run_dir_path)
        if self.keep_workspaces or not os.path.isdir(home):
            return
        for name in os.listdir(home):
            if name in self._AGENT_HOME_KEEP:
                continue
            path = os.path.join(home, name)
            try:
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            except OSError:
                pass          # a file the CLI still holds is not worth failing a row over
        # The host $HOME the segment made is throwaway in full.
        try:
            shutil.rmtree(os.path.join(run_dir_path, ".host-home"))
        except OSError:
            pass


RUNNERS = {
    "claude": WebBenchRunner,
    "copilot": CopilotWebBenchRunner,
    "codex": CodexWebBenchRunner,
}


def make_runner(agent=None, scaffold=None, **kwargs):
    """Factory the driver's ``--runner te_harness.webbench:make_runner`` calls.

    Called with the driver's ``--agent`` label when the driver supports it, and with no
    arguments by older callers (which then get the TE_AGENT default, or claude). An
    unknown label is an error, never a silent fallback to Claude.

    ``scaffold`` names an external tool baseline to install for every task; the driver
    derives it from the arm, so the arm registry stays the single source of truth.
    Web-Bench supports the ones that need no container and no Python symbol map -- rtk
    today. ``TE_SCAFFOLD`` is the fallback for a caller that cannot pass it.
    """
    agent = (agent or os.environ.get("TE_AGENT") or "claude").lower()
    if agent not in RUNNERS:
        # An unknown label is an error, never a silent fallback to Claude: rows filed under
        # the wrong agent are wrong numbers rather than a crash, which is the worst failure
        # a benchmark can have.
        raise RuntimeError(
            f"no Web-Bench runner for --agent {agent!r}; "
            f"available: {', '.join(sorted(RUNNERS))}")
    _install_signal_cleanup()
    scaffold = scaffold or os.environ.get("TE_SCAFFOLD") or None
    if scaffold is not None and scaffold not in WEBBENCH_SCAFFOLDS:
        raise RuntimeError(
            f"the {scaffold!r} baseline is not wired for Web-Bench "
            f"(supported: {', '.join(sorted(WEBBENCH_SCAFFOLDS))}). Web-Bench runs the "
            f"agent on the HOST with no per-task repo container, and the repo map "
            f"generates a PYTHON symbol map that a Node/TypeScript project does not have. "
            f"See docs/HARNESS.md, \"External tool scaffolds\".")
    return RUNNERS[agent](agent=agent, scaffold=scaffold, **kwargs)
