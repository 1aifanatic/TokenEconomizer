"""SWE-bench task executor for TokenEconomizer: real Claude Code, real Docker, real grade.

This is the concrete `AgentRunner` the repo's driver expects behind
``--runner te_harness.swebench:make_runner``. One task =

  1. start the instance's official SWE-bench image (``/testbed`` at base_commit);
  2. run Claude Code INSIDE it (`claude -p --output-format stream-json`), with the
     economizer's per-task decisions applied -- injected manual prefix, `--disallowed-tools`
     from the untooling mask, and the spiral guard streaming the same JSONL the mechanisms
     later re-read from disk;
  3. capture the agent's diff and grade it with the OFFICIAL SWE-bench eval script +
     log parser (`swebench.harness.test_spec` / `.grading`) in that same container;
  4. return the row + transcript the economizer's post-task hooks consume.

Why the agent runs inside the container: the SWE-bench image already pins the repo, the
conda env, and the exact test command the official grader uses, and Claude Code ships as a
static ELF that bind-mounts in and runs there unmodified -- so the agent and the grader see
byte-identical state, with no host checkout to drift.

Environment (see the repo's .env / .env.example; the two paths marked REQUIRED and the
key have no default -- they are site-specific, so a missing one raises here rather than
resolving to whichever machine happened to write this file):
  ANTHROPIC_API_KEY      Anthropic API key. REQUIRED: the Claude lane bills
                         api.anthropic.com directly and nothing else. ANTHROPIC_BASE_URL
                         and ANTHROPIC_AUTH_TOKEN are not inputs -- see resolve_anthropic_key
  ANTHROPIC_MODEL        agent model            (default claude-sonnet-5)
  (no optimizer-model knob: the between-task proposer/judge/nudge-writer runs on the
   AGENT'S OWN model, against the agent's own endpoint. TE_OPTIMIZER_MODEL is refused.)
  TE_CLAUDE_BIN          host claude binary     (default: resolved from `which claude`)
  TE_SWEBENCH_GRADING    instance_id -> grading fields JSON. REQUIRED, unless DATA is set,
                         in which case $DATA/swebench/swebench_grading.json
  TE_SWEBENCH_REPO       checkout of SWE-bench, for the official grader. REQUIRED
  TE_EFFORT_LEVEL        reasoning effort: low|medium|high|xhigh|max      (default high)
  TE_TASK_TIMEOUT        per-segment agent wall-clock cap, seconds (default 1800)
  TE_EVAL_TIMEOUT        grading wall-clock cap, seconds           (default 1800)
  TE_OPTIMIZER_MAX_TOKENS  output cap for optimizer calls (default 16384; the cap spans
                         THINKING + response, and too low truncates a judge's per-tool JSON
                         into an unparseable answer, which reads downstream as "no verdict"
                         while still billing for every token spent thinking)
  TE_OPTIMIZER_LOG       where to record between-task optimizer calls
                         (default $TOKENECONOMIZER_RESULTS/logs/optimizer_calls.jsonl;
                         unset and with no results root, they are not recorded)
  TE_SWEBENCH_MAX_CPUS   CPU cap for the task container, cores      (default 16; 0 = no
                         cap). Also sets the matching
                         OMP/MKL/OPENBLAS/NUMEXPR/LOKY thread caps -- see _cpu_limit_args
  TE_KEEP_CONTAINERS     '1' leaves containers up for post-mortem  (default: remove)
  TE_VERBOSE             '1' prints per-task progress
"""
import hashlib
import inspect
import base64
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The economizer package supplies the vendored spiral controller + pricing weights. It is
# stdlib-only, so importing it here pulls in nothing heavy.
from tokeneconomizer import trace
from tokeneconomizer import pricing
from te_bench.nudge_guard import NudgeGuard

from te_bench import baselines as scaffolds_pkg
# The Codex-native spiral controller. Layer 1 and stdlib-only, like everything else this
# harness imports from the core -- it reads a transcript line and answers "resume or not".
from te_bench import codex_spiral

from . import codex, copilot
from .guards import net_guard

# The one endpoint the Claude lane bills: the Anthropic API, on ANTHROPIC_API_KEY.
_ANTHROPIC_DIRECT = "https://api.anthropic.com"

# Variables that would point Claude Code at another endpoint, credential or model catalog.
# None of them is an input here: no task container is ever handed one (the agent's
# environment is built from named variables only), and an agent that inherits the
# operator's shell has them removed first (agent_credential_env). So the agent bills exactly
# the endpoint its rows record.
IGNORED_ANTHROPIC_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                          "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                          "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")


def resolve_anthropic_key(env=None):
    """ANTHROPIC_API_KEY, the one credential the Claude lane bills on.

    Raises when it is unset -- at construction, rather than once per task. A set
    ANTHROPIC_BASE_URL is reported once and otherwise ignored: the agent and the
    between-task optimizer both call api.anthropic.com, whatever the shell says.
    """
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_BASE_URL"):
        _log("ANTHROPIC_BASE_URL is set and ignored: this harness bills api.anthropic.com "
             "only")
    key = env.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is unset -- the Claude lane bills the Anthropic API directly "
            "and has no other credential (see the repo's .env)")
    return key


def answer_key_attempts(run_dir_path):
    """How many commands in a task's transcript were answer-key-shaped, or None.

    Read from the trace AFTER the fact, through the one trace seam, so it is
    agent-independent: it is the only answer-key number available on a lane whose CLI
    ignores the PreToolUse hook, and it counts ATTEMPTS rather than refusals. A count that
    climbs while `net_guard_blocks` stays flat is the signature of a model routing around
    the hook -- which is exactly what these two fields exist to make visible. An attempt
    the guard REFUSED still counts here; that is the point, and `net_guard_blocks` (or
    `answer_key_hosts_blocked`) is the field that says whether it was stopped.

    Module-level rather than a method because BOTH domains need it: Web-Bench writes the
    same `transcript.jsonl`, and on the Codex Web-Bench lane -- where the hook is not
    honoured and the guard is an /etc/hosts block in a namespace -- this is the only
    per-row leak telemetry there is.
    """
    path = os.path.join(run_dir_path, "transcript.jsonl")
    # None, not 0. `load_trajectory` returns [] for a file that is not there, which would
    # report a task whose transcript was never written as CLEAN -- and a lane of zeros is
    # exactly what this field exists to distinguish from a lane of no data.
    if not os.path.exists(path):
        return None
    try:
        traj = trace.load_trajectory(path)
    except Exception:
        return None
    n = 0
    for _name, args, _res, _ok in traj:
        if not isinstance(args, dict):
            continue
        cmd = args.get("command") or args.get("cmd") or ""
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(x) for x in cmd)
        if cmd and net_guard.decide(str(cmd))[0]:
            n += 1
    return n


def agent_credential_env(env, *, api_key):
    """Make `env`, an agent process environment, bill the Anthropic API on `api_key` alone.

    For the harnesses whose agent runs on the HOST and inherits `os.environ` (Web-Bench):
    whatever the operator's shell carries in IGNORED_ANTHROPIC_VARS would otherwise reach
    the CLI and move the lane off the endpoint its rows record. Returns `env` for chaining.
    """
    for var in IGNORED_ANTHROPIC_VARS:
        env.pop(var, None)
    env["ANTHROPIC_API_KEY"] = api_key
    return env


# Tool names the CLI must never lose, whatever the untooling mask says: without an edit
# path and a shell there is no task, and a "resolved: false" caused by a missing Write is a
# harness artifact, not a mechanism result.
PROTECTED_TOOLS = frozenset({"Bash", "Edit", "Write", "Read"})

# What `claude --effort` accepts. Validated at construction so a typo fails before the run
# rather than silently falling back to the CLI default.
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def guard_accepts(param):
    """True if `te_bench.nudge_guard.NudgeGuard` takes `param` on this checkout.

    The guard's signature moves independently of this file: `never_abort` exists in
    some versions and not in others. Passing a parameter it does not accept raises TypeError
    inside `_make_guard`/`run_task` -- i.e. at task 0, before a single row is written --
    so every optional guard parameter must be probed rather than assumed. Both domain
    runners go through this one helper so the two cannot drift apart.
    """
    try:
        return param in inspect.signature(
            NudgeGuard.__init__).parameters
    except (TypeError, ValueError):          # not introspectable: assume it does not
        return False


# Containers this process started and has not yet removed. Docker containers are NOT in
# the launcher's process group, so `kill -TERM -<pid>` on a detached run (see
# scripts/launch/_env.sh) would otherwise leave a task container running -- and, with it, a
# billing agent. The signal handler below closes that hole.
_LIVE_CONTAINERS = set()


def _cleanup_containers(*_a):
    for name in list(_LIVE_CONTAINERS):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        _LIVE_CONTAINERS.discard(name)


def _install_signal_cleanup():
    """Remove live task containers on TERM/INT/HUP, then die of that same signal so the
    exit status stays honest. No-op off the main thread."""
    def handler(signum, frame):
        _cleanup_containers()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):        # not the main thread; nothing to install
            pass


# --------------------------------------------------------------------------------------
# project-environment activation (every SWE-bench lane)
# --------------------------------------------------------------------------------------
# The official images install the instance's dependencies into the `testbed` conda env and
# activate it from /root/.bashrc. No lane here runs with HOME=/root, and no CLI's shell
# sources that file, so without activation a bare `python` / `pytest` in the agent's shell
# resolves to the BASE interpreter (Python 3.11, no project deps), which cannot import the
# project's own package, while TASK_TEMPLATE tells the agent the env is active.
#
# Prepending the env's bin dir to PATH in the agent PROCESS environment works for EVERY
# shell shape (measured: `bash -lc`, `bash -c`, any HOME); the CLIs hand their environment
# to the shells they spawn. BASH_ENV hooks do not reach Claude Code's shell, so they are
# not used. Recorded on every row as `env_activation`, because rows with and without it
# are not comparable on cost and must not be pooled.
_CONDA_ENVS_DIR = "/opt/miniconda3/envs"
_PREFERRED_ENV = "testbed"


def env_activation_enabled():
    """TE_ENV_ACTIVATION=0 leaves the project env inactive (the image's own PATH)."""
    return os.environ.get("TE_ENV_ACTIVATION", "1") != "0"


def choose_project_env(env_bins):
    """The env bin dir to activate from a list of `<envs>/<name>/bin` paths, or None.

    `testbed` when present; otherwise the only env there is; otherwise nothing -- guessing
    among several would activate the wrong interpreter and be worse than the status quo.
    """
    bins = [b for b in env_bins if b]
    for b in bins:
        if os.path.basename(os.path.dirname(b)) == _PREFERRED_ENV:
            return b
    return bins[0] if len(bins) == 1 else None


def activation_env(env_bin, container_path):
    """The three variables that make the env active for any shell the agent spawns."""
    prefix = os.path.dirname(env_bin)
    path = env_bin + (":" + container_path if container_path else "")
    return {"PATH": path, "CONDA_PREFIX": prefix,
            "CONDA_DEFAULT_ENV": os.path.basename(prefix)}


def optimizer_log_path():
    """Where between-task optimizer calls are recorded, or None to not record them.

    These calls (proposer / tool-judge / spiral-judge / nudge-writer) happen BETWEEN tasks,
    so they have no run dir of their own and would otherwise be invisible -- you can see the
    tokens they cost in the ledger but not what they asked or what came back. That makes an
    empty learned manual impossible to tell apart from a broken proposer.
    """
    explicit = os.environ.get("TE_OPTIMIZER_LOG")
    if explicit:
        return explicit
    root = os.environ.get("TOKENECONOMIZER_RESULTS")
    return os.path.join(root, "logs", "optimizer_calls.jsonl") if root else None


# Which task-run the next between-task optimizer call belongs to. The proposer / tool
# judge / spiral judge / nudge writer fire BETWEEN tasks, so they have no run dir of their
# own; without this the optimizer log could only be summed over a whole results root, never
# attributed to a cell -- which is the level every other cost number is reported at. The
# runners set it at the top of each task and it stays put until the next one, so a call
# made after task N is charged to task N, exactly as the ledger charges `optimizer_tokens`.
# `agent` is part of it because the log is ONE file per results root while `cell`
# (`<arm>/<repo>_s<seed>`) names no tree: a Claude lane and a Copilot lane over the same
# arm and repo collide on the key, and a per-cell groupby without `agent` would merge them.
_OPT_CTX = {}


def _cell_of(run_dir_path):
    """`<arm>/<repo>_s<seed>` -- the cell key `summary_by_cell.csv` reports on.

    A run dir is `<results-root>/<arm>/<repo>_s<seed>/<timestamp>__...`, so the two parent
    components ARE the cell; deriving it here means no extra plumbing through the driver.
    """
    parent = os.path.dirname(os.path.normpath(run_dir_path))
    return os.path.join(os.path.basename(os.path.dirname(parent)), os.path.basename(parent))


def set_optimizer_context(**fields):
    """Record which task-run subsequent optimizer calls belong to. Cheap and idempotent."""
    _OPT_CTX.clear()
    _OPT_CTX.update({k: v for k, v in fields.items() if v is not None})


# Which mechanism made a between-task optimizer call. The `optimizer_call(prompt, model)`
# seam deliberately carries no label -- it is the core package's public runner interface and
# the mechanisms reach it through `runner(prompt, model)` -- so the kind is recovered from
# the prompt's own leading system text, which is authored in this repo and is distinctive.
# Without it the log groups by cell but not by MECHANISM, where it should hold one
# attributable line per proposer / tool judge / spiral judge / nudge-writer call.
# Unrecognised prompts log kind=None rather than guessing.
_OPT_KINDS = (
    ("proposer",     "You mine a DURABLE, REPO-SPECIFIC operating manual"),
    ("proposer",     "You maintain a SHORT list of durable, GENERAL operating notes"),
    ("proposer",     "You record durable, FILE-SPECIFIC structural knowledge"),
    ("proposer",     "You record a durable SUBSYSTEM MAP"),
    ("tool_judge",   "You configure the toolset for an autonomous coding agent"),
    ("spiral_judge", "You monitor an AI coding agent that is partway through ONE task"),
    ("nudge_writer", "An AI coding agent is stuck in a REPETITIVE, UNPRODUCTIVE loop"),
    ("detector",     "You audit an AI coding agent's PROCESS trace"),
    # NOT a mechanism call: the endpoint health probe `_preflight_optimizer_endpoint`
    # makes once per runner construction. It goes through the same seam, so it lands in
    # this log -- and it does so BEFORE any task, so `_OPT_CTX` is empty and its row also
    # carries cell=None. Labelling it is what lets a per-cell overhead groupby exclude it;
    # unlabelled, it can be most of a short run's log while reading as "unrecognised
    # prompt", which is a different and much more alarming thing.
    ("preflight",    "Reply with the single word: ok"),
)


def optimizer_call_kind(prompt):
    """'proposer'|'tool_judge'|'spiral_judge'|'nudge_writer'|'detector'|'preflight'|None.

    `preflight` is the endpoint health probe, not a mechanism -- filter it out before
    attributing overhead (it is also the one kind whose row has no `cell`).
    """
    head = (prompt or "")[:600]
    for kind, sig in _OPT_KINDS:
        if sig in head:
            return kind
    return None


def _log_optimizer_call(model, prompt, text, usage, stop_reason=None):
    path = optimizer_log_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **_OPT_CTX,
                "kind": optimizer_call_kind(prompt),
                "model": model, "usage": usage, "stop_reason": stop_reason,
                "truncated": stop_reason == "max_tokens",
                "prompt_head": prompt[:400], "prompt_tail": prompt[-400:],
                "prompt_chars": len(prompt),
                "response": text[:4000], "response_chars": len(text),
            }) + "\n")
    except Exception as e:                      # observability must never break a run
        _log(f"could not write optimizer log: {e}")


def _log(msg):
    if os.environ.get("TE_VERBOSE", "1") == "1":
        print(f"[te_harness] {msg}", flush=True)
        sys.stdout.flush()


def _run(cmd, timeout=120, check=False):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... -> rc={p.returncode}\n{p.stderr[-800:]}")
    return p


def image_for(instance_id):
    """Official image tag for an instance ('a__b-1' -> 'sweb.eval.x86_64.a_1776_b-1')."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def cli_version(binary):
    """`claude --version` string, so a run records the exact CLI build that produced it."""
    try:
        return _run([binary, "--version"], timeout=30).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def claude_bin():
    """Host path to the Claude Code binary, resolved through any version symlink."""
    p = os.environ.get("TE_CLAUDE_BIN") or shutil.which("claude")
    if not p:
        raise RuntimeError("no claude binary found; set TE_CLAUDE_BIN")
    return os.path.realpath(p)


# --------------------------------------------------------------------------------------
# grading: the OFFICIAL SWE-bench eval script + log parser, run in the task's container
# --------------------------------------------------------------------------------------
def swebench_repo():
    """Checkout supplying the official grader. No default: it is a site-specific path, and
    a wrong one does not fail loudly -- it fails as an ImportError deep in grading, after
    a task has already been paid for. Set TE_SWEBENCH_REPO (see .env.example)."""
    repo = os.environ.get("TE_SWEBENCH_REPO")
    if not repo:
        raise RuntimeError(
            "no SWE-bench checkout: set TE_SWEBENCH_REPO to one supplying the official "
            "grader (swebench.harness.grading) -- see the repo's .env.example")
    return os.path.realpath(repo)


def default_grading_path():
    """instance_id -> grading-fields JSON. TE_SWEBENCH_GRADING, else under $DATA."""
    explicit = os.environ.get("TE_SWEBENCH_GRADING")
    if explicit:
        return explicit
    data = os.environ.get("DATA")
    if data:
        return os.path.join(data, "swebench", "swebench_grading.json")
    raise RuntimeError(
        "no grading table: set TE_SWEBENCH_GRADING to the instance_id -> grading fields "
        "JSON (or DATA to the root holding swebench/swebench_grading.json) -- see the "
        "repo's .env.example")


def _import_swebench():
    repo = swebench_repo()
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from swebench.harness.test_spec.test_spec import make_test_spec
    from swebench.harness.grading import get_eval_report
    return make_test_spec, get_eval_report


# Tools that reach the public internet. Both benchmarks' answers are ON the public
# internet -- SWE-bench instances are GitHub issues whose fixing PR and gold patch are a
# fetch away, and Web-Bench ships every project's grading spec in `bytedance/web-bench`.
# An agent that can browse is therefore not solving the task, it is retrieving the answer,
# and the resolve verdict measures nothing.
#
# This is not hypothetical: given the tools, newer models do reach for the network on these
# tasks and pull something patch-shaped -- a fixing PR's files, a commit's `.patch`, a
# project's test spec -- and a patch retrieved that way grades RESOLVED like any other.
# Whether an agent browses is a matter of model behaviour, so only removing the tools
# makes the verdict independent of it.
#
# Blocked for EVERY arm, deliberately. Leaving it to the untool mechanism would be worse
# than useless: untool drops a tool only after observing DISUSE, so precisely the agents
# that browse keep the ability, and the arms would differ in whether they can cheat.
# `TE_ALLOW_NETWORK_TOOLS=1` lifts it for a run that genuinely wants a browsing agent.
NETWORK_TOOLS = ("WebSearch", "WebFetch")


def network_tool_block():
    """The network tools to add to `--disallowed-tools`, or () when explicitly allowed."""
    if os.environ.get("TE_ALLOW_NETWORK_TOOLS", "").strip() in ("1", "true", "yes"):
        return ()
    return NETWORK_TOOLS


class _Grader:
    """Builds the official eval script per instance and scores its log.

    `get_eval_report` is the same function the official `run_evaluation` uses, so a
    `resolved` here means what it means on the leaderboard: every FAIL_TO_PASS test now
    passes and no PASS_TO_PASS test regressed.
    """

    def __init__(self, grading_path=None):
        self.path = grading_path or default_grading_path()
        with open(self.path, encoding="utf-8") as fh:
            self.rows = json.load(fh)
        self.make_test_spec, self.get_eval_report = _import_swebench()

    def instance(self, instance_id):
        row = self.rows.get(instance_id)
        if row is None:
            raise KeyError(f"{instance_id} not in {self.path}")
        out = dict(row)
        out["instance_id"] = instance_id
        # make_test_spec wants the dataset's native shape: list fields may be JSON strings.
        for k in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            if isinstance(out.get(k), str):
                out[k] = json.loads(out[k])
        out.setdefault("problem_statement", "")
        return out

    def eval_script(self, instance_id):
        return self.make_test_spec(self.instance(instance_id)).eval_script

    def score(self, instance_id, model_patch, log_path):
        spec = self.make_test_spec(self.instance(instance_id))
        pred = {"instance_id": instance_id, "model_patch": model_patch,
                "model_name_or_path": "te_harness"}
        report = self.get_eval_report(test_spec=spec, prediction=pred,
                                      test_log_path=log_path, include_tests_status=True)
        r = report.get(instance_id, {})
        return bool(r.get("resolved")), r


# --------------------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------------------
def _cpu_limit_args():
    """`--cpus` for the task container, plus the thread caps that have to agree with it.

    An unlimited container sees every core on the HOST -- `nproc` inside one on a 384-core
    box reports 384 -- and the agent's own verification step routinely runs the repo's test
    suite, which for scientific repos is parallel by default: one agent-issued
    `pytest -k "... or common"` on scikit-learn holds ~250 cores for minutes
    (cpu-time/elapsed) on a 384-core host. That is the agent checking its work, not the
    grader, so it recurs on any task where the model picks a broad selector.

    `--cpus` ALONE IS NOT ENOUGH, and this is the part that is easy to get wrong: it caps
    total CPU but does not change what `nproc` reports, so joblib would still size its
    LokyProcess pool at 384 and thrash inside a 16-core quota -- slower than no cap at all.
    The thread caps are what actually bound the pool, and they must match the quota.

    Default 16: this harness is written for shared hosts, where an uncapped lane is
    antisocial. `TE_SWEBENCH_MAX_CPUS=0` removes the cap. The
    value is recorded in run_config.json -> `cpu_limit` because it shapes wall-clock (and,
    via TE_TASK_TIMEOUT / TE_EVAL_TIMEOUT, can shape resolve).
    """
    raw = (os.environ.get("TE_SWEBENCH_MAX_CPUS", "16") or "").strip()
    try:
        n = int(float(raw))
    except ValueError:
        n = 16
    if n <= 0:
        return []
    args = ["--cpus", str(n)]
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "LOKY_MAX_CPU_COUNT"):
        args += ["-e", f"{k}={n}"]
    return args


class ClaudeSwebenchRunner:
    """AgentRunner: Claude Code in the official image, graded by the official harness."""

    # What a scaffold needs to know about THIS agent (see scaffolds/base.AgentProfile).
    # Claude Code reads project instructions from CLAUDE.md and takes an MCP config file
    # directly; the container runs as root with a writable HOME under /tmp.
    scaffold_profile = scaffolds_pkg.AgentProfile(
        kind="claude",
        home="/tmp/agent-home",
        instructions_file="CLAUDE.md",
        nav_tools=("Read", "Grep", "Glob"),
        mcp_args=lambda cfg: ["--mcp-config", cfg],
        patch_excludes=("CLAUDE.md",))

    def __init__(self, agent="claude", model=None, grading_path=None, scaffold=None):
        self.agent = agent
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        # The Anthropic API directly, on ANTHROPIC_API_KEY -- the one endpoint this lane
        # bills. There is no argument for it: see resolve_anthropic_key.
        self.api_key = resolve_anthropic_key()
        # The mechanisms' between-task LLM calls (proposer, tool judge, spiral judge, nudge
        # writer) run on THE AGENT'S OWN MODEL, always. Their cost is charged to the same
        # ledger as the task rows and compared against a baseline that made no such calls,
        # so a cheaper proposer would understate the overhead the arm actually incurs --
        # the economized arm would look better for having billed its own thinking at a
        # discount. One model per cell is also what the cell name claims. There is no knob:
        # TE_OPTIMIZER_MODEL is refused rather than half-honoured.
        if os.environ.get("TE_OPTIMIZER_MODEL"):
            raise RuntimeError(
                "TE_OPTIMIZER_MODEL is no longer honoured: the between-task optimizer "
                "runs on the agent's own model so mechanism overhead is priced on the "
                "same basis as the task rows. Unset it.")
        self.optimizer_model = self.model
        # Reasoning effort is part of the cost basis, not a detail: it moves both the token
        # spend and the resolve rate, so it must be set EXPLICITLY rather than left to
        # whatever the installed CLI defaults to. Default is 'high'.
        self.effort = (os.environ.get("TE_EFFORT_LEVEL")
                       or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL") or "high")
        if self.effort not in EFFORT_LEVELS:
            raise RuntimeError(f"TE_EFFORT_LEVEL={self.effort!r} is not one of "
                               f"{sorted(EFFORT_LEVELS)}")
        # An external tool scaffold (rtk / serena / repomap) -- a BASELINE, not a
        # mechanism. Resolved and preflighted here so a misconfigured one (no host cache
        # for rtk's binary, no aider for the repo map) kills the lane before task 0
        # instead of writing a whole stream of rows that merely look like a scaffold result.
        self.scaffold_name = scaffold or None
        self.scaffold = scaffolds_pkg.get(self.scaffold_name)
        if self.scaffold is not None:
            self.scaffold.preflight(self.scaffold_profile)
            _log(f"scaffold: {self.scaffold.name} (external baseline)")
        self.claude = claude_bin()
        # Same early pricing check the Copilot runners make: an unpriced model id otherwise
        # raises from pricing.effective_tokens at the END of run_task, after the rollout has
        # been paid for and graded. Claude ids resolve to the Claude curve, so this only
        # ever fires on a genuinely unknown id.
        pricing.weights_for({"model": self.model, "agent": self.agent})
        # Generic slots the shared run_task/_write_run_config read, so an agent
        # subclass only overrides launch + accounting, not the whole task lifecycle.
        self.agent_bin = self.claude
        self.protected_tools = PROTECTED_TOOLS
        # The endpoint this run actually billed against, recorded verbatim in
        # run_config.json. An agent subclass MUST set it: defaulting to Anthropic here
        # would stamp api.anthropic.com onto rows that never touched it.
        self.routing_base = _ANTHROPIC_DIRECT
        self.cli_version = cli_version(self.claude)
        self.grader = _Grader(grading_path)
        # The mechanisms' judges answer with one JSON entry PER TOOL, which runs past a
        # small cap; a truncated answer is not a partial verdict but an unparseable one, so
        # the mechanism silently returns "drop nothing" while still billing for the call.
        # The cap covers THINKING + response, and a manual-distill (injection) call can
        # spend all of an 8192 cap on thinking and emit a ZERO-character answer, paying full
        # freight for nothing. Thinking is p50 ~4.8k but the observed distribution is
        # CENSORED at 8192, so 16384 is a doubling with room for the JSON, not a measured
        # ceiling; the truncation warning below is what says it is still not enough.
        # Nudge-judge calls stay far below it (max 713 output tokens observed), so the cap
        # binds only on the injection path.
        self.optimizer_max_tokens = int(os.environ.get("TE_OPTIMIZER_MAX_TOKENS", "16384"))
        self.task_timeout = int(os.environ.get("TE_TASK_TIMEOUT", "1800"))
        self.eval_timeout = int(os.environ.get("TE_EVAL_TIMEOUT", "1800"))
        self.keep = os.environ.get("TE_KEEP_CONTAINERS") == "1"
        # Stated rather than left to run_config's getattr default: this harness makes its
        # mechanism calls as plain Messages requests, the transport the Copilot harness
        # uses too whenever its provider speaks it. Naming it here means the field
        # describes a decision instead of an absence.
        self.optimizer_transport = "api"
        self._preflight_optimizer_endpoint()

    def _preflight_optimizer_endpoint(self):
        """Refuse the lane if the mechanism endpoint will not answer. See the shared
        helper for why a silent fallback is worse than a hard failure."""
        preflight_optimizer_endpoint(
            api_key=self.api_key, configured_model=self.optimizer_model, label="claude",
            remedy="Check that ANTHROPIC_API_KEY is valid and can call MODEL.")

    # ---- 1. the paid optimizer call the mechanisms make between tasks ------------------
    def optimizer_call(self, prompt, model="agent"):
        """One small direct Messages-API request. Returns (text, effective_tokens).

        `effective_tokens` is cache-aware and priced with the SAME weight table the task
        rows use, so mechanism overhead lands in the ledger on the task cost's own scale
        rather than as a raw token count.
        """
        return anthropic_optimizer_call(
            prompt, model=model, configured_model=self.optimizer_model,
            max_tokens=self.optimizer_max_tokens, api_key=self.api_key)

    # ---- 2. container plumbing --------------------------------------------------------
    def _reap_exited(self, name):
        """Remove EXITED containers from earlier attempts at this same cell.

        Names are unique per task-run (see `_container_name`), which is what makes two
        lanes on one cell safe -- but it also means the exact-name `docker rm -f <name>` in
        `_start` never reaps what a previously killed run left behind, and a hard-killed
        driver would leak a container per attempt.

        So reap by the readable stem instead, and ONLY containers that have exited. A
        RUNNING container sharing this stem belongs to a live lane working the same cell,
        and force-removing it would delete that lane's container mid-task. Best-effort
        throughout -- failing to reap is untidy, not incorrect.
        """
        stem = name.rsplit("-", 1)[0]
        try:
            p = _run(["docker", "ps", "-a", "--filter", f"name=^{stem}-",
                      "--filter", "status=exited", "--format", "{{.Names}}"], timeout=60)
            for stale in p.stdout.split():
                if stale != name:
                    _run(["docker", "rm", "-f", stale], timeout=60)
        except Exception:
            pass

    # ---- 1b. project-environment activation ---------------------------------------
    def _activate_project_env(self, name):
        """Discover the image's project env and record how it will be activated.

        Called at the end of every runner's `_start`. Measures rather than assumes: the
        env dir is listed, the PATH the container was built with is read back, and the
        resulting `python` is resolved -- so `run_config.json -> env_activation` says
        what the agent's shell actually saw, per task.
        """
        store = self.__dict__.setdefault("_env_activation", {})
        if not env_activation_enabled():
            store[name] = {"mode": "none", "requested": False}
            return store[name]
        script = (f'printf "PATH=%s\\n" "$PATH"; for d in {_CONDA_ENVS_DIR}/*/bin; do '
                  '[ -d "$d" ] && printf "ENV=%s\\n" "$d"; done; true')
        r = _run(["docker", "exec", name, "bash", "-c", script], timeout=120)
        container_path, env_bins = "", []
        for line in (r.stdout or "").splitlines():
            if line.startswith("PATH="):
                container_path = line[5:].strip()
            elif line.startswith("ENV="):
                env_bins.append(line[4:].strip())
        env_bin = choose_project_env(env_bins)
        if not env_bin:
            _log(f"env activation: no project env found under {_CONDA_ENVS_DIR} "
                 f"(candidates: {env_bins}) -- agent runs with the image's PATH")
            store[name] = {"mode": "none", "requested": True, "candidates": env_bins}
            return store[name]
        env = activation_env(env_bin, container_path)
        chk = _run(["docker", "exec", "-e", f"PATH={env['PATH']}", name, "bash", "-c",
                    'command -v python; python -c "import sys; print(sys.prefix)"'],
                   timeout=120)
        out = (chk.stdout or "").split()
        python = out[0] if out else None
        prefix = out[1] if len(out) > 1 else None
        info = {"mode": "path-prepend", "requested": True, "env_bin": env_bin,
                "env_name": env["CONDA_DEFAULT_ENV"], "python": python,
                "verified": bool(python and python.startswith(env_bin))}
        info["env"] = env
        if not info["verified"]:
            _log(f"env activation: PATH prepend did not resolve python to {env_bin} "
                 f"(got {python!r}, prefix {prefix!r})")
        store[name] = info
        return info

    def _env_activation_info(self, name):
        return self.__dict__.get("_env_activation", {}).get(name) or {"mode": "none"}

    def _env_activation_args(self, name):
        """`docker exec -e` args for the agent process. Empty when nothing was activated."""
        info = self._env_activation_info(name)
        if info.get("mode") != "path-prepend":
            return []
        out = []
        for k, v in info["env"].items():
            out += ["-e", f"{k}={v}"]
        return out

    def _record_env_activation(self, run_dir_path, name, row):
        """Write the measured activation into run_config.json and the row."""
        info = dict(self._env_activation_info(name))
        info.pop("env", None)
        row["env_activation"] = info.get("mode")
        cfg_path = os.path.join(run_dir_path, "run_config.json")
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            cfg["env_activation"] = info
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, default=str)
        except OSError as e:
            _log(f"could not record env_activation in run_config.json: {e}")

    def _start(self, name, instance_id):
        _run(["docker", "rm", "-f", name])
        self._reap_exited(name)
        cmd = ["docker", "run", "-d", "--name", name, "--entrypoint", "bash",
               "-v", f"{self.claude}:/usr/local/bin/claude:ro",
               "-e", "HOME=/tmp/agent-home", "-w", "/testbed"] + _cpu_limit_args()
        if self.scaffold is not None:
            for spec in self.scaffold.mounts(self.scaffold_profile):
                cmd += ["-v", spec]
        _LIVE_CONTAINERS.add(name)
        # Container start is the one step whose latency is set by the HOST, not by us: on
        # a loaded shared box `docker run -d` can take many minutes. A timeout here costs
        # the task (`run_task` records it unresolved, `container_start_failed`, and the
        # stream goes on) and can leave the container to finish starting on its own.
        # Configurable, and generous by default.
        p = _run(cmd + [image_for(instance_id), "-lc", "sleep infinity"],
                 timeout=int(os.environ.get("TE_DOCKER_START_TIMEOUT", "900")))
        if p.returncode != 0:
            raise RuntimeError(f"docker run failed for {instance_id}: {p.stderr[-500:]}")
        _run(["docker", "exec", name, "mkdir", "-p", "/tmp/agent-home"])
        self._activate_project_env(name)
        return name

    def _exec(self, name, script, timeout=300, workdir="/testbed"):
        return _run(["docker", "exec", "-w", workdir, name, "bash", "-lc", script],
                    timeout=timeout)

    # ---- 2b. the network guard (every arm) ---------------------------------------------
    # Container-side twin of webbench's `_install_net_guard`. The agent runs INSIDE the task
    # container, so the hook script, its registration and its log all have to live there --
    # a host-side settings.json is never read by the CLI in the container.
    #
    # WHY THIS AND NOT THE TOOL MASK. `network_tool_block()` drops WebSearch/WebFetch, but
    # the shell is the BIGGER route to the network: an answer-key-shaped URL (`.patch`,
    # `/commit/`, `api.github.com/search/...`) is one `curl` away, no web tool needed.
    # `Bash` is in PROTECTED_TOOLS and can never be masked, so the hook is the only seam.
    # Derived from the AGENT PROFILE's home, never hardcoded: the Claude container runs
    # HOME=/tmp/agent-home and the Copilot one HOME=/home/agent, so a literal path here
    # would hand the Copilot runner a directory its `_start` never creates, and `docker cp`
    # would fail on every task.
    @property
    def NET_GUARD_SCRIPT(self):
        return f"{self.scaffold_profile.home}/net_guard.py"

    @property
    def NET_GUARD_LOG(self):
        return f"{self.scaffold_profile.home}/net_guard_blocks.jsonl"

    def _install_net_guard(self, name):
        """Register the PreToolUse guard inside the task container. True when armed.

        Applies to EVERY arm. An agent that can fetch the instance's own fix commit makes
        the resolve verdict meaningless, which is not an arm-level concern -- and leaving
        it to untool would be worse than useless, since untool drops a tool only after
        observing disuse, so precisely the agents that browse would keep the ability.

        APPENDED to whatever is already in `PreToolUse`, and installed AFTER the scaffold.
        `Scaffold.merge_json_file` merges one level deep, so `hooks` is dict-updated and a
        scaffold's `PreToolUse` list REPLACES the whole list rather than extending it: an
        rtk arm registering after us would silently carry rtk's hook and not ours. Order
        costs nothing -- a scaffold's own setup runs through `docker exec`, not the agent's
        Bash tool, so no hook ever applied to it.

        Never raises. A task whose guard failed to install must still produce a row, but
        the row says which it is (`net_guard_installed`), so an unguarded row can be
        filtered out instead of being silently pooled with guarded ones.
        """
        if not network_tool_block():   # TE_ALLOW_NETWORK_TOOLS lifts BOTH halves, or neither
            return False
        try:
            cp = _run(["docker", "cp", net_guard.__file__,
                       f"{name}:{self.NET_GUARD_SCRIPT}"], timeout=120)
            if cp.returncode != 0:
                raise RuntimeError((cp.stderr or "")[-200:])
            # The interpreter is resolved INSIDE the image and written ABSOLUTE. The CLI
            # spawns a hook without a login shell, so a bare `python3` is not guaranteed to
            # be on its PATH, and a hook that always errors is worse than none: the CLI
            # reports the failure while the agent keeps going, so the run looks guarded and
            # is not. `net_guard` imports only the stdlib, so any interpreter will serve.
            probe = self._exec(name, "command -v python3 || command -v python", timeout=60)
            pys = [ln for ln in (probe.stdout or "").split() if ln.startswith("/")]
            if not pys:
                raise RuntimeError("no python interpreter found in the image")
            if not self._register_net_guard(name, pys[0]):
                raise RuntimeError("hook registration did not report success")
            # PROVE IT BLOCKS, rather than trusting that registration reported success.
            # Registration succeeding says the config file was written or the plugin
            # installed -- neither of which means the CLI reads it, that the matcher name
            # is right, or that the guard can find the command in the payload it is handed
            # (the two CLIs spell that differently -- guards/net_guard.hook_command). Any
            # of those can fail silently while every provenance field reads healthy, so the
            # only trustworthy check is a real refusal.
            self._verify_net_guard(name, pys[0])
            return True
        except Exception as e:
            _log(f"net guard install FAILED ({type(e).__name__}: {e}) -- row runs UNGUARDED")
            return False

    def _install_answer_key_hosts(self, name):
        """Black-hole the answer-key hosts in the container's /etc/hosts. True when done.

        THE PRIMARY CONTROL, and the one that does not depend on the agent's cooperation.
        The PreToolUse hook above runs only where the CLI's hook contract is met -- each
        lane's file, key and matcher, and on Codex `--dangerously-bypass-hook-trust` as
        well -- and it sees only the shell tool. A layer that needs none of that is what
        makes the guard hold when one of those assumptions breaks with a CLI release.

        This layer catches every fetch route an agent has here, because all of them
        resolve a hostname first: `curl`, `wget`, `git clone`, and
        `python -c "import urllib.request"`. Verified in a real SWE-bench container: each
        fails with "Connection refused" afterwards while pypi.org, files.pythonhosted.org
        and a real `pip download` keep working -- which is the whole point of resolving a
        NAME LIST rather than cutting the container's network off. Applied to every arm,
        baseline included: an agent that can read the instance's own fix commit makes the
        resolve verdict meaningless, which is not an arm-level concern.

        Never raises. A task whose block failed must still produce a row, but the row says
        which it is so an unguarded row can be filtered rather than silently pooled.
        """
        if not network_tool_block():   # TE_ALLOW_NETWORK_TOOLS lifts every layer, or none
            return False
        try:
            # Appended, and idempotently: the image's own /etc/hosts carries the container
            # identity, and replacing it would break name resolution for the container
            # itself. `grep -q` keeps a re-exec from stacking duplicate stanzas.
            blob = base64.b64encode(net_guard.hosts_block().encode()).decode()
            r = self._exec(
                name,
                f"grep -q {shlex.quote(net_guard.HOSTS_MARKER)} /etc/hosts "
                f"|| echo {blob} | base64 -d >> /etc/hosts; "
                f"grep -c {shlex.quote(net_guard.HOSTS_MARKER)} /etc/hosts",
                timeout=60)
            if (r.stdout or "").strip().splitlines()[-1:] != ["1"]:
                raise RuntimeError((r.stderr or r.stdout or "")[-200:])
            # PROVE IT REFUSES, rather than trusting that the file was written. A write can
            # succeed into a container whose resolver ignores /etc/hosts (musl with a
            # different nsswitch, a VPN resolver), and that failure is invisible: the file
            # looks right and every fetch still succeeds.
            probe = self._exec(
                name,
                "getent hosts github.com | awk '{print $1}' | head -1", timeout=60)
            if "127.0.0.1" not in (probe.stdout or ""):
                raise RuntimeError(
                    "hosts stanza written but github.com still resolves to "
                    f"{(probe.stdout or '').strip()!r} -- this container's resolver is not "
                    "reading /etc/hosts, so nothing is blocked")
            return True
        except Exception as e:
            _log(f"answer-key hosts block FAILED ({type(e).__name__}: {e}) "
                 "-- row runs UNGUARDED at the DNS layer")
            return False

    def _answer_key_attempts(self, run_dir_path):
        """This task's answer-key-shaped command count. See `answer_key_attempts`."""
        return answer_key_attempts(run_dir_path)

    @staticmethod
    def _net_guard_merge_program(entry):
        """Python, run INSIDE the container, that appends `entry` to the `PreToolUse` list
        of the JSON hook file named by argv[1], idempotently, and prints `armed`.

        Claude Code's settings.json and Codex's hooks.json share this shape -- a top-level
        `hooks` object keyed by PascalCase event name -- so one program serves both lanes."""
        return (
            "import json, os, sys\n"
            "p = sys.argv[1]\n"
            f"entry = json.loads({json.dumps(json.dumps(entry))})\n"
            "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "try:\n"
            "    cur = json.load(open(p))\n"
            "except Exception:\n"
            "    cur = {}\n"
            "pre = cur.setdefault('hooks', {}).setdefault('PreToolUse', [])\n"
            "if not any('net_guard' in json.dumps(h) for h in pre):\n"
            "    pre.append(entry)\n"
            "json.dump(cur, open(p, 'w'), indent=2)\n"
            "print('armed')\n")

    def _write_hook_entry(self, name, python, path):
        """Append the guard's PreToolUse entry to the hook file at `path` in the container."""
        entry = {"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{python} {self.NET_GUARD_SCRIPT}"}]}
        prog = self._net_guard_merge_program(entry)
        r = self._exec(
            name,
            f"cat > /tmp/te_net_guard_install.py <<'TE_EOF'\n{prog}\nTE_EOF\n"
            f"{python} /tmp/te_net_guard_install.py {path}", timeout=60)
        if r.returncode != 0 or "armed" not in (r.stdout or ""):
            raise RuntimeError(((r.stderr or "") + (r.stdout or ""))[-200:])
        return True

    def _register_net_guard(self, name, python):
        """Claude Code: append a PreToolUse hook to the agent's own settings.json."""
        return self._write_hook_entry(
            name, python, f"{self.scaffold_profile.home}/.claude/settings.json")

    # The probe command is one the guard MUST refuse and that reaches no network even if
    # the guard is missing: `decide` matches on the substring `github` alone, so `echo`
    # is enough to exercise the whole path without a request leaving the container.
    NET_GUARD_PROBE = "echo https://github.com/probe"

    def _verify_net_guard(self, name, python):
        """Run the guard against a payload the CLI would send. Raises unless it refuses.

        Feeds the hook script directly rather than driving the agent: a real agent turn
        would cost a paid call per task, and what can break here -- payload spelling,
        interpreter path, script not copied -- is all exercised by the script itself.

        **What this does NOT cover**: whether the CLI actually invokes the hook. That rests
        on the settings-file location, the hook key and the matcher name, which are per
        agent (`_register_net_guard`) and were each verified by hand against the live CLIs
        on 2026-09-08 -- not by this check. If you change any of those three, re-probe
        end to end with a real agent turn; a green `net_guard_installed` will not catch it.
        """
        payload = json.dumps({"tool_name": "Bash", "toolName": "bash",
                              "tool_input": {"command": self.NET_GUARD_PROBE},
                              "toolArgs": {"command": self.NET_GUARD_PROBE}})
        r = self._exec(
            name,
            f"printf '%s' {shlex.quote(payload)} | {python} {self.NET_GUARD_SCRIPT}; "
            "echo rc=$?",
            timeout=60)
        if "rc=2" not in (r.stdout or ""):
            raise RuntimeError(
                "guard installed but did NOT refuse its probe (expected rc=2, got "
                f"{(r.stdout or '').strip()[-80:]!r}) -- it would block nothing")

    def _net_guard_blocks(self, name):
        """How many Bash commands the guard refused this task. No log = it never fired.

        Read while the container is still alive -- `_stop` removes it -- and recorded on
        EVERY row, zero included: a guard is only trustworthy if you can watch it fire, and
        a model that starts routing around it shows up here as a count that falls to zero
        while the traces still contain fetches.
        """
        try:
            p = self._exec(name, f"wc -l < {self.NET_GUARD_LOG} 2>/dev/null || echo 0",
                           timeout=60)
            return int((p.stdout or "0").strip().split()[0])
        except Exception:
            return 0

    # ---- 2b. external tool scaffold (baseline arms only) ------------------------------
    def _scaffold_context(self, name, task, run_dir_path):
        """The handles a scaffold may use. Docker is reached only through these."""
        def exec_(script, timeout=300, env=None):
            cmd = ["docker", "exec", "-w", "/testbed"]
            for key, value in (env or {}).items():
                cmd += ["-e", f"{key}={value}"]
            return _run(cmd + [name, "bash", "-lc", script], timeout=timeout)

        def copy_in(host_path, container_path):
            return _run(["docker", "cp", host_path, f"{name}:{container_path}"],
                        timeout=300)

        def read_bytes(script, timeout=600):
            """Raw stdout of a container script. Binary, because the repo-map baseline
            streams a tar out this way and decoding it as text would corrupt it."""
            proc = subprocess.run(
                ["docker", "exec", name, "bash", "-lc", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            if proc.returncode != 0:
                raise scaffolds_pkg.ScaffoldError(
                    f"container script failed: {(proc.stderr or b'')[-200:]!r}")
            return proc.stdout

        return scaffolds_pkg.ScaffoldContext(
            profile=self.scaffold_profile, container=name, task=task,
            run_dir=run_dir_path, exec_=exec_, copy_in=copy_in, host_run=_run,
            read_bytes=read_bytes, log=_log)

    def _apply_scaffold(self, name, task, run_dir_path):
        """Install the arm's scaffold. Returns (notes, mcp_config).

        A scaffold that raises is recorded and the task continues NATIVE: one broken
        install must not cost the row, because a row flagged `scaffold_error` is
        diagnosable while a missing row is not.
        """
        if self.scaffold is None:
            return {}, None
        try:
            notes = self.scaffold.setup(self._scaffold_context(name, task, run_dir_path))
        except Exception as e:                                    # noqa: BLE001
            _log(f"{task['instance_id']}: scaffold {self.scaffold.name} FAILED "
                 f"({type(e).__name__}: {e}) -- task runs native, row flagged")
            return {"arm_setup": self.scaffold.name, "scaffold_error":
                    f"{type(e).__name__}: {e}"[:300]}, None
        return notes, notes.get("mcp_config")

    def _graded_diff(self, name):
        """The agent's diff, with every scaffold artifact subtracted.

        A scaffold writes project instructions INTO the repo, so those files are tracked
        by `git diff` and would be applied by the official grader -- scoring the
        scaffold's prompt text as part of the fix. The exclusions are git pathspecs, and
        what was excluded is recorded in run_config.json.
        """
        excludes = self._patch_excludes()
        # With no scaffold the command is left EXACTLY as it was before scaffolds existed.
        # `diff -- .` is equivalent from the repo root, but "equivalent" is not "identical",
        # and every stored baseline row was produced by the bare form.
        diff = "git -C /testbed diff"
        if excludes:
            spec = " ".join(f"':(exclude,top){e}'" for e in excludes)
            diff = f"{diff} -- . {spec}"
        return self._exec(
            name,
            f"git -C /testbed add -A -N >/dev/null 2>&1; {diff}").stdout

    def _patch_excludes(self):
        """Paths never counted as the agent's fix: the agent's own instructions file plus
        whatever the active scaffold writes. Sorted so run_config is stable."""
        if self.scaffold is None:
            return ()
        return tuple(sorted(set(self.scaffold_profile.patch_excludes)
                            | set(self.scaffold.patch_excludes)))

    def _stop(self, name):
        """Remove the task container. NEVER raises -- cleanup is untidy, not incorrect.

        This is called from `run_task`'s `finally`, so anything it raises escapes the
        per-task error handling and kills the whole STREAM -- and on a loaded shared box
        with rootless Docker, `docker rm -f` can outlast a 120 s timeout.

        The same reasoning `_start` already carries -- "a host that cannot start containers
        must not take the STREAM down with it" -- applies at least as strongly here, where
        the task is already graded and its row already built. A leaked container is
        reaped later by `_reap_exited`, so failing to remove one costs disk, not results.

        The timeout is also generous and configurable (TE_DOCKER_STOP_TIMEOUT): removal on
        a loaded rootless daemon is bounded by the HOST, not by us.
        """
        if self.keep:
            _log(f"keeping container {name} (TE_KEEP_CONTAINERS=1)")
            _LIVE_CONTAINERS.discard(name)
            return
        try:
            _run(["docker", "rm", "-f", name],
                 timeout=int(os.environ.get("TE_DOCKER_STOP_TIMEOUT", "600")))
        except Exception as e:
            _log(f"could not remove container {name} ({type(e).__name__}: {e}) -- "
                 "leaving it for _reap_exited; the task's row is unaffected")
        _LIVE_CONTAINERS.discard(name)

    # ---- 3. one streamed agent segment ------------------------------------------------
    def _segment(self, name, prompt, transcript_fh, exclude_tools, max_turns, guard,
                 mcp_config=None):
        """Run `claude -p` once, streaming stream-json. Every raw line is appended to the
        transcript (the mechanisms re-read exactly this file) and, when the spiral guard is
        active, folded into it live; a guard stop kills the agent mid-stream, leaving the
        container's working tree -- the agent's own edits -- intact for the next segment.

        Returns the run's final `result` event (or {} if the stream ended without one).
        """
        # SECRETS NEVER GO IN ARGV. `docker exec -e VAR` with no "=value" tells docker to
        # take the value from the CLIENT's environment, so the key is passed through the
        # Popen env below instead of appearing in the process table -- which on a shared
        # host is readable by every other user via `ps`.
        #
        # ANTHROPIC_API_KEY is the ONLY Anthropic variable the container receives. Every
        # variable is named here explicitly, so nothing that could point the CLI at another
        # endpoint or credential (IGNORED_ANTHROPIC_VARS) reaches it from the host.
        cmd = ["docker", "exec", "-i", "-w", "/testbed",
               "-e", "ANTHROPIC_API_KEY",
               "-e", "HOME=/tmp/agent-home",
               # Where the PreToolUse guard appends its refusals; the hook inherits this
               # from the CLI process that spawns it.
               "-e", f"{net_guard.LOG_ENV}={self.NET_GUARD_LOG}",
               # The project env, for every shell the CLI spawns -- see
               # _activate_project_env. After HOME.
               *self._env_activation_args(name),
               # SWE-bench images run as root, and the CLI refuses
               # --dangerously-skip-permissions as root unless it knows it is sandboxed --
               # which this disposable, per-task container genuinely is.
               "-e", "IS_SANDBOX=1",
               "-e", f"CLAUDE_CODE_EFFORT_LEVEL={self.effort}",
               "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
               "-e", "DISABLE_TELEMETRY=1", "-e", "DISABLE_AUTOUPDATER=1",
               name, "claude", "-p",
               "--output-format", "stream-json", "--verbose",
               "--dangerously-skip-permissions",
               "--model", self.model, "--max-turns", str(max_turns),
               # Explicit flag, not just the env var: this CLI exposes --effort directly,
               # and effort silently defaulting low would change both cost and resolve.
               "--effort", self.effort]
        drop = [t for t in (exclude_tools or []) if t not in self.protected_tools]
        # Network tools are dropped for every arm, on top of whatever untool decided --
        # see NETWORK_TOOLS: the answers to both benchmarks are a fetch away.
        drop += [t for t in network_tool_block() if t not in drop]
        if drop:
            cmd += ["--disallowed-tools", ",".join(drop)]
        # A scaffold's MCP server (Serena). Appended last so it cannot be reordered into
        # the middle of the tool mask.
        cmd += self.scaffold_profile.mcp_args(mcp_config)

        # The docker CLIENT's environment carries the secrets that `-e VAR` forwards.
        cenv = dict(os.environ)
        cenv["ANTHROPIC_API_KEY"] = self.api_key
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1, env=cenv)
        proc.stdin.write(prompt)
        proc.stdin.close()

        result, stopped, deadline = {}, False, time.time() + self.task_timeout
        # Usage is folded in as the stream arrives, because the break paths below kill the
        # CLI before its terminal `result` event: without this the segment's cost is
        # simply lost. See _UsageRecovery.
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
                _log("segment hit TE_TASK_TIMEOUT; stopping the agent")
                stopped, stop_reason = True, "task_timeout"
                break

        if stopped:
            # Kill the agent INSIDE the container: terminating the `docker exec` client
            # leaves the CLI running and still billing.
            _run(["docker", "exec", name, "pkill", "-TERM", "-f", "claude"])
            time.sleep(2)
            _run(["docker", "exec", name, "pkill", "-KILL", "-f", "claude"])
            proc.kill()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = (proc.stderr.read() or "")[-600:]
        if err.strip() and not result:
            _log(f"agent stderr: {err.strip()[:300]}")
        if not result:
            # No terminal `result` event: a guard/timeout stop, or a stream that died.
            # Recover what the transcript can prove rather than recording zeros, and take
            # `output_tokens` from the CLI's own session log, which the kill does not erase.
            result = recovery.synthesize(stop_reason or "no_result_event",
                                         self._session_log_text(name))
        return result

    def _session_log_text(self, name):
        """Claude Code's session log for this container, or '' if unreadable.

        Read AFTER the kill: the CLI appends each assistant message as it completes, so the
        file is already on disk and the kill cannot take it back. Globbed rather than
        composed from a slug -- the CLI derives the directory name from cwd
        (`/testbed` -> `-testbed`) and that derivation is its business, not ours. A nudge
        re-invokes the CLI and so writes a SECOND session file; both are concatenated and
        `session_output_tokens` deduplicates by message id, so double-counting is not
        possible from the overlap.

        Never raises and never fails a task: an unreadable log just means the row keeps an
        honest `output_tokens=0` and stays flagged.
        """
        home = self.scaffold_profile.home
        try:
            p = self._exec(name, f"cat {home}/.claude/projects/*/*.jsonl 2>/dev/null",
                           timeout=60)
            return p.stdout or ""
        except Exception as e:
            _log(f"session log unreadable ({type(e).__name__}: {e})")
            return ""

    # ---- 3a. spiral hooks (overridden per agent) --------------------------------------
    # The generic guard nudges by REPROMPTING (a fresh `-p` call carrying a redirect on top
    # of the agent's own on-disk edits) and reports its telemetry under `spiral_*`. The
    # Codex-native controller resumes the EXACT thread instead -- a fresh session would
    # re-pay for the context it already built -- so it takes no prompt, returns a resume
    # REQUEST rather than a string, and reports under `codex_spiral_*` because that is what
    # `mechanisms.spiral.SpiralStage.after_task` reads off the row. Three hooks rather than
    # a second copy of run_task: everything else about the lifecycle -- container, scaffold,
    # net guard, patch extraction, official grading -- is identical across all three agents.
    def _build_guard(self, run_kwargs):
        """The in-flight spiral guard for this task, or None when the arm has none."""
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
        # branches without trimech-safe -- see guard_accepts() for why it is probed.
        if guard_accepts("never_abort"):
            guard_kwargs["never_abort"] = bool(run_kwargs.get("spiral_never_abort"))
        return NudgeGuard(**guard_kwargs)

    def _guard_continuation(self, guard, prompt):
        """The next segment's input after a guard stop, or falsey to stop."""
        return guard.continuation(prompt)

    def _guard_row(self, guard):
        """Guard telemetry for the row."""
        return {
            "aborted": guard.aborted,
            "spiral_abort": guard.aborted,
            "spiral_nudges": guard.nudges_done,
            "spiral_abort_suppressed": getattr(guard, "abort_suppressed", False),
            "spiral_events": guard.events,
            "spiral_judge_tokens": guard.judge_tokens,
            "spiral_nudge_writer_tokens": guard.nudge_writer_tokens,
        }

    # ---- 3b. accounting hooks (overridden per agent) ----------------------------------
    # Claude Code reports its token breakdown in the stream's terminal `result` event, so
    # both hooks read that event. Copilot reports NO tokens there and must mine its debug
    # log instead (see te_harness.copilot), which is why these are hooks and not inlined:
    # everything else about run_task -- container lifecycle, spiral segments, patch
    # extraction, official grading -- is identical across the two agents.
    def _merge_segments(self, a, b):
        """Combine two spiral segments' usage into one."""
        return _merge_usage(a, b)

    def _row_from_segment(self, result):
        """Token/turn row fields from whatever _segment returned."""
        return _row_from_result(result)

    def _container_name(self, instance_id, arm, run_dir_path):
        """Docker container name for ONE task-run. Must be unique across live lanes.

        `_start` opens with `docker rm -f <name>`, so a name carrying no agent, model or
        seed (`te-<instance>-<arm>`) would let a Claude lane and a Copilot lane on the same
        (instance, arm) resolve to one name, and the second lane would DELETE the first
        one's container mid-grade. Nothing would report it: the grading `docker exec` comes
        back empty, and `get_eval_report` scores an empty `eval.log` as
        `patch_successfully_applied: false` -- a row that reads like a bad model rather than
        a lost container, on a lane that still exits 0.

        The run directory already encodes timestamp, stream index, agent, instance, arm,
        seed and mode, so a short digest of it is unique per task-run. The readable stem is
        kept so `docker ps` still says which instance and arm you are looking at.
        """
        stem = ("te-" + instance_id.replace("__", "-").replace("/", "-")
                + "-" + str(arm)).lower()
        key = f"{self.agent}|{self.model}|{os.path.abspath(run_dir_path)}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        # Docker caps names at 63 chars and wants [a-zA-Z0-9][a-zA-Z0-9_.-]*.
        stem = re.sub(r"[^a-z0-9_.-]", "-", stem)[:50].rstrip("-.")
        return f"{stem}-{digest}"

    # ---- 4. the AgentRunner entry point ------------------------------------------------
    def run_task(self, task, *, arm, injected, exclude_tools, run_dir_path, **run_kwargs):
        instance_id = task["instance_id"]
        set_optimizer_context(arm=arm, instance_id=instance_id, model=self.model,
                              agent=self.agent, cell=_cell_of(run_dir_path),
                              run_dir=os.path.basename(run_dir_path))
        os.makedirs(run_dir_path, exist_ok=True)
        name = self._container_name(instance_id, arm, run_dir_path)
        max_turns = int(run_kwargs.get("max_turns") or os.environ.get("TE_MAX_TURNS", 80))
        t0 = time.time()

        guard = self._build_guard(run_kwargs)

        prompt = build_prompt(task, injected)
        transcript = os.path.join(run_dir_path, "transcript.jsonl")
        row = {"instance": instance_id, "repo": task.get("repo", ""),
               "model": self.model, "agent": self.agent, "arm": arm,
               "effort": self.effort, "cli_version": self.cli_version}

        # Self-describing run: the CLI records neither the effort level nor the tool mask
        # in its transcript, so a finished run could not answer "what settings produced
        # this?" from its own artifacts. Write them down next to the transcript.
        self._write_run_config(run_dir_path, task, arm, injected, exclude_tools,
                               max_turns, run_kwargs, guard)

        # A host that cannot start containers must not take the STREAM down with it: a
        # TimeoutExpired/RuntimeError from `docker run -d` propagating out of run_task would
        # kill the launcher at whichever task it hit, and a wedged rootless daemon or an
        # intermittent stall can hit any task. Record the task as an unresolved,
        # explicitly-flagged error and let the stream continue; a run where every task
        # carries start_error is a legible infrastructure failure, not a silent crash.
        net_guard_installed, net_guard_blocks = False, 0
        hosts_blocked = False
        try:
            self._start(name, instance_id)
        except (subprocess.TimeoutExpired, RuntimeError) as e:
            _log(f"{instance_id}: CONTAINER START FAILED ({type(e).__name__}) "
                 f"-> unresolved (stream continues)")
            self._stop(name)
            row.update({"resolved": False, "is_error": True,
                        "api_error_status": "container_start_failed",
                        "eval_report": {"start_error": str(e)[:400]},
                        "num_turns": 0, "patch_bytes": 0,
                        "wall_s": round(time.time() - t0, 1)})
            # run_task only RETURNS the row; the driver persists row.json.
            return row, run_dir_path
        # Measured at _start; written here so a start failure above still leaves the
        # placeholder run_config intact and a started task records what its shell saw.
        self._record_env_activation(run_dir_path, name, row)
        try:
            # The scaffold installs BEFORE the agent starts, so its setup traffic is not
            # model traffic and is not charged to the arm (see scaffolds/__init__.py).
            scaffold_notes, mcp_config = self._apply_scaffold(name, task, run_dir_path)
            row.update(scaffold_notes)
            # AFTER the scaffold, so the guard extends the scaffold's hook list instead of
            # being replaced by it -- see `_install_net_guard`.
            net_guard_installed = self._install_net_guard(name)
            # The DNS layer, on every agent. Independent of the hook above and of each CLI's
            # hook contract, which is per agent and can break silently with a CLI release.
            hosts_blocked = self._install_answer_key_hosts(name)
            with open(transcript, "w", encoding="utf-8") as fh:
                result = self._segment(name, prompt, fh, exclude_tools, max_turns, guard,
                                       mcp_config=mcp_config)
                # Spiral rung 1: re-invoke in the SAME container on top of the agent's own
                # on-disk edits, with the leakage-safe redirect appended.
                while guard is not None:
                    nxt = self._guard_continuation(guard, prompt)
                    if not nxt:
                        break
                    _log(f"{instance_id}: spiral nudge -> segment {guard.segment}")
                    seg = self._segment(name, nxt, fh, exclude_tools, max_turns, guard,
                                        mcp_config=mcp_config)
                    result = self._merge_segments(result, seg)

            # Patch extraction and grading get the SAME protection container start has,
            # and for the same reason: a host-side stall must not take the STREAM down.
            # `_graded_diff` is a `docker exec ... git diff` with a 300 s cap, and on a
            # loaded shared box it can stall; a TimeoutExpired propagating out of run_task
            # would end the cell with every remaining task unrun. The agent's turn is
            # already bought by this point, so the honest thing is to keep the row, record
            # why it has no grade, and let the stream continue: a task with
            # `grading_error` is filterable, an ended stream is not recoverable.
            patch, resolved, report = "", False, {}
            try:
                patch = self._graded_diff(name)
                with open(os.path.join(run_dir_path, "model_patch.diff"), "w",
                          encoding="utf-8") as fh:
                    fh.write(patch)
                resolved, report = self._grade(name, instance_id, patch, run_dir_path)
            except (subprocess.TimeoutExpired, RuntimeError) as e:
                _log(f"{instance_id}: GRADING FAILED ({type(e).__name__}) "
                     f"-> unresolved, flagged (stream continues)")
                row["is_error"] = True
                row["api_error_status"] = "grading_failed"
                row["grading_error"] = f"{type(e).__name__}: {str(e)[:400]}"
                report = {"grading_error": row["grading_error"]}
        finally:
            # Read BEFORE the container is removed -- the log lives inside it.
            net_guard_blocks = self._net_guard_blocks(name)
            self._stop(name)

        row.update(self._row_from_segment(result))
        row["resolved"] = resolved
        row["eval_report"] = report
        row["patch_bytes"] = len(patch)
        row["wall_s"] = round(time.time() - t0, 1)
        row["untooled_excluded"] = list(exclude_tools or [])
        row["net_guard_installed"] = net_guard_installed
        row["net_guard_blocks"] = net_guard_blocks
        row["answer_key_hosts_blocked"] = hosts_blocked
        # Attempts, not refusals, and read back from the trace -- the agent-independent
        # number. See _answer_key_attempts.
        row["answer_key_attempts"] = self._answer_key_attempts(run_dir_path)
        if self.scaffold is not None:
            row["scaffold"] = self.scaffold.name
            row["patch_excludes"] = list(self._patch_excludes())
        if guard is not None:
            row.update(self._guard_row(guard))
        _log(f"{instance_id} arm={arm} resolved={resolved} turns={row.get('num_turns')} "
             f"eff_tok={pricing.effective_tokens(row):.0f} {row['wall_s']}s")
        return row, run_dir_path

    def _write_run_config(self, run_dir_path, task, arm, injected, exclude_tools,
                          max_turns, run_kwargs, guard):
        """Record everything that shapes this run into `run_config.json`.

        Anything that changes cost or resolve belongs here: the effort level and turn cap,
        the exact tool mask actually handed to the CLI (after PROTECTED_TOOLS filtering,
        which is what the agent really saw), the endpoint billed, the CLI build, and the
        spiral parameters. The injected manual is fingerprinted rather than inlined -- it
        can be long, and the state file already holds its text.
        """
        # The SAME two-step the CLI invocation performs (see `_segment`): recomputing only
        # the untool half here would record `excluded_applied: []` on every row and make a
        # guarded run byte-identical to an unguarded one in its own provenance.
        drop = [t for t in (exclude_tools or []) if t not in self.protected_tools]
        drop = drop + [t for t in network_tool_block() if t not in drop]
        cfg = {
            "instance_id": task.get("instance_id"), "repo": task.get("repo"), "arm": arm,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "max_turns": max_turns,
            "cli_version": self.cli_version,
            "agent_bin": self.agent_bin,
            "image": image_for(task["instance_id"]),
            "cpu_limit": _cpu_limit_args()[1] if _cpu_limit_args() else None,
            "routing": {
                "base_url": self.routing_base,
                "optimizer_model": self.optimizer_model,
                "optimizer_transport": getattr(self, "optimizer_transport", "api"),
            },
            # Placeholder; _record_env_activation overwrites it once the container is up.
            "env_activation": {"requested": env_activation_enabled(), "mode": None},
            "tools": {
                "excluded_requested": list(exclude_tools or []),
                "excluded_applied": drop,
                "protected": sorted(self.protected_tools),
            },
            "injection": {
                "present": bool(injected),
                "chars": len(injected or ""),
                "sha256": (hashlib.sha256(injected.encode()).hexdigest()[:16]
                           if injected else None),
            },
            "spiral": {
                "guard": bool(run_kwargs.get("spiral_guard")),
                "envelope": run_kwargs.get("abort_envelope"),
                "thresholds": run_kwargs.get("spiral_thr"),
                "judge": run_kwargs.get("spiral_judge") is not None,
                "nudge_writer_llm": run_kwargs.get("spiral_nudge_writer") is not None,
                "nudge_budget": getattr(guard, "nudge_budget", 0),
                "never_abort": getattr(guard, "never_abort", False),
            },
            "scaffold": {
                "name": self.scaffold.name if self.scaffold else None,
                # What was subtracted from the graded diff. Without this a reader cannot
                # tell an empty patch from one whose only change WAS the scaffold's file.
                "patch_excludes": list(self._patch_excludes()),
            },
            "timeouts": {"task_s": self.task_timeout, "eval_s": self.eval_timeout},
            "grading_source": self.grader.path,
        }
        with open(os.path.join(run_dir_path, "run_config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, default=str)

    def _grade(self, name, instance_id, patch, run_dir_path):
        """Run the official eval script in the container and score its log.

        The eval script resets the test files to base and applies the instance's
        `test_patch` itself, so any test edits the agent made are discarded exactly as the
        official pipeline discards them -- only source changes can earn a resolve.
        """
        if not patch.strip():
            _log(f"{instance_id}: empty diff -> unresolved without running tests")
            return False, {"empty_patch": True}
        script = self.grader.eval_script(instance_id)
        host_log = os.path.join(run_dir_path, "eval.log")
        # A hung eval must not take the STREAM down with it: a TimeoutExpired from these
        # docker execs propagating out of run_task would kill the launcher mid-stream, and
        # an instance whose eval never terminates (scikit-learn-14710, see
        # workloads.SWEBENCH_EXCLUDED) would end its cell at any eval_timeout. One
        # pathological instance is a scored failure, not a reason to lose the tasks behind
        # it. Whatever the script printed before the cap is still written to eval.log, so
        # the hang stays diagnosable.
        try:
            w = self._exec(name, "cat > /tmp/eval.sh <<'TE_EOF'\n" + script + "\nTE_EOF\n"
                                 "chmod +x /tmp/eval.sh")
            if w.returncode != 0:
                return False, self._eval_exec_failed(
                    instance_id, host_log, "", w.returncode, w.stderr, "writing eval.sh")
            p = self._exec(name, "/tmp/eval.sh 2>&1 | tail -c 4000000",
                           timeout=self.eval_timeout)
            out = p.stdout
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            with open(host_log, "w", encoding="utf-8") as fh:
                fh.write(out)
            _log(f"{instance_id}: EVAL TIMEOUT after {self.eval_timeout}s -> unresolved "
                 f"(stream continues; see eval.log)")
            return False, {"eval_timeout": self.eval_timeout}
        # An eval that produced NOTHING did not grade the patch -- it failed to run. A
        # container that disappeared under us (see _container_name) produces exactly this:
        # `docker exec` exits nonzero with the reason on ITS stderr while the
        # in-container `2>&1` captures nothing. Scoring that empty log would hand
        # get_eval_report a file with no APPLY_PATCH_PASS marker, which comes back as
        # `patch_successfully_applied: false, resolved: false` -- indistinguishable from a
        # genuine miss, on a lane that still exits 0. Flag it instead.
        if p.returncode != 0 and not out.strip():
            return False, self._eval_exec_failed(
                instance_id, host_log, out, p.returncode, p.stderr, "running eval.sh")
        with open(host_log, "w", encoding="utf-8") as fh:
            fh.write(out)
        if not out.strip():
            _log(f"{instance_id}: eval script exited 0 but printed NOTHING -> not graded")
            return False, {"eval_exec_failed": True, "eval_exec_rc": 0,
                           "eval_exec_error": "eval script produced no output"}
        try:
            return self.grader.score(instance_id, patch, host_log)
        except Exception as e:
            _log(f"{instance_id}: grading failed: {e}")
            return False, {"grading_error": str(e)}

    def _eval_exec_failed(self, instance_id, host_log, out, rc, stderr, phase):
        """Record a grading exec that never ran, loudly and distinguishably.

        Returns the eval_report dict. `resolved` is still False -- but the row says the
        task was NOT GRADED rather than graded-and-missed, so aggregate.py and a
        human reading the row can tell the two apart.
        """
        err = (stderr or "").strip()[-500:]
        try:
            with open(host_log, "w", encoding="utf-8") as fh:
                fh.write(out or "")
                if err:
                    fh.write(f"\n[te_harness] docker exec failed while {phase} "
                             f"(rc={rc}):\n{err}\n")
        except OSError:
            pass
        _log(f"{instance_id}: EVAL DID NOT RUN while {phase} (rc={rc}) -> NOT GRADED. "
             f"{err or 'no stderr'}")
        return {"eval_exec_failed": True, "eval_exec_rc": rc,
                "eval_exec_phase": phase, "eval_exec_error": err}


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def preflight_optimizer_endpoint(*, configured_model, label, remedy, api_key="", bearer="",
                                 base_url=_ANTHROPIC_DIRECT):
    """Prove the mechanism endpoint answers BEFORE task 0 -- but only if it will be used.

    `anthropic_optimizer_call` never raises: a dead or refusing endpoint returns ("", 0.0),
    every mechanism reads that as "no verdict" and falls back to its LLM-free path, and the
    lane runs to completion as an economized arm whose proposer and judges never fired. The
    solver is unaffected -- it authenticates itself, over its own transport -- so the cell
    grades and BILLS normally with its mechanisms dark, and nothing in the row says so
    except overhead that is exactly zero. Spend one 16-token call here and refuse instead.

    Shared by every runner whose mechanism calls use this wire -- the Claude runners, and a
    Copilot runner on an Anthropic-typed BYOK provider -- so the check cannot drift between
    them.

    ONLY for an arm that will actually make such a call: `baseline`, `baseline-low`,
    `baseline-medium` and the scaffold arms make none, so probing for them would bill a call
    they never need and refuse a lane over an endpoint they never touch. The driver sets
    TE_OPTIMIZER_PREFLIGHT from `config.needs_optimizer`. Unset means "assume it might", so
    a direct caller of make_runner keeps the check.
    """
    if os.environ.get("TE_OPTIMIZER_PREFLIGHT", "1") == "0":
        _log(f"{label}: no mechanism on this arm makes a paid call -- "
             "skipping the endpoint preflight")
        return
    probe, _eff = anthropic_optimizer_call(
        "Reply with the single word: ok", model="agent",
        configured_model=configured_model, max_tokens=16,
        api_key=api_key, bearer=bearer, base_url=base_url)
    if not (probe or "").strip():
        raise RuntimeError(
            f"the mechanism-call endpoint {base_url} did not answer a "
            "16-token probe. Every mechanism would read that as 'no verdict' and quietly "
            "fall back to its LLM-free path, so an economized arm would run as a baseline "
            f"wearing its name. {remedy}")


def anthropic_optimizer_call(prompt, *, model="agent", configured_model,
                            max_tokens=8192, api_key="", bearer="",
                            base_url=_ANTHROPIC_DIRECT):
    """One small Messages-API request. Returns (text, eff_tokens).

    Shared by every harness in this package: the between-task proposer / judge / nudge
    writer is the same call whatever the task domain is, and it must be priced on the SAME
    weight table the task rows use so mechanism overhead lands in the ledger on the task
    cost's own scale rather than as a raw token count.

    The Claude lanes call api.anthropic.com on ANTHROPIC_API_KEY. `base_url` and `bearer`
    exist for one other caller: a Copilot lane on an Anthropic-typed BYOK provider, whose
    mechanism calls go to that provider on the credential its tasks bill
    (copilot.anthropic_optimizer_endpoint).

    Never raises: a transport failure returns ("", 0.0), which the mechanisms read as "no
    verdict" and fall back to their LLM-free path. Retries the codes worth retrying.
    """
    # The driver/launchers pass an AGENT label here ("agent", "claude", "copilot"),
    # not a billable model id; only a real id overrides the configured optimizer model.
    mid = model if (model or "").startswith("claude-") else configured_model
    body = json.dumps({
        "model": mid, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    # Exactly ONE credential: the API key when there is one, else the BYOK provider's bearer
    # token. Because this function never raises, a request the endpoint refuses arrives as
    # "no verdict" and the arm quietly runs its LLM-free path -- so the header is chosen,
    # never doubled up.
    if api_key:
        headers["x-api-key"] = api_key
    elif bearer:
        headers["authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/messages",
        data=body, method="POST", headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                d = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            _log(f"optimizer_call HTTP {e.code}: {e.read()[:200]!r}")
            return "", 0.0
        except Exception as e:                           # network hiccup -> LLM-free path
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            _log(f"optimizer_call failed: {e}")
            return "", 0.0
    text = "".join(b.get("text", "") for b in d.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text")
    u = d.get("usage") or {}
    if d.get("stop_reason") == "max_tokens":
        _log(f"optimizer response hit max_tokens ({max_tokens}) and was "
             f"TRUNCATED -- the caller will fail to parse it and act on nothing. "
             f"Raise TE_OPTIMIZER_MAX_TOKENS.")
    _log_optimizer_call(mid, prompt, text, u, stop_reason=d.get("stop_reason"))
    eff = pricing.effective_tokens({
        "model": mid,
        "input_tokens": u.get("input_tokens") or 0,
        "cache_read_input_tokens": u.get("cache_read_input_tokens") or 0,
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
    })
    return text, eff


TASK_TEMPLATE = """You are working in the repository checked out at /testbed.

Resolve the following issue. Modify the SOURCE code so the described behavior is fixed.

<issue>
{problem}
</issue>

Rules:
- Work only inside /testbed; the repo is already at the right commit and its environment
  is installed (the `testbed` conda env is active).
- Do NOT modify test files: the grader resets them and applies its own tests.
- Do not commit; leaving the fix in the working tree is what gets graded.
- When you believe the fix is complete, stop.
"""


def build_prompt(task, injected):
    """Task prompt = the economizer's learned manual (if any) + the issue."""
    body = TASK_TEMPLATE.format(problem=task.get("problem_statement", ""))
    return (injected.rstrip() + "\n\n" + body) if injected else body


def _merge_usage(a, b):
    """Sum the token counters of two segments' `result` events (spiral re-invocations
    bill twice; the ledger must see the TOTAL, not just the last segment)."""
    if not a:
        return b or {}
    if not b:
        return a
    out = dict(b)
    ua, ub = (a.get("usage") or {}), (b.get("usage") or {})
    out["usage"] = {k: (ua.get(k) or 0) + (ub.get(k) or 0)
                    for k in set(ua) | set(ub)
                    if isinstance(ua.get(k, 0), (int, float))
                    and isinstance(ub.get(k, 0), (int, float))}
    for k in ("num_turns", "total_cost_usd", "duration_ms"):
        if isinstance(a.get(k), (int, float)) and isinstance(b.get(k), (int, float)):
            out[k] = a[k] + b[k]
    # Degradation is a property of the SUM: one recovered segment makes the whole task's
    # total a lower bound, so the flags OR rather than being taken from the last segment.
    # A spiral task is exactly this shape -- segment 1 completes normally, segment 2 is
    # killed by the guard -- so taking `dict(b)`'s flags alone would clear the marker on
    # the most common case there is.
    for k in ("usage_incomplete", "output_tokens_missing", "num_turns_recovered",
              "output_tokens_missing_turn"):
        out[k] = bool(a.get(k)) or bool(b.get(k))
    # Turns carrying an exact figure ADD -- unlike the flags, this is a count, and a
    # two-segment spiral task has exact turns recovered from each segment's own log.
    ex = [s.get("output_tokens_exact_turns") for s in (a, b)]
    if any(isinstance(v, int) for v in ex):
        out["output_tokens_exact_turns"] = sum(v for v in ex if isinstance(v, int))
    reasons = [r for r in (a.get("usage_incomplete_reason"),
                           b.get("usage_incomplete_reason")) if r]
    if reasons:
        out["usage_incomplete_reason"] = "+".join(reasons)
    return out


def session_output_tokens(text):
    """`(output_tokens, {message_ids})` from Claude Code's OWN session log.

    The CLI persists each assistant message together with its REAL usage as that message
    completes -- which is the only surviving copy of `output_tokens` when the terminal
    `result` event never arrives. Measured 2026-09-10 against runs that DID produce a
    result event: deduplicated, this matches it EXACTLY (421 vs 421, 534 vs 534).

    DEDUPLICATION IS NOT OPTIONAL. Every assistant message is written to the log TWICE --
    same `message.id`, different `uuid` -- so summing the entries returns exactly 2x the
    truth, which is a plausible-looking number and therefore the dangerous kind of wrong.
    Keyed on `message.id`, last write wins.

    Returns the id SET as well as the total: the stream sees the in-flight message start
    (so `_UsageRecovery` has its id) while this log does not carry it until it completes,
    so the difference between the two id sets is exactly the unaccounted turn.

    Nothing here does I/O -- the caller supplies the text, because the file lives inside a
    container on SWE-bench and on the host on Web-Bench. Stdlib only, and never raises: a
    schema change upstream must degrade to "recovered nothing", not to a failed task.
    """
    per_msg = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        msg = (ev or {}).get("message")
        if not isinstance(msg, dict):
            continue
        mid, usage = msg.get("id"), msg.get("usage")
        if not mid or not isinstance(usage, dict):
            continue
        out = usage.get("output_tokens")
        if isinstance(out, bool) or not isinstance(out, int):
            continue
        per_msg[mid] = out
    return sum(per_msg.values()), set(per_msg)


class _UsageRecovery:
    """Rebuilds a `result`-shaped usage event from the stream's own assistant messages.

    Claude Code reports its token breakdown ONLY in the stream's terminal
    `{"type":"result"}` event. Both break paths in `_segment` -- a spiral guard stop and a
    TE_TASK_TIMEOUT -- kill the CLI mid-stream, so that event never arrives. Read as `{}`,
    such a segment would make `_row_from_result` produce a row of ZEROS: every token field
    and `num_turns` 0 for a rollout that really ran for tens of turns and hundreds of
    thousands of tokens.

    Because only economized arms install a guard, that understatement would be
    ONE-DIRECTIONAL: it would make those arms look cheaper than they are and could never
    touch a baseline. Grading is unaffected (that reads the patch), but turn-based claims
    are not.

    What is recoverable, and what is not:

      * Every `assistant` event carries `message.usage` with a `message.id`. The snapshots
        are cumulative per message and repeat as that message's content blocks stream in,
        so the totals come from deduplicating on `message.id` (last snapshot wins) and
        summing. Validated against 60 healthy rows that DO have a `result` event:
        `input_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens` match
        it EXACTLY, 60/60.
      * `output_tokens` is NOT recoverable. The per-message figure is the placeholder from
        `message_start`, which understates the truth by 7.5x-134.7x (median 28x) -- far too
        variable to model. It is reported as 0 and the row is flagged, never estimated: a
        wrong number that looks like data is worse than an absent one.
      * `num_turns` is approximated by the deduplicated assistant-message count, which
        matched exactly on 107 of 120 healthy rows and was within 1 on 12 more.

    A recovered row is therefore honest but incomplete, and says so: `usage_incomplete` is
    set, with `output_tokens_missing` and `num_turns_recovered` naming which parts are
    soft. Such a row is a LOWER BOUND on cost -- aggregations must not read it as exact,
    and must never read an unflagged zero as free.
    """

    def __init__(self):
        self._by_msg = {}

    def observe(self, ev):
        """Fold one parsed stream event in. Cheap enough to call on every line."""
        if (ev or {}).get("type") != "assistant":
            return
        msg = ev.get("message") or {}
        usage, mid = msg.get("usage"), msg.get("id")
        if usage and mid:
            self._by_msg[mid] = usage          # cumulative per message: last wins

    def synthesize(self, reason, session_text=None):
        """A `result`-shaped dict for a stream that never produced one, or {} if nothing
        was seen at all (a segment that died before its first assistant message really did
        cost nothing, and must not be flagged as if it had).

        `session_text` is the CLI's own session log, when the caller could read it. With
        it, `output_tokens` stops being a hole: every turn that COMPLETED carries its real
        figure there (`session_output_tokens`), so only a turn still being generated at the
        moment of the kill is unaccounted -- and on a spiral stop there usually is none,
        because the guard fires while folding a `tool_result`, i.e. BETWEEN turns.

        The flag is cleared only on PROOF of completeness: every message id the stream saw
        also appears in the session log. Anything less stays flagged. Getting that backwards
        would produce exactly what this class exists to prevent -- an unflagged row that
        reads like a measurement and is not.
        """
        if not self._by_msg:
            return {}
        tot = {}
        for usage in self._by_msg.values():
            for k, v in usage.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tot[k] = tot.get(k, 0) + v
        recovered = {
            "input_tokens": int(tot.get("input_tokens") or 0),
            "cache_creation_input_tokens": int(
                tot.get("cache_creation_input_tokens") or 0),
            "cache_read_input_tokens": int(tot.get("cache_read_input_tokens") or 0),
            "output_tokens": 0,                # placeholder-only; see the class docstring
        }
        exact_out, exact_turns, inflight = None, 0, True
        if session_text:
            try:
                exact_out, done_ids = session_output_tokens(session_text)
                exact_turns = len(done_ids)
                if exact_turns:
                    recovered["output_tokens"] = int(exact_out)
                    # Proof of completeness: nothing the stream saw is missing from the log.
                    inflight = bool(set(self._by_msg) - done_ids)
                else:
                    exact_out = None
            except Exception as e:                      # never fail a task over accounting
                _log(f"session-log usage read failed ({type(e).__name__}: {e})")
                exact_out = None
        got_output = exact_out is not None and exact_turns > 0
        complete = got_output and not inflight
        if got_output:
            _log(f"segment ended without a result event ({reason}); output_tokens "
                 f"recovered EXACTLY from the CLI session log: {recovered['output_tokens']} "
                 f"over {exact_turns} completed turns"
                 + ("" if complete else " -- one in-flight turn unaccounted, row stays "
                                        "flagged"))
        else:
            _log(f"segment ended without a result event ({reason}); recovered usage from "
                 f"{len(self._by_msg)} assistant messages: "
                 f"in={recovered['input_tokens']} "
                 f"cw={recovered['cache_creation_input_tokens']} "
                 f"cr={recovered['cache_read_input_tokens']} "
                 f"out=UNRECOVERABLE -- row flagged usage_incomplete")
        return {
            "type": "result",
            "usage": recovered,
            "num_turns": len(self._by_msg),
            # Flagged unless the session log proves every turn is accounted for.
            "usage_incomplete": not complete,
            "usage_recovered_from": "session_log" if got_output else "transcript",
            "usage_incomplete_reason": None if complete else reason,
            "output_tokens_missing": not got_output,
            # How many turns carry an exact figure, and whether a partial turn is missing.
            # A spiral stop lands between turns (missing_turn False); a TE_TASK_TIMEOUT can
            # land mid-generation (True), and those two are not equally trustworthy.
            "output_tokens_exact_turns": exact_turns if got_output else 0,
            "output_tokens_missing_turn": bool(inflight) if got_output else True,
            "num_turns_recovered": True,
        }


def _row_from_result(result):
    """Token breakdown + turn count from Claude Code's final `result` event, in the field
    names `pricing.effective_tokens` reads."""
    u = (result or {}).get("usage") or {}
    it = u.get("input_tokens") or 0
    cr = u.get("cache_read_input_tokens") or 0
    cw = u.get("cache_creation_input_tokens") or 0
    ot = u.get("output_tokens") or 0
    return {
        "input_tokens": it, "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw, "output_tokens": ot,
        "tokens_total": it + cr + cw + ot,
        "num_turns": (result or {}).get("num_turns") or 0,
        "is_error": bool((result or {}).get("is_error")),
        "api_error_status": (result or {}).get("api_error_status"),
        "cost_usd_vendor": (result or {}).get("total_cost_usd"),
        # Degradation markers. Present and False on a healthy row, so "no flag" means
        # "not checked by this harness version" rather than "verified complete" -- and a
        # zero-token row is never silently indistinguishable from a free one.
        "usage_incomplete": bool((result or {}).get("usage_incomplete")),
        "output_tokens_missing": bool((result or {}).get("output_tokens_missing")),
        "num_turns_recovered": bool((result or {}).get("num_turns_recovered")),
        "usage_incomplete_reason": (result or {}).get("usage_incomplete_reason"),
        # Session-log recovery provenance: how many turns carry an EXACT output figure,
        # and whether a mid-generation turn is still unaccounted. Absent (0/False) on a
        # healthy row, which never needed recovery at all.
        "output_tokens_exact_turns": int(
            (result or {}).get("output_tokens_exact_turns") or 0),
        "output_tokens_missing_turn": bool(
            (result or {}).get("output_tokens_missing_turn")),
    }


class CopilotSwebenchRunner(ClaudeSwebenchRunner):
    """AgentRunner: the GitHub Copilot CLI in the official image, graded by the official
    harness.

    Everything about the task lifecycle is shared with the Claude runner -- container
    start, spiral segments, patch extraction, official gold grading -- so only four things
    are overridden: construction (GitHub auth instead of an Anthropic key), the container
    mounts + seed, the launch argv, and the accounting. See te_harness.copilot for why the
    accounting cannot be shared: Copilot's terminal `result` event carries no token fields
    at all, so the Claude path would silently record a row of zeros.

    Two behavioural differences worth knowing before comparing arms with the Claude runner:

      * **No turn cap.** Copilot has no `--max-turns`. `max_turns` is still recorded in
        run_config.json (and the spiral envelope still counts turns), but it is NOT
        enforced on the agent -- TE_TASK_TIMEOUT and the spiral guard are the only bounds.
      * **Auth is on-disk and expires.** The container authenticates from the seeded
        ~/.copilot, whose short-lived token is only refreshed when a copilot process runs
        on the HOST. On a long unattended sweep that token goes stale and every later
        container fast-fails with an empty patch, so keep a host-side keepalive running
        or set COPILOT_GITHUB_TOKEN.
    """

    # Copilot reads project instructions from .github/copilot-instructions.md, takes an
    # MCP config as `--additional-mcp-config @<path>` (built by copilot.build_argv), and
    # runs as root with HOME=/home/agent. Its native navigation tools are lower-case.
    scaffold_profile = scaffolds_pkg.AgentProfile(
        kind="copilot",
        home="/home/agent",
        instructions_file=".github/copilot-instructions.md",
        nav_tools=("view", "grep", "glob"),
        # build_argv owns the flag spelling; nothing is appended to argv here.
        mcp_args=lambda cfg: [],
        patch_excludes=(".github/copilot-instructions.md", ".github/hooks"))

    def __init__(self, agent="copilot", model=None, grading_path=None, scaffold=None):
        self.agent = agent
        # Native Copilot bills through the GitHub subscription and picks its own model
        # unless told otherwise; ANTHROPIC_MODEL is meaningless here, so COPILOT_MODEL is
        # the knob and 'auto' (the CLI's own router) is the default.
        # 'auto' normalises to None: it is a routing directive, not a billable model id,
        # and recording it as one makes pricing hard-fail at the END of a paid rollout.
        self.model = copilot.normalize_model(model or os.environ.get("COPILOT_MODEL"))
        copilot.validate_priceable(self.model, self.agent)
        # Same rule as the Claude path: the optimizer is the agent's own model, and it is
        # RECORDED as such -- optimizer_call substitutes self.model for the "agent" label,
        # so recording "agent" would describe a cost basis the run did not use. A pinned id
        # records as itself; an unpinned model is the CLI's own router, which is what the
        # task ran on too, and records as "auto".
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
        # An external tool scaffold (rtk / serena / repomap) -- a BASELINE, not a
        # mechanism. Same contract as the Claude path: resolved and preflighted here so a
        # misconfigured one kills the lane before task 0 rather than writing rows that
        # merely look like a scaffold result. `scaffold_profile` above is what makes this
        # agent-correct (copilot's own instructions file, nav tools and patch excludes).
        self.scaffold_name = scaffold or None
        self.scaffold = scaffolds_pkg.get(self.scaffold_name)
        if self.scaffold is not None:
            self.scaffold.preflight(self.scaffold_profile)
            _log(f"scaffold: {self.scaffold.name} (external baseline)")
        # need_container_seed: the cache and config dirs are bind-mounted, and docker
        # invents an empty dir for a missing source rather than failing.
        self.copilot = copilot.require_install(need_container_seed=True)
        self.agent_bin = self.copilot
        self.protected_tools = copilot.PROTECTED_TOOLS
        # Copilot's own BYOK (copilot.byok_active) bills a custom provider instead of the
        # GitHub subscription -- a different cost basis, so run_config.json names it.
        self.routing_base = copilot.provider_base()
        # TRANSPORT for the mechanisms' between-task calls. `copilot -p` is an agent
        # session and carries the CLI's system prompt on every call; these calls are
        # one-shot text completions that need none of it, and the Claude harness has always
        # made them as plain Messages requests. Use the same wire here WHEN the lane's own
        # BYOK provider speaks it and so bills what the tasks bill
        # (copilot.anthropic_optimizer_endpoint has the rule and the reason); otherwise
        # keep the CLI.
        self.optimizer_endpoint = copilot.anthropic_optimizer_endpoint()
        self.optimizer_transport = "api" if self.optimizer_endpoint else "cli"
        if self.optimizer_endpoint:
            _log(f"copilot: mechanism calls -> {self.optimizer_endpoint[0]} "
                 "(the BYOK provider, direct Messages API)")
            self._preflight_optimizer_endpoint()
        self.cli_version = copilot.cli_version(self.copilot)
        self.grader = _Grader(grading_path)
        # An output cap applies on the API transport only -- `copilot -p` has no such flag,
        # so on the CLI branch this stays a value nothing reads. Same default and same env
        # var as the Claude harness, so the two agents' mechanism calls are capped alike.
        self.optimizer_max_tokens = int(os.environ.get("TE_OPTIMIZER_MAX_TOKENS", "16384"))
        self.task_timeout = int(os.environ.get("TE_TASK_TIMEOUT", "1800"))
        self.eval_timeout = int(os.environ.get("TE_EVAL_TIMEOUT", "1800"))
        self.keep = os.environ.get("TE_KEEP_CONTAINERS") == "1"

    def _preflight_optimizer_endpoint(self):
        """Refuse the lane if its BYOK provider will not answer a mechanism call. Same check,
        and same TE_OPTIMIZER_PREFLIGHT gate, as every other runner -- see the shared helper.
        Only reached on the API transport: `copilot -p` needs no probe of its own."""
        base, key, bearer = self.optimizer_endpoint
        preflight_optimizer_endpoint(
            base_url=base, api_key=key, bearer=bearer,
            configured_model=self.optimizer_model, label="copilot",
            remedy="Check COPILOT_PROVIDER_BASE_URL and the credential that goes with it.")

    # ---- 1. the paid optimizer call ---------------------------------------------------
    def optimizer_call(self, prompt, model="agent"):
        """One between-task proposer / judge / nudge-writer call.

        Over the Messages API when this lane's BYOK provider speaks it and so bills what
        its TASKS bill (`self.optimizer_endpoint`, resolved at construction), else through
        the host `copilot` binary with every tool denied and excluded and custom
        instructions off, so either way it is a pure text completion that cannot read the
        testbed.

        Both are priced on the same weight table the task rows use, and `optimizer_model`
        is the agent's own model on both -- only the wire differs. `run_config.json` records
        which one ran, because the two are NOT comparable on overhead.
        """
        if self.optimizer_endpoint:
            base, key, bearer = self.optimizer_endpoint
            return anthropic_optimizer_call(
                prompt, model=model, configured_model=self.optimizer_model,
                max_tokens=self.optimizer_max_tokens,
                api_key=key, bearer=bearer, base_url=base)
        return copilot.optimizer_call(
            prompt, binary=self.copilot,
            model=(model if model not in ("agent", "copilot") else self.model))

    # ---- 2. container plumbing --------------------------------------------------------
    def _start(self, name, instance_id):
        """Same as the Claude path but mounting the three pieces of the Copilot install and
        seeding a writable $HOME from them. The agent runs as `agent`, not root, because
        Copilot's config/cache layout assumes a normal home."""
        _run(["docker", "rm", "-f", name])
        self._reap_exited(name)
        cmd = (["docker", "run", "-d", "--name", name, "--entrypoint", "bash"]
               + copilot.container_mounts(self.copilot)
               + ["-e", "HOME=/home/agent", "-w", "/testbed"] + _cpu_limit_args())
        _LIVE_CONTAINERS.add(name)
        p = _run(cmd + [image_for(instance_id), "-lc", "sleep infinity"],
                 timeout=int(os.environ.get("TE_DOCKER_START_TIMEOUT", "900")))
        if p.returncode != 0:
            raise RuntimeError(f"docker run failed for {instance_id}: {p.stderr[-500:]}")
        # Run as ROOT with HOME=/home/agent -- the configuration the AgentEO study used.
        # SWE-bench images have no unprivileged user (adding one and chowning /testbed is
        # both slower and a behavioural difference from the published rows), and unlike
        # Claude Code the Copilot CLI does not refuse to run as root.
        setup = ("mkdir -p /home/agent " + copilot.CONTAINER_LOG_DIR + "; "
                 + copilot.container_seed_setup())
        r = _run(["docker", "exec", name, "bash", "-lc", setup], timeout=600)
        if r.returncode != 0:
            _log(f"copilot seed setup warned: {(r.stderr or '')[-300:]}")
        self._activate_project_env(name)
        return name

    def _register_net_guard(self, name, python):
        """Copilot reads hooks from `$COPILOT_HOME/settings.json`, not `.claude/`.

        Same *shape* as the Claude path -- merge a PreToolUse entry into a settings file --
        but three details differ, and all three were measured against Copilot CLI 1.0.83
        on 2026-09-08 rather than assumed:

          * **Which file.** `$COPILOT_HOME/settings.json`, which in the container is
            `/home/agent/.copilot/settings.json` (HOME is `/home/agent`, seeded per task
            and thrown away with the container). This CLI never reads
            `.claude/settings.json`, so a guard written there would report armed and guard
            nothing. Note `hooks.json` at that level is **not** read; only
            `settings.json` is.
          * **The spelling.** `preToolUse` and matcher `bash` -- camelCase and lower-case,
            against Claude's `PreToolUse` and `Bash`, and a flat entry rather than
            Claude's nested `hooks` list. A wrong matcher never fires.
          * **No plugin.** `baselines/rtk.py` reaches the same hook through
            `copilot plugin install`, which works but is the install form §13 q7 records
            as deprecated. A settings entry needs no plugin, so the guard does not inherit
            that clock. It also means the guard and rtk do not contend: they land in
            different places, and two independent `bash` hooks were verified to both fire
            on one call.

        Merged rather than overwritten because `container_seed_setup` copies the host's
        config into this dir first, and that may already carry a `settings.json`.
        """
        entry = {"matcher": "bash", "type": "command",
                 "command": f"{python} {self.NET_GUARD_SCRIPT}"}
        settings = f"{self.scaffold_profile.home}/.copilot/settings.json"
        prog = (
            "import json, os, sys\n"
            "p = sys.argv[1]\n"
            f"entry = json.loads({json.dumps(json.dumps(entry))})\n"
            "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "try:\n"
            "    cur = json.load(open(p))\n"
            "except Exception:\n"
            "    cur = {}\n"
            "pre = cur.setdefault('hooks', {}).setdefault('preToolUse', [])\n"
            "if not any('net_guard' in json.dumps(h) for h in pre):\n"
            "    pre.append(entry)\n"
            "json.dump(cur, open(p, 'w'), indent=2)\n"
            "print('armed')\n")
        r = self._exec(
            name,
            f"cat > /tmp/te_net_guard_install.py <<'TE_EOF'\n{prog}\nTE_EOF\n"
            f"{python} /tmp/te_net_guard_install.py {settings}", timeout=60)
        if r.returncode != 0 or "armed" not in (r.stdout or ""):
            raise RuntimeError(((r.stderr or "") + (r.stdout or ""))[-200:])
        return True

    # ---- 3. one streamed agent segment ------------------------------------------------
    def _segment(self, name, prompt, transcript_fh, exclude_tools, max_turns, guard,
                 mcp_config=None):
        """Run `copilot -p` once, streaming its JSONL. Returns a usage dict (NOT a Claude
        `result` event) -- see the accounting hooks below.

        `max_turns` is accepted and ignored: Copilot has no turn-cap flag. The spiral guard
        and TE_TASK_TIMEOUT are the bounds.
        """
        # A DEDICATED log dir per segment. The spiral path re-invokes in the same container,
        # and the read-back below globs the whole dir -- so a shared dir would make segment
        # 2 re-read segment 1's usage, which _merge_segments then adds to it again. Both
        # segments' tokens would be counted twice, inflating exactly the arm the spiral
        # mechanism is supposed to show saving on.
        seg = getattr(guard, "segment", 0)
        logdir = f"{copilot.CONTAINER_LOG_DIR}/seg{seg}"
        _run(["docker", "exec", name, "mkdir", "-p", logdir])

        # `mcp_config` carries a scaffold's MCP server (Serena). build_argv owns the flag
        # spelling -- Copilot wants `--additional-mcp-config @<path>`, not Claude's
        # `--mcp-config <path>` -- which is why the profile appends nothing to argv here.
        argv = copilot.build_argv(prompt, log_dir=logdir, model=self.model,
                                  effort=self.effort, exclude_tools=exclude_tools,
                                  mcp_config=mcp_config)
        cmd = (["docker", "exec", "-i", "-w", "/testbed", "-e", "HOME=/home/agent"]
               + self._env_activation_args(name))    # project env, see _activate_project_env
        # BYOK credentials are forwarded BY NAME only (docker exec -e NAME reads the
        # client's environment), so the secret never reaches argv on a shared host.
        for k in copilot.provider_secret_names():
            cmd += ["-e", k]
        for k, v in copilot.run_env().items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [name] + argv

        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        stream_lines, stopped = [], False
        deadline = time.time() + self.task_timeout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            transcript_fh.write(line + "\n")
            transcript_fh.flush()
            stream_lines.append(line)
            # The vendored guard's completed_calls() understands BOTH the Copilot schema
            # (tool.execution_start/complete) and Claude's, so no translation is needed.
            if guard is not None and guard.monitor(line):
                stopped = True
                break
            if time.time() > deadline:
                _log("segment hit TE_TASK_TIMEOUT; stopping the agent")
                stopped = True
                break

        if stopped:
            # Kill INSIDE the container: killing the `docker exec` client leaves the CLI
            # running and still billing.
            _run(["docker", "exec", name, "pkill", "-TERM", "-f", "copilot"])
            time.sleep(2)
            _run(["docker", "exec", name, "pkill", "-KILL", "-f", "copilot"])
            proc.kill()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = (proc.stderr.read() or "")[-600:]

        # A configuration the CLI refuses outright fails IDENTICALLY on every task, so
        # stop the sweep here rather than filling the results tree with 0-turn rows that
        # look like the agent tried and failed.
        bad = copilot.fatal_config_error(err)
        if bad:
            raise RuntimeError(
                f"copilot refused this configuration: {bad}\n"
                f"  model={self.model!r} effort={self.effort!r}\n"
                "  fix COPILOT_MODEL / TE_EFFORT_LEVEL; "
                "COPILOT_MODEL=auto always works but does not apply an effort level.")

        log_text = _run(["docker", "exec", name, "bash", "-lc",
                         f"cat {logdir}/*.log 2>/dev/null"], timeout=180).stdout or ""
        usage = copilot.row_usage("\n".join(stream_lines), log_text)
        if err.strip() and not usage.get("num_turns"):
            _log(f"agent stderr: {err.strip()[:300]}")
        # Stash the debug log for the untooling catalog parser: trace.parse_catalog(run_dir,
        # 'copilot') reads exactly this filename. Without it the tool judge is handed
        # empty descriptions, drops nothing, and still bills for the call.
        usage["_debug_log"] = log_text
        return usage

    # ---- 3b. accounting hooks ----------------------------------------------------------
    def _merge_segments(self, a, b):
        merged = copilot.merge_usage(a, b)
        merged["_debug_log"] = (a or {}).get("_debug_log", "") + (b or {}).get("_debug_log", "")
        return merged

    def _row_from_segment(self, result):
        row = dict(result or {})
        row.setdefault("is_error", False)
        # Copilot exposes no per-run USD figure (only a premium-request count), so the
        # vendor cost stays null rather than a fabricated zero.
        row.setdefault("cost_usd_vendor", None)
        # Same degradation predicate as the Claude path, so one filter covers both agents.
        # Copilot needs no _UsageRecovery -- it reads the whole log dir back AFTER the
        # kill (`cat {logdir}/*.log`), so a guard or timeout stop costs it no usage. Its
        # own failure mode is a log dir it could not parse, which
        # `row_usage` already reports as tokens_source="stream_output_only" with None (not
        # 0) in the unaccounted fields.
        src = row.get("tokens_source")
        row["usage_incomplete"] = src is not None and src != "debug_log"
        row["output_tokens_missing"] = row.get("output_tokens") is None
        row["num_turns_recovered"] = False        # turn_start events survive the kill
        if row["usage_incomplete"]:
            row.setdefault("usage_incomplete_reason", f"tokens_source={src}")
        return row      # _debug_log rides along; run_task persists and strips it

    # ---- 4. entry point: same lifecycle + the debug log the catalog parser needs --------
    def run_task(self, task, *, arm, injected, exclude_tools, run_dir_path, **run_kwargs):
        row, out_dir = super().run_task(
            task, arm=arm, injected=injected, exclude_tools=exclude_tools,
            run_dir_path=run_dir_path, **run_kwargs)
        # trace.parse_catalog(run_dir, 'copilot') reads exactly this filename. Without it
        # the untooling judge is handed tool names with EMPTY descriptions, drops nothing,
        # and still bills for the call -- the mechanism silently measures itself as useless.
        log_text = row.pop("_debug_log", "")
        if log_text:
            try:
                with open(os.path.join(out_dir, "copilot_debug.log"), "w",
                          encoding="utf-8") as fh:
                    fh.write(log_text)
            except OSError as e:
                _log(f"could not save copilot_debug.log: {e}")
        else:
            _log("no copilot debug log recovered -- untooling catalog will be empty "
                 "and token accounting falls back to stream output only")
        return row, out_dir


# Every scaffold is hostable on the Codex lane, each through the channel Codex actually has
# -- none installs successfully and then does nothing, which would read as a scaffold result:
#   rtk     -- its own Codex mode (`rtk init -g --codex`, instruction files under CODEX_HOME;
#              the hook mode is one this CLI ignores), measured by `rtk_compliance`;
#   repomap -- project instructions, which the profile declares as AGENTS.md;
#   serena  -- an MCP server, which Codex takes from `[mcp_servers.*]` in config.toml rather
#              than an argv flag: the profile's `mcp_args` is empty on purpose and
#              `_apply_scaffold` rewrites the config with the scaffold's spec. Measured by
#              `serena_tool_calls`, because a configured server is not a used one.


class CodexSwebenchRunner(ClaudeSwebenchRunner):
    """AgentRunner: the OpenAI Codex CLI (`codex exec --json`) in the instance's pinned
    image, graded by the official SWE-bench harness. It bills the OpenAI API directly on
    OPENAI_API_KEY (codex.resolve_codex_routing).

    Four things differ from the Claude runner; everything else -- container lifecycle,
    spiral segments, patch extraction, official gold grading -- is inherited.

      1. **The endpoint is a FILE.** Codex reads its provider and base URL from
         `$CODEX_HOME/config.toml`, not from the environment. The file is rewritten per
         task because it also carries this task's frozen capability mask.
      2. **The tool mask is a capability mask.** Codex has no `--disallowed-tools`. The
         Codex-native untooling mechanism emits logical capability packages and
         `codex.config_toggles` maps them onto verified config toggles.
      3. **Its own usage schema.** `input_tokens` in Codex's usage block is the GRAND
         input total including both cache fields; see `codex.row_usage`.
      4. **No turn cap.** Codex has no `--max-turns`, the same caveat the Copilot lane
         carries: `max_turns` is recorded and counted by the envelope but not enforced.
         TE_TASK_TIMEOUT and the spiral guard are the only bounds.
    """

    # Codex reads project instructions from AGENTS.md and navigates the repo THROUGH the
    # shell (cat/grep/sed) rather than with separate nav tools, so every read/search byte
    # lands in the shell bucket and `nav_tools` is empty. It takes no MCP config on this
    # path, so nothing is appended to argv.
    scaffold_profile = scaffolds_pkg.AgentProfile(
        kind="codex",
        home="/tmp/agent-home",
        instructions_file="AGENTS.md",
        nav_tools=(),
        # Empty on purpose, not because Codex lacks MCP: it configures servers in
        # config.toml (`[mcp_servers.<name>]`), and _apply_scaffold below writes them there.
        mcp_args=lambda cfg: [],
        patch_excludes=("AGENTS.md", "RTK.md"),
        # Where the CLI reads its global instructions from. A scaffold's install must run
        # under this, not under $HOME -- see AgentProfile.env.
        env={"CODEX_HOME": codex.C_HOME})

    def __init__(self, agent="codex", model=None, grading_path=None, scaffold=None):
        self.agent = agent
        # The OpenAI API directly -- OPENAI_BASE_URL only names OpenAI's own regional
        # endpoint, for a region-scoped key. Resolved from the environment alone: see
        # codex.resolve_codex_routing.
        self.routing = codex.resolve_codex_routing()
        self.routing_base = self.routing["base_url"]
        self.api_key = self.routing["api_key"]
        # The BILLED model. `gpt-5.6-sol` was probed on 2026-09-09 and is both listed and
        # ACCEPTED on /v1/responses by a direct OpenAI key. The `-codex` ALIASES are the
        # trap -- /v1/models lists them and /v1/responses 404s them -- which is why the
        # check below is a catalog lookup AND why a bad id still has to survive to a real
        # call to be caught. Rather than guess, verify_model refuses the lane with the
        # served list in the message.
        self.model = model or os.environ.get("CODEX_MODEL") or "gpt-5.6-sol"
        codex.validate_priceable(self.model, self.agent)
        _log(f"codex: model={self.model} endpoint={self.routing_base}")
        # Same rule as both other agents: the between-task optimizer runs on the AGENT'S
        # OWN model so mechanism overhead is priced on the same basis as the task rows.
        if os.environ.get("TE_OPTIMIZER_MODEL"):
            raise RuntimeError(
                "TE_OPTIMIZER_MODEL is no longer honoured: the between-task optimizer "
                "runs on the agent's own model so mechanism overhead is priced on the "
                "same basis as the task rows. Unset it.")
        self.optimizer_model = self.model
        self.optimizer_transport = "api"
        self.optimizer_max_tokens = int(os.environ.get("TE_OPTIMIZER_MAX_TOKENS", "16384"))
        # Reasoning effort is part of the cost basis, so it is set explicitly rather than
        # left to whatever the installed CLI defaults to.
        self.effort = (os.environ.get("TE_EFFORT_LEVEL")
                       or os.environ.get("CODEX_EFFORT_LEVEL") or "high")
        if self.effort not in codex.EFFORT_LEVELS:
            raise RuntimeError(f"TE_EFFORT_LEVEL={self.effort!r} is not one of "
                               f"{sorted(codex.EFFORT_LEVELS)}")
        self.scaffold_name = scaffold or None
        self.scaffold = scaffolds_pkg.get(self.scaffold_name)
        if self.scaffold is not None:
            self.scaffold.preflight(self.scaffold_profile)
            _log(f"scaffold: {self.scaffold.name} (external baseline)")
        # Resolve the install BEFORE anything else touches Docker: a bind mount of a
        # missing vendor dir becomes an EMPTY dir in the container rather than an error.
        self.codex_shim, self.codex_vendor = codex.require_install()
        self.agent_bin = codex.C_BIN
        self.protected_tools = codex.PROTECTED_TOOLS
        self.cli_version = codex.cli_version(self.codex_shim)
        # PROVE THE CONTRACT PARSES, against this CLI build, before task 0. The accepted
        # spelling of the pruning toggles moved between codex-cli 0.146 and 0.153, and an
        # unrecognized field is IGNORED rather than rejected unless --strict-config is
        # passed -- so a CLI upgrade would silently turn the untooling arm into a baseline
        # wearing its name. Validate the widest config this lane can emit (every prunable
        # capability at once), so the check covers masks later tasks may freeze.
        # The `skills` package is pruned by disabling each bundled system skill by path, so
        # the set is read from THIS CLI build rather than assumed (a CLI upgrade that ships
        # new bundled skills would otherwise leave them listed, a silent loss of saving).
        self.system_skills = tuple(codex.bundled_system_skills(binary=self.codex_shim))
        if not self.system_skills:
            self.system_skills = codex.DEFAULT_SYSTEM_SKILLS
            _log("codex: could not discover the CLI's bundled system skills -- using the "
                 f"0.153.1 set {list(self.system_skills)}")
        else:
            _log(f"codex: bundled system skills on this CLI: {list(self.system_skills)}")
        # The instruction package needs the base prompt captured for THIS CLI build; with
        # no capture the package stays loaded and the lane says so, rather than applying a
        # prompt recorded from another version.
        self.base_prompt = codex.base_prompt_for(self.cli_version)
        if self.base_prompt:
            _log(f"codex: base prompt capture found for {self.cli_version!r} "
                 f"({len(self.base_prompt) // 4} tokens); the interactive_guidance and "
                 "skills how-to sections can be pruned through model_instructions_file")
        else:
            _log(f"codex: no base prompt capture for {self.cli_version!r} -- the "
                 "interactive_guidance package cannot be applied on this lane and will "
                 "stay loaded; capture this CLI's base prompt into "
                 "te_harness/data/codex_base_prompt/<version>.md to enable it")
        probe_cfg = codex.config_toml(
            routing=self.routing, model=self.model, effort=self.effort,
            capabilities=sorted(codex.CONFIG_CAPABILITIES), system_skills=self.system_skills,
            instructions=codex.lean_instructions(self.base_prompt,
                                                 sorted(codex.CONFIG_CAPABILITIES)))
        bad = codex.validate_config(probe_cfg, binary=self.codex_shim)
        if bad:
            raise RuntimeError(
                "codex refused the generated config.toml, so the request contract this "
                "lane would send is not the one it describes:\n" + bad +
                "\n  A renamed toggle is IGNORED without --strict-config, which is why "
                "this is checked here: the untooling arm would otherwise run as a "
                "baseline under its own name. Update codex._TOP_LEVEL_TOGGLES / "
                "_TABLE_TOGGLES for this CLI build.")
        # Refuse a lane whose endpoint does not serve the id it intends to bill.
        served, why = codex.verify_model(self.model, self.routing)
        if served is False:
            raise RuntimeError("codex model check failed: " + why)
        if served is None:
            _log(f"codex: {why} -- an unserved id would fail per task, so check "
                 f"`curl {self.routing_base}/models` by hand before a long run")
        self.grader = _Grader(grading_path)
        self.task_timeout = int(os.environ.get("TE_TASK_TIMEOUT", "1800"))
        self.eval_timeout = int(os.environ.get("TE_EVAL_TIMEOUT", "1800"))
        self.keep = os.environ.get("TE_KEEP_CONTAINERS") == "1"
        # Per-task state the segment builder needs but run_task does not thread: the
        # frozen capability mask, and the resume thread id.
        self._capabilities = ()
        self._thread_id = ""
        codex.preflight_optimizer(routing=self.routing, configured_model=self.model)

    # ---- 1. the paid optimizer call ---------------------------------------------------
    def optimizer_call(self, prompt, model="agent"):
        """One between-task proposer / judge / nudge-writer call, over /v1/responses.

        Against this lane's own endpoint and on this lane's own model, so mechanism spend
        bills what its tasks bill. See codex.optimizer_call.
        """
        return codex.optimizer_call(
            prompt, configured_model=(self.model if model in ("agent", "codex") else model),
            routing=self.routing, max_tokens=self.optimizer_max_tokens)

    # ---- 2. container plumbing --------------------------------------------------------
    def _start(self, name, instance_id):
        """Same as the Claude path but mounting the Codex vendor dir instead of a single
        binary. The ELF is static-PIE and finds its siblings (`rg`, the sandbox helpers)
        relative to itself, so one read-only mount is the whole install."""
        _run(["docker", "rm", "-f", name])
        self._reap_exited(name)
        cmd = (["docker", "run", "-d", "--name", name, "--entrypoint", "bash"]
               + codex.container_mounts(self.codex_vendor)
               + ["-e", "HOME=/tmp/agent-home", "-w", "/testbed"] + _cpu_limit_args())
        if self.scaffold is not None:
            for spec in self.scaffold.mounts(self.scaffold_profile):
                cmd += ["-v", spec]
        _LIVE_CONTAINERS.add(name)
        p = _run(cmd + [image_for(instance_id), "-lc", "sleep infinity"],
                 timeout=int(os.environ.get("TE_DOCKER_START_TIMEOUT", "900")))
        if p.returncode != 0:
            raise RuntimeError(f"docker run failed for {instance_id}: {p.stderr[-500:]}")
        # The agent home too: the net guard's script is `docker cp`'d into it, and docker cp
        # into a directory that does not exist fails.
        _run(["docker", "exec", name, "mkdir", "-p", codex.C_HOME, self.scaffold_profile.home])
        # The contract is written HERE, not as a side effect of arming the net guard.
        # `_install_net_guard` returns early when TE_ALLOW_NETWORK_TOOLS=1 lifts the guard,
        # so a write hanging off `_register_net_guard` would leave that lane with NO
        # config.toml at all -- no provider block, so no task could reach any endpoint. The
        # guard's hook goes in hooks.json beside this file, never into it.
        self._write_config(name)
        self._activate_project_env(name)
        return name

    def _write_config(self, name):
        """Install this task's `config.toml` -- the request contract -- in the container.

        Written per task, not per container, because it carries the capability mask the
        mechanism froze for THIS task. Base64'd through a single exec so no quoting in the
        TOML can be reinterpreted by the shell.
        """
        lean = codex.lean_instructions(getattr(self, "base_prompt", None),
                                       self._capabilities)
        self._lean_instructions_tokens = len(lean) // 4 if lean else 0
        cfg = codex.config_toml(
            routing=self.routing, model=self.model, effort=self.effort,
            capabilities=self._capabilities,
            system_skills=getattr(self, "system_skills", None), instructions=lean,
            mcp_servers=getattr(self, "_mcp_servers", None))
        # config.toml, plus the lean base prompt when the frozen mask prunes an
        # instruction package (it is what `model_instructions_file` in the config names).
        # Hooks are NOT declared here: the net guard writes them to hooks.json beside this
        # file (`_register_net_guard`). One config layer carrying both representations makes
        # Codex warn and prefer one, so the contract keeps exactly one.
        blob = base64.b64encode(cfg.encode()).decode()
        script = (f"mkdir -p {codex.C_HOME} && echo {blob} | base64 -d > "
                  f"{codex.C_HOME}/config.toml")
        if lean:
            lblob = base64.b64encode(lean.encode()).decode()
            script += f" && echo {lblob} | base64 -d > {codex.INSTRUCTIONS_PATH}"
        r = self._exec(name, script + " && echo wrote", timeout=60)
        if "wrote" not in (r.stdout or ""):
            raise RuntimeError(
                "could not install the codex config.toml: "
                + ((r.stderr or "") + (r.stdout or ""))[-200:])
        return cfg

    # ---- 2b. the network guard --------------------------------------------------------
    # The interpreter the PreToolUse hook runs under once `_register_net_guard` has armed it,
    # "" when it is not armed. `_segment` reads it: an armed task passes
    # `--dangerously-bypass-hook-trust`, because Codex skips every hook a person has not
    # approved in /hooks and an unattended container has no way to approve one.
    _guard_python = ""

    def _register_net_guard(self, name, python):
        """Codex: the guard's PreToolUse entry in `$CODEX_HOME/hooks.json`.

        Codex DOES honour it. Verified live on codex-cli 0.153.1 (2026-09-30, gpt-5.6-sol,
        direct API) with exactly this file and `--dangerously-bypass-hook-trust`: the guard
        refused `echo https://github.com/probe` before it ran -- "Command blocked by
        PreToolUse hook: Blocked: this benchmark's grading specs are public on GitHub ..." --
        and logged the refusal, while a control command in the same turn ran normally.

        Three details are load-bearing, and without any one of them the guard never runs:
        the event is keyed PascalCase `PreToolUse`, as Claude Code reads it (not kebab-case
        `pre-tool-use`); the handler is a command string, not an argv list; and the run
        passes the trust bypass.

        The rest is the shared lifecycle's: the script is copied in, the interpreter found,
        and the guard made to refuse its probe before the lane reports itself armed.
        """
        self._write_hook_entry(name, python, f"{codex.C_HOME}/hooks.json")
        self._guard_python = python
        return True

    # ---- 2c. scaffolds that are MCP servers ---------------------------------------------
    def _apply_scaffold(self, name, task, run_dir_path):
        """Same lifecycle, then wire any MCP server the scaffold asked for into config.toml.

        Ordering is the whole point: `_start` writes config.toml before the scaffold exists,
        and Codex reads MCP servers from that file rather than from argv, so a scaffold's
        `mcp_servers` spec has to be written AFTER setup by rewriting the config. The JSON the
        scaffold also wrote (`mcp_config`) is provenance on this lane; the argv seam that
        consumes it on Claude is a no-op here.
        """
        self._mcp_servers = None
        notes, mcp_config = super()._apply_scaffold(name, task, run_dir_path)
        spec = notes.pop("mcp_servers", None) if isinstance(notes, dict) else None
        if spec:
            self._mcp_servers = spec
            self._write_config(name)
            notes["mcp_wiring"] = "config.toml"
            notes["mcp_servers_configured"] = sorted(spec)
        return notes, mcp_config

    # ---- 3. one rollout segment -------------------------------------------------------
    def _segment(self, name, prompt, transcript_fh, exclude_tools, max_turns, guard,
                 mcp_config=None):
        """Run `codex exec --json` once (or `codex exec resume` for a spiral segment).

        `prompt` is a string for a fresh segment, or the resume REQUEST dict the Codex
        guard returned -- `{mode, session_id, prompt, effort}`. Resuming the exact thread
        rather than starting a new session is the mechanism: a fresh session would re-pay
        for the context the stalled one already built.

        `max_turns` is accepted and ignored (Codex has no turn-cap flag). Returns a usage
        dict, NOT a Claude `result` event -- see the accounting hooks.
        """
        resume = prompt if isinstance(prompt, dict) else None
        text = resume["prompt"] if resume else prompt
        hooked = bool(self._guard_python)
        if resume:
            argv = codex.build_resume_argv(
                resume["session_id"], effort=resume.get("effort"), hook=hooked)
        else:
            argv = codex.build_argv(hook=hooked)
        cmd = ["docker", "exec", "-i", "-w", "/testbed"]
        cmd += self._env_activation_args(name)      # project env, see _activate_project_env
        # The key is forwarded BY NAME only (`docker exec -e VAR` reads the client's
        # environment), so it never reaches argv -- readable by every other user via `ps`
        # on a shared host.
        cmd += ["-e", codex.KEY_ENV]
        env = codex.run_env({net_guard.LOG_ENV: self.NET_GUARD_LOG})
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [name] + argv

        cenv = dict(os.environ)
        cenv[codex.KEY_ENV] = self.api_key
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1, env=cenv)
        proc.stdin.write(text)
        proc.stdin.close()

        lines, stopped = [], False
        deadline = time.time() + self.task_timeout
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
                _log("segment hit TE_TASK_TIMEOUT; stopping the agent")
                stopped = True
                break

        if stopped:
            # Kill INSIDE the container: terminating the `docker exec` client leaves the
            # CLI running and still billing. Match the process name EXACTLY -- a
            # `pkill -f codex` also matches the harness's own helpers.
            _run(["docker", "exec", name, "pkill", "-TERM", "-x", "codex"])
            time.sleep(2)
            _run(["docker", "exec", name, "pkill", "-KILL", "-x", "codex"])
            proc.kill()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = (proc.stderr.read() or "")[-600:]

        # A configuration the CLI refuses fails IDENTICALLY on every task, so stop the
        # sweep here rather than filling the tree with 0-turn rows that look like the agent
        # tried. `validate_config` already covers the field NAMES at construction; this
        # catches what only a real run can (an unknown model, a dead provider block).
        if "Error loading config.toml" in err or "unknown configuration field" in err:
            raise RuntimeError(
                "codex refused its config at run time:\n" + err.strip()[:600])

        usage = codex.row_usage("\n".join(lines))
        if err.strip() and not usage.get("num_turns"):
            _log(f"agent stderr: {err.strip()[:300]}")
        return usage

    # ---- 3a. spiral hooks -------------------------------------------------------------
    def _build_guard(self, run_kwargs):
        """The Codex-native post-green finalize controller, when the arm carries one.

        `codex_spiral_policy` arrives already frozen at the repository's learned
        resolved-turn threshold (`mechanisms.spiral.SpiralStage.run_kwargs`); a lane with
        no threshold yet passes None and runs unguarded, which is the cold-start behaviour
        the mechanism intends.
        """
        policy = run_kwargs.get("codex_spiral_policy")
        if policy is None:
            return None
        return codex_spiral.CodexResumeGuard(
            policy=policy, verbose=os.environ.get("TE_VERBOSE", "1") == "1")

    def _guard_continuation(self, guard, prompt):
        """The Codex guard resumes a thread, so it takes no prompt and returns a request."""
        return guard.continuation()

    def _guard_row(self, guard):
        """Telemetry under the `codex_spiral_*` keys `SpiralStage.after_task` reads.

        `aborted` is always False and that is the mechanism's defining property rather
        than an omission: the post-green finalize NEVER terminally aborts a task, it only
        redirects one that has already passed its own success check.
        """
        tel = guard.telemetry()
        return {
            "aborted": False,
            # The guard's OWN count of command+edit events, which is the unit its trigger
            # compares against the envelope. The row's `num_turns` is agent STEPS (messages
            # + tool calls) and runs larger, so feeding that to the envelope tracker raises
            # the gate and the controller barely fires. `SpiralStage.after_task` reads this
            # field.
            "codex_spiral_tool_turns": (tel.get("snapshot") or {}).get("tool_turns"),
            "codex_spiral_nudges": tel["nudges"],
            "codex_spiral_events": tel["events"],
            "codex_spiral_thread_captured": tel["thread_id_captured"],
            "codex_spiral_paid_judge_tokens": tel["paid_judge_effective_tokens"],
            "codex_spiral_policy": tel["policy"],
        }

    # ---- 3b. accounting hooks ---------------------------------------------------------
    def _merge_segments(self, a, b):
        return codex.merge_usage(a, b)

    def _row_from_segment(self, result):
        row = dict(result or {})
        row.setdefault("is_error", False)
        row.setdefault("cost_usd_vendor", None)
        # Same degradation predicate as the other two agents, so one filter covers all
        # three. Codex's own failure mode is a stream killed before `turn.completed`, which
        # takes the usage block with it -- row_usage reports that as tokens_source
        # no_turn_event with None (not 0) in every token field, so such a task is
        # filterable instead of being averaged in as free.
        src = row.get("tokens_source")
        row["usage_incomplete"] = src is not None and src != "stream_usage"
        row["output_tokens_missing"] = row.get("output_tokens") is None
        row["num_turns_recovered"] = False
        if row["usage_incomplete"]:
            row.setdefault("usage_incomplete_reason", f"tokens_source={src}")
        return row

    # ---- 4. entry point ---------------------------------------------------------------
    def run_task(self, task, *, arm, injected, exclude_tools, run_dir_path, **run_kwargs):
        """Same lifecycle, with the capability mask frozen before the container starts.

        The mask reaches the runner two ways and both are needed. `exclude_tools` carries
        the RELAY capability ids (the mechanism's `build_tools` return), and
        `codex_config_capabilities` carries the logical packages that have a supported
        native config toggle. The second is not derivable from the first: the
        `collaboration` package expands to NO relay id, so a run that read only
        `exclude_tools` would prune it from nothing while reporting it pruned.
        """
        self._capabilities = tuple(run_kwargs.get("codex_config_capabilities") or ())
        # Per task: `_start` writes config.toml before this task's scaffold has run, and a
        # previous task's MCP spec must not ride along in that first write.
        self._mcp_servers = None
        self._guard_python = ""
        row, out_dir = super().run_task(
            task, arm=arm, injected=injected, exclude_tools=exclude_tools,
            run_dir_path=run_dir_path, **run_kwargs)
        row["codex_config_capabilities"] = list(self._capabilities)
        row["codex_lean_instructions_tokens"] = int(
            getattr(self, "_lean_instructions_tokens", 0) or 0)
        if self.scaffold_name == "rtk":
            # Instruction-based on this agent, so the measure of the arm is whether the
            # agent complied. Read off the trace, like answer_key_attempts.
            row.update(codex.rtk_compliance(os.path.join(out_dir, "transcript.jsonl")))
        if self.scaffold_name == "serena":
            # A configured MCP server is not a used one: count the calls.
            row.update(codex.mcp_tool_calls(os.path.join(out_dir, "transcript.jsonl"),
                                            server="serena"))
            if row.get("mcp_item_sample") and not row.get("mcp_tool_calls"):
                _log(f"{task['instance_id']}: MCP item with an unrecognised shape -- "
                     f"pin it: {row['mcp_item_sample'][:200]}")
        return row, out_dir


# Which runner each --agent label selects. A label with no entry is an error rather than a
# silent fallback: falling back to the Claude runner would run Claude Code under
# `--agent copilot` and file the rows under a copilot/ tree -- wrong numbers rather than
# a crash, which is the worst failure mode a benchmark can have.
RUNNERS = {
    "claude": ClaudeSwebenchRunner,
    "copilot": CopilotSwebenchRunner,
    "codex": CodexSwebenchRunner,
}


def make_runner(agent=None, scaffold=None, **kwargs):
    """Factory the driver's ``--runner te_harness.swebench:make_runner`` calls.

    Called with the driver's ``--agent`` label when the driver supports it, and with no
    arguments by older callers (which then get the TE_AGENT default, or claude).

    ``scaffold`` names an external tool baseline (rtk / serena / repomap) to install into
    every task container; the driver derives it from the arm, so the arm registry stays the
    single source of truth for which arm carries which scaffold. ``TE_SCAFFOLD`` is the
    fallback for a caller that cannot pass it.
    """
    agent = (agent or os.environ.get("TE_AGENT") or "claude").lower()
    if agent not in RUNNERS:
        raise RuntimeError(
            f"no SWE-bench runner for --agent {agent!r}; "
            f"available: {', '.join(sorted(RUNNERS))}")
    _install_signal_cleanup()
    scaffold = scaffold or os.environ.get("TE_SCAFFOLD") or None
    return RUNNERS[agent](agent=agent, scaffold=scaffold, **kwargs)
