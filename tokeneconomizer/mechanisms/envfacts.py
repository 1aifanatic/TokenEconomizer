"""Environment facts: the LLM-free injection layer shared by every lane.

WHAT IT DOES. As a repository's task stream runs, this controller reads each finished
rollout through the one trace seam and looks for RECURRING SHAPES OF EVIDENCE that an
agent re-discovers the same environment fact by failing at it, task after task. When a
shape recurs across drifted tasks it becomes a one-line, declarative fact that is injected
before the next task through the instruction channel (the prompt prefix on every lane). It
runs alone (profile `envfacts`, the Codex lane's default) or underneath the paid proposer
(profile `envfacts+distiller`), where its claims are handed to the proposer as
already-stated so the paid budget is not spent rediscovering them.

THE DESIGN RULE. The detectors describe shapes, never a harness: nothing here names a
path, an environment manager, a repository or a benchmark. Every specific string in a
rendered fact -- an interpreter path, a runner form, a refused host -- is a PARAMETER learned
from the traces. The same code must form the interpreter fact on a harness whose shell is
not activated, form nothing on one that is, and form the "history ends at HEAD" fact only
where the probes really return nothing. The WORDS that name intents (which executables are
interpreters, runners, package tools, fetchers, probes) are a `Vocabulary` the domain
adapter supplies, so the same families read a Python/git repository and a Node/npm one.

THE FAMILIES.

  FormRepair   in one task, invocation form A of an intent (run the interpreter, run the
               tests, use the package tool) fails with an infrastructure error, and a later
               form B of the same intent succeeds. Fact: "`A ...` fails here with <error>;
               `B ...` works." Retires when a bare form runs the test runner successfully.
  Boundary     every call of an operation class (fetching from a host) fails with one
               environmental error class and none ever succeeds. Fact: "Connections to
               <hosts> are refused here." Retires when a fetch to a listed host succeeds.
  EmptyProbe   discovery probes of one kind (VCS refs beyond the checkout) return nothing in
               every task where they run. Fact: "no remote, no refs beyond HEAD". Retires --
               hard -- the moment a probe returns something, so it never forms on an image
               whose history continues past the base commit.

LIFECYCLE. The validated `_Fact` state machine from `mechanisms/injection.py`, unchanged:
CANDIDATE -> ACTIVE after K_PROMOTE confirmations on drifted tasks, recency-weighted score
with asymmetric contradiction, and a FIRING GATE so a fact is injected only while the waste
it prevents keeps occurring (or, once injected, keeps being prevented).

COST HONESTY. No LLM is involved anywhere: `opt_tokens` is exactly 0. The rendered block is
prompt bytes, priced by the ledger like the rest of the prompt (written once at cache-write,
re-read every step); it is capped at MAX_TOKENS and MAX_FACTS, and a lint refuses any fact
that reads as completion or verification advice -- the failure mode of the earlier Codex
injection variants, which lost resolves with "you are done once the tests pass" hints.

Stdlib only; agent-agnostic through `trace.load_trajectory` (Codex exit codes, Claude and
Copilot error flags all reduce to the same (form, outcome) observations).
"""
import json
import os
import re

from ..trace import strip_rtk_prefix
from collections import Counter
from dataclasses import dataclass

from .injection import _Fact, edited_pyfiles
from ..trace import load_trajectory

STATE_SUBDIR = "envfacts_state"
MAX_FACTS = 4
MAX_TOKENS = 150          # ~4 chars per token; the whole block, header included
HEADER = "# environment facts learned from earlier tasks in this checkout"

# --------------------------------------------------------------------------------------
# vocabulary -- the domain-supplied words that name intents (never harness constants)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Vocabulary:
    """What the families recognise as an interpreter, a test runner, a package tool, a
    fetcher, an environment probe. A domain adapter supplies one (`env_vocabulary()`); the
    families themselves are the same for every vocabulary."""
    name: str
    interpreters: frozenset       # executables that run the language
    canonical_interpreter: str    # how a shebang-run script is spelled as a form
    runner_heads: frozenset       # bare test-runner executables
    runner_scripts: tuple         # scripts that ARE the runner when an interpreter runs them
    runner_modules: frozenset     # `<interpreter> -m <module>` runners
    package_tools: frozenset
    fetch_heads: frozenset = frozenset({"curl", "wget"})
    env_probe_heads: frozenset = frozenset({"which", "command", "type", "whereis"})


PYTHON_GIT = Vocabulary(
    name="python-git",
    interpreters=frozenset({"python", "python2", "python3", "pypy", "pypy3"}),
    canonical_interpreter="python",
    runner_heads=frozenset({"pytest", "py.test", "tox", "nox", "nosetests", "trial"}),
    runner_scripts=("runtests.py", "bin/test", "bin/doctest", "manage.py"),
    runner_modules=frozenset({"pytest", "unittest", "nose", "django", "tox", "nox"}),
    package_tools=frozenset({"pip", "pip3", "conda", "mamba", "poetry", "pipenv", "uv",
                             "apt", "apt-get"}),
)

NODE_WEB = Vocabulary(
    name="node-web",
    interpreters=frozenset({"node", "nodejs", "deno", "bun", "tsx", "ts-node"}),
    canonical_interpreter="node",
    runner_heads=frozenset({"jest", "vitest", "mocha", "playwright", "cypress", "ava",
                            "karma"}),
    runner_scripts=(),
    runner_modules=frozenset(),
    package_tools=frozenset({"npm", "yarn", "pnpm", "corepack", "apt", "apt-get"}),
)

DEFAULT_VOCAB = PYTHON_GIT
# Module-level aliases of the default vocabulary, kept for the offline analyses.
INTERPRETERS = PYTHON_GIT.interpreters
RUNNER_HEADS = PYTHON_GIT.runner_heads
RUNNER_SCRIPTS = PYTHON_GIT.runner_scripts
RUNNER_MODULES = PYTHON_GIT.runner_modules
PACKAGE_TOOLS = PYTHON_GIT.package_tools
FETCH_HEADS = PYTHON_GIT.fetch_heads
GIT_FETCH_SUBS = frozenset({"fetch", "clone", "pull", "ls-remote"})
GIT_PROBE_SUBS = frozenset({"remote", "branch", "tag", "fsck", "stash", "show-ref",
                            "for-each-ref"})
ENV_PROBE_HEADS = PYTHON_GIT.env_probe_heads
PREFIX_HEADS = ("sudo", "env", "time", "exec", "nice", "nohup", "timeout")
_PASSTHROUGH = frozenset({"cd", "echo", "head", "tail", "cat", "sort", "uniq", "tee", "true",
                          "printf", "less", "more"})

_ACTIVATE_RE = re.compile(r"^(?:source|\.)\s+\S*activate\s+([\w.-]+)\s*$")
_CONDA_ACTIVATE_RE = re.compile(r"^(?:conda|mamba)\s+activate\s+([\w.-]+)\s*$")
_CONDA_RUN_RE = re.compile(r"^(?:conda|mamba)\s+run\s+(?:--no-capture-output\s+|--live-stream\s+)*"
                           r"(?:-n|--name)\s+([\w.-]+)\s+")
_TOOL_RUN_RE = re.compile(r"^(poetry|pipenv|uv|npx|bundle|yarn|pnpm)\s+(?:run|exec)\s+")
_NPX_RE = re.compile(r"^npx\s+(?:(?:--yes|-y|--no-install|--no)\s+)*")
_INTERP_VERSIONED_RE = re.compile(r"^(?:python|node)\d(?:\.\d+)?$")
_ENV_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+")
_WRAP_RE = re.compile(r"^/bin/(?:ba)?sh\s+-l?c\s+(.*)$", re.S)
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
_URL_HOST_RE = re.compile(r"https?://([^/\s'\"`]+)")

# Error classes an ENVIRONMENT produces (as opposed to the code under test). Order matters:
# the first match names the class.
ERROR_CLASSES = (
    ("module_missing", re.compile(r"ModuleNotFoundError|No module named|Cannot find module|"
                                  r"ERR_MODULE_NOT_FOUND", re.I)),
    ("command_missing", re.compile(r"command not found|: not found\b|is not recognized", re.I)),
    ("permission_denied", re.compile(r"Permission denied|EACCES", re.I)),
    ("usage_error", re.compile(r"(?m)^usage:|unrecognized arguments|no such option|"
                               r"error: argument|unknown option", re.I)),
    ("connection_refused", re.compile(r"Connection refused|Failed to connect to|"
                                      r"couldn't connect to|Could not connect|ECONNREFUSED", re.I)),
    ("name_resolution", re.compile(r"Could not resolve host|Temporary failure in name "
                                   r"resolution|Name or service not known|getaddrinfo|"
                                   r"nodename nor servname|ENOTFOUND", re.I)),
    ("unreachable", re.compile(r"Network is unreachable|No route to host|Connection timed out|"
                               r"Operation timed out|timed out after|ETIMEDOUT", re.I)),
)
NETWORK_ERRORS = frozenset({"connection_refused", "name_resolution", "unreachable"})
FORM_ERRORS = frozenset({"module_missing", "command_missing", "usage_error",
                         "permission_denied"})

# What "the probe returned something" looks like, per VCS probe kind. Deliberately narrow
# patterns so the output of an unrelated segment in a compound command cannot masquerade
# as a hit.
_PROBE_HIT = {
    "remote": re.compile(r"(?m)^\S+\s+\S+\s+\((?:fetch|push)\)\s*$"),
    "branch": re.compile(r"(?m)^\s*remotes/\S+"),
    "tag": re.compile(r"(?m)^v?\d+(?:\.\d+)+(?:[a-z]+\d*|\.post\d+|-\S+)?\s*$"),
    "fsck": re.compile(r"(?m)^(?:unreachable|dangling)\s+(?:commit|tree|blob|tag)\s+[0-9a-f]{7,}"),
    "stash": re.compile(r"(?m)^stash@\{\d+\}"),
    "refs": re.compile(r"(?m)^[0-9a-f]{7,}\s+refs/"),
}

# Words that would turn a fact into completion or verification advice.
_LINT_RE = re.compile(r"\b(skip|no need|don'?t|do not|stop|done|completed?|finish(?:ed)?|"
                      r"verify|verification|instead of (?:running|testing))\b", re.I)

_ERROR_PHRASE = {
    "module_missing": "ModuleNotFoundError",
    "command_missing": "command not found",
    "usage_error": "a usage error",
    "permission_denied": "permission denied",
}


# --------------------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------------------
def unwrap(cmd):
    """Strip an agent's `/bin/bash -lc "<cmd>"` wrapper, if present."""
    cmd = cmd or ""
    m = _WRAP_RE.match(cmd.strip())
    if not m:
        return cmd
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
        q = inner[0]
        inner = inner[1:-1]
        if q == '"':
            inner = inner.replace('\\"', '"').replace("\\$", "$").replace("\\\\", "\\")
    return inner


def segments(cmd):
    """Split a shell command into simple segments on && || ; | and newlines, outside quotes.
    Heredoc bodies are elided first so their contents are never mistaken for commands."""
    cmd = _HEREDOC_RE.sub(lambda m: m.group(0).split("\n", 1)[0] + " <heredoc>", cmd or "")
    out, buf, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            buf.append(c)
            if c == q:
                q = None
            elif c == "\\" and i + 1 < len(cmd):
                buf.append(cmd[i + 1]); i += 1
        elif c in "\"'":
            q = c; buf.append(c)
        elif c == "\n" or c == ";" or c == "|" or (c == "&" and cmd[i:i + 2] == "&&"):
            out.append("".join(buf)); buf = []
            if cmd[i:i + 2] in ("&&", "||"):
                i += 1
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return [s.strip().strip("()").strip() for s in out if s.strip().strip("()").strip()]


def _tokens(seg):
    return seg.split()


class Observation:
    """One classified command segment."""
    __slots__ = ("call", "intent", "form", "head", "prefix", "hosts", "probe", "outcome",
                 "segment")

    def __init__(self, call, intent, form, head, prefix, segment, hosts=(), probe=None):
        self.call, self.intent, self.form, self.head, self.prefix = call, intent, form, head, prefix
        self.segment, self.hosts, self.probe, self.outcome = segment, tuple(hosts), probe, None

    def __repr__(self):
        return f"Obs({self.intent},{self.form!r},{self.outcome})"


def classify_command(cmd, call_index=0, vocab=None):
    """Observations for one shell command (possibly compound). Activation prefixes carry
    across the segments of one command, which is how `source activate X && python ...`
    becomes a single form. `vocab` names the domain's intents (default Python/git)."""
    vocab = vocab or DEFAULT_VOCAB
    obs = []
    carried = ""
    for seg in segments(unwrap(cmd)):
        # Transparent wrappers first: `K=V rtk pytest`, `rtk K=V pytest` and `rtk proxy pytest`
        # are all a pytest run. rtk's Codex mode puts that prefix on every command the agent
        # types (trace.strip_rtk_prefix), and a first-token vocabulary that did not remove it
        # would form no fact at all on the very arm that stacks injection on rtk.
        seg = _ENV_ASSIGN_RE.sub("", seg).strip()
        seg = _ENV_ASSIGN_RE.sub("", strip_rtk_prefix(seg)).strip()
        if not seg:
            continue
        m = _ACTIVATE_RE.match(seg) or _CONDA_ACTIVATE_RE.match(seg)
        if m:
            carried = f"{seg.split()[0] if seg.startswith(('conda', 'mamba')) else 'source'} activate {m.group(1)} &&"
            if carried.startswith("conda") or carried.startswith("mamba"):
                carried = f"conda activate {m.group(1)} &&"
            continue
        prefix = carried
        body = seg
        m = _CONDA_RUN_RE.match(body)
        if m:
            prefix = (prefix + " " if prefix else "") + f"conda run -n {m.group(1)}"
            body = body[m.end():]
        m = _TOOL_RUN_RE.match(body)
        if m:
            prefix = (prefix + " " if prefix else "") + f"{m.group(1)} run"
            body = body[m.end():]
        else:
            m = _NPX_RE.match(body)
            if m and m.end() > 0:
                prefix = (prefix + " " if prefix else "") + "npx"
                body = body[m.end():]
        toks = _tokens(body)
        while toks and toks[0] in PREFIX_HEADS:
            toks = toks[1:] if toks[0] != "timeout" else toks[2:]
        if not toks:
            continue
        head = toks[0]
        base = head.rsplit("/", 1)[-1]
        rest = toks[1:]
        o = _classify(head, base, rest, body, prefix, seg, call_index, vocab)
        if o is not None:
            obs.append(o)
    return obs


def _runner_script(rest, vocab):
    for t in rest:
        if t.startswith("-"):
            continue
        for s in vocab.runner_scripts:
            if t == s or t.endswith("/" + s) or t.endswith(s):
                return t.lstrip("./") if t.startswith("./") else t
        return None
    return None


def _classify(head, base, rest, body, prefix, seg, call, vocab):
    def form(expr):
        return (prefix + " " + expr).strip() if prefix else expr

    if base in vocab.interpreters or _INTERP_VERSIONED_RE.match(base):
        if rest and all(t in ("--version", "-V", "-VV", "-v") for t in rest):
            return Observation(call, "probe_env", form(head), head, prefix, seg)
        if "-m" in rest:
            mod = rest[rest.index("-m") + 1] if rest.index("-m") + 1 < len(rest) else ""
            if mod in vocab.runner_modules:
                return Observation(call, "run_tests", form(f"{head} -m {mod}"), head, prefix, seg)
        script = _runner_script(rest, vocab)
        if script and (script != "manage.py" or "test" in rest):
            return Observation(call, "run_tests", form(f"{head} {script}"), head, prefix, seg)
        return Observation(call, "run_interpreter", form(head), head, prefix, seg)
    if base in vocab.runner_heads:
        return Observation(call, "run_tests", form(head), head, prefix, seg)
    for s in vocab.runner_scripts:
        if head == s or head.endswith("/" + s) or head == "./" + s:
            if s == "manage.py" and "test" not in rest:
                break
            # A script run through its shebang is the interpreter running it; spell the
            # form that way so `./tests/runtests.py` and `python tests/runtests.py` pool.
            return Observation(call, "run_tests",
                               form(f"{vocab.canonical_interpreter} " + head.lstrip("./")),
                               head, prefix, seg)
    if base in ("make", "npm", "yarn", "pnpm", "cargo", "go") and rest and rest[0] == "test":
        return Observation(call, "run_tests", form(f"{base} test"), head, prefix, seg)
    if base in vocab.env_probe_heads:
        return Observation(call, "probe_env", form(base), head, prefix, seg)
    if base in vocab.fetch_heads:
        return Observation(call, "fetch_remote", form(base), head, prefix, seg,
                           hosts=_URL_HOST_RE.findall(body))
    if base == "git" and rest:
        sub = rest[0]
        if sub in GIT_FETCH_SUBS:
            return Observation(call, "fetch_remote", form(f"git {sub}"), head, prefix, seg,
                               hosts=_URL_HOST_RE.findall(body))
        if sub in GIT_PROBE_SUBS:
            kind = _probe_kind(sub, rest[1:])
            if kind:
                return Observation(call, "probe_vcs_refs", form(f"git {sub}"), head, prefix,
                                   seg, probe=kind)
        return None
    if base in vocab.package_tools:
        if base.startswith(("pip", "npm", "yarn", "pnpm")) and rest and rest[0] in (
                "install", "download", "add", "ci"):
            hosts = _URL_HOST_RE.findall(body)
            return Observation(call, "fetch_remote", form(f"{base} {rest[0]}"), head, prefix,
                               seg, hosts=hosts)
        return Observation(call, "package_tool", form(base), head, prefix, seg)
    return None


def _probe_kind(sub, args):
    a = set(args)
    if sub == "remote":
        return "remote"
    if sub == "branch" and a & {"-a", "-r", "--all", "--remotes", "-ar", "-ra"}:
        return "branch"
    if sub == "tag" and "--contains" in a:      # plain `git tag` lists PAST tags too
        return "tag"
    if sub == "fsck":
        return "fsck"
    if sub == "stash" and args[:1] == ["list"]:
        return "stash"
    if sub in ("show-ref", "for-each-ref"):
        return "refs"
    return None


def error_class(output):
    for name, rx in ERROR_CLASSES:
        if rx.search(output or ""):
            return name
    return None


def _exit_code_hint(output):
    m = re.search(r"(?i)\bexit code[:=\s]+(\d+)", output or "")
    return int(m.group(1)) if m else None


def observe_task(transcript_path, vocab=None):
    """Per-task digest: observations with outcomes, the edited files, the call count.

    Outcome attribution inside a compound command is heuristic and conservative: an
    infrastructure error is attributed to the segment whose executable the error names, else
    to the last relevant segment; a call that succeeded marks every relevant segment as
    having run; a failed call with no infrastructure error marks only the first relevant
    segment as having run and leaves the others unknown (an `&&` chain may not have reached
    them)."""
    calls = load_trajectory(transcript_path)
    obs_all = []
    triples = []
    for idx, (name, args, out, ok) in enumerate(calls):
        tool = (name or "").lower()
        triples.append((tool, args if isinstance(args, dict) else {}, out or ""))
        if tool not in ("shell", "bash", "stop_bash"):
            continue
        cmd = (args.get("command") or args.get("cmd") or "") if isinstance(args, dict) else ""
        out = out or ""
        obs = classify_command(cmd, idx, vocab)
        if not obs:
            continue
        # Pass-through segments (a leading `cd`, a trailing `| head`, an `echo ---`
        # separator) neither add evidence nor pollute the output, so they do not break a
        # call's homogeneity; filters that rewrite content (grep, sed, awk, wc) do.
        n_seg = sum(1 for sg in segments(unwrap(cmd))
                    if (sg.split() or [""])[0].rsplit("/", 1)[-1] not in _PASSTHROUGH)
        homogeneous = n_seg == len(obs) and len({o.intent for o in obs}) == 1
        hint = _exit_code_hint(out)
        success = bool(ok) if hint is None else (hint == 0)
        err = error_class(out)
        if err in FORM_ERRORS or err in NETWORK_ERRORS:
            targets = [o for o in obs if o.intent in ("run_interpreter", "run_tests",
                                                       "package_tool", "fetch_remote")]
            named = [o for o in targets
                     if (o.head and o.head in out) or (o.head and o.head.rsplit("/", 1)[-1] in
                                                        _error_line(out, err))]
            if err in NETWORK_ERRORS:
                named = [o for o in targets if o.intent == "fetch_remote"
                         and (not o.hosts or any(h in out for h in o.hosts))] or \
                        [o for o in targets if o.intent == "fetch_remote"]
            chosen = named or (targets[-1:] if not success else [])
            for o in chosen:
                o.outcome = err
            if success:
                for o in obs:
                    if o.outcome is None:
                        o.outcome = "ran"
        elif success:
            for o in obs:
                o.outcome = "ran"
        else:
            first = True
            for o in obs:
                if o.intent in ("run_interpreter", "run_tests", "package_tool", "fetch_remote"):
                    o.outcome = "ran" if first else None
                    first = False
                else:
                    o.outcome = "ran" if o.intent != "probe_vcs_refs" else None
        for o in obs:
            if o.intent == "probe_vcs_refs":
                # Judged only when every segment of the call is a probe: another segment's
                # output (a `git describe`, a `sed`) would otherwise be read as the probe's.
                if not homogeneous:
                    o.outcome = None
                else:
                    o.outcome = "hit" if _PROBE_HIT[o.probe].search(out) else (
                        "empty" if success else None)
            if o.intent == "fetch_remote" and o.outcome == "ran":
                # Reachability needs a homogeneous call that exited 0 with real output and no
                # network error; a compound command's exit code and output belong to its
                # last segment, not to the fetch.
                if not homogeneous or not success or not out.strip() or err in NETWORK_ERRORS:
                    o.outcome = None
        obs_all.extend(obs)
    return {"obs": obs_all, "edited": sorted(edited_pyfiles(triples)), "n_calls": len(calls)}


def _error_line(out, err):
    rx = dict(ERROR_CLASSES)[err]
    m = rx.search(out or "")
    if not m:
        return ""
    start = (out or "").rfind("\n", 0, m.start()) + 1
    end = (out or "").find("\n", m.end())
    return (out or "")[start:end if end >= 0 else None]


def _is_bare(form):
    """A form with no activation prefix and no explicit path is 'bare'."""
    head = form.split()[0] if form else ""
    return "/" not in head and " activate " not in form and " run " not in form and \
        not form.startswith(("conda run", "poetry run", "pipenv run", "uv run", "npx "))


# --------------------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------------------
class EnvFact:
    def __init__(self, fid, family, high, params=None):
        self.id = fid
        self.family = family
        self.params = params or {}
        self.life = _Fact(fid, high)

    # ---- lifecycle passthrough ----
    @property
    def state(self):
        return self.life.state

    def injectable(self):
        return self.life.injectable()

    def firing_rate(self):
        return self.life.firing_rate()

    # ---- rendering ----
    def claim(self):
        p = self.params
        if self.family == "form_repair":
            b = self._best("b_counts")
            e = self._best("error_counts") or "module_missing"
            return f"`{p['a']} …` fails here with {_ERROR_PHRASE.get(e, e)}; `{b} …` works."
        if self.family == "boundary":
            hosts = [h for h, _ in Counter(p.get("hosts", {})).most_common(3)]
            return ("Outbound connections to " + _join(hosts) +
                    " are refused here; nothing can be fetched from them.")
        if self.family == "empty_probe":
            return ("This checkout has no git remote and no branches, tags or unreachable "
                    "objects beyond HEAD; its history ends at the checked-out commit.")
        return ""

    def _best(self, key):
        c = self.params.get(key) or {}
        return max(c.items(), key=lambda kv: kv[1])[0] if c else None

    def to_dict(self):
        return {"id": self.id, "family": self.family, "params": self.params,
                "life": self.life.to_dict()}

    @classmethod
    def from_dict(cls, d):
        f = cls(d["id"], d["family"], d["life"].get("high", False), d.get("params") or {})
        f.life = _Fact.from_dict(d["life"])
        return f


CONTRADICT_STREAK = 2     # FormRepair: consecutive contradicted tasks before one counts


def fold(fact, now, verdict, fired, exercised, files):
    """Fold one task's verdict into a fact's lifecycle.

    Two adjustments to the shared state machine. A high-stakes CANDIDATE that is contradicted
    retires at once (`_Fact` hard-kills only ACTIVE facts): an image whose history continues
    past HEAD does not become clean by being probed again. And a FormRepair contradiction --
    the bare form ran the test runner -- only counts after CONTRADICT_STREAK consecutive
    exercised tasks said so: a single task whose tests happened to need none of the missing
    packages is not a fixed environment, and the asymmetric contradiction weight would
    otherwise retire a fact confirmed thirty times over."""
    if fact.family == "form_repair" and exercised:
        streak = fact.params.get("contra_streak", 0)
        streak = streak + 1 if verdict == "contradict" else 0
        fact.params["contra_streak"] = streak
        if verdict == "contradict" and streak < CONTRADICT_STREAK:
            verdict = None
    fact.life.update(now, verdict, fired, exercised, files)
    if verdict == "contradict" and fact.life.high and fact.life.state == "CANDIDATE":
        fact.life.state, fact.life.retired_at = "RETIRED", now


def _root(form):
    """The part of a working form that carries the environment: its activation prefix, or
    the directory of its explicit path."""
    if " && " in form:
        return form.split(" && ", 1)[0]
    if form.startswith(("conda run", "poetry run", "pipenv run", "uv run", "npx")):
        return " ".join(form.split()[:3])
    head = form.split()[0] if form else ""
    return head.rsplit("/", 1)[0] if "/" in head else head


def _join(items):
    items = list(items)
    if not items:
        return "the hosts it tried"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def lint_ok(text):
    return not _LINT_RE.search(text or "")


# --------------------------------------------------------------------------------------
# the distiller
# --------------------------------------------------------------------------------------
class EnvFactsDistiller:
    """Learns and injects environment facts over a repository's task stream.

    Interface parity with `injection.Distiller`, so `PrefixStage` drives both the same way:
        d = EnvFactsDistiller(repo, vocab=...)      # fresh, or .load(repo, root, vocab=...)
        prefix = d.render()                         # block for the NEXT task ("" if none)
        d.update(transcript_path, iid=..., resolved=...)
        d.save(root)
    `runner`/`model` are accepted and ignored: nothing here is paid. `vocab` is the domain's
    `Vocabulary` (default Python/git); `injectable_claims()` is what a paid proposer stacked
    on top is told not to restate. `max_facts` / `max_tokens` cap the rendered block
    (default MAX_FACTS / MAX_TOKENS).
    """
    def __init__(self, repo, runner=None, model="agent", vocab=None, max_facts=None,
                 max_tokens=None):
        self.max_facts = int(max_facts or MAX_FACTS)
        self.max_tokens = int(max_tokens or MAX_TOKENS)
        self.repo = repo
        self.vocab = vocab or DEFAULT_VOCAB
        self.now = 0
        self.facts = {}                 # id -> EnvFact
        self.opt_tokens = 0.0           # invariant: LLM-free
        self.last_injected = set()      # fact ids rendered for the task now running
        self.last_digest = None         # the most recent per-task verdicts (telemetry)

    # ---- render --------------------------------------------------------------------
    def render(self, task=None):
        chosen = self._select()
        self.last_injected = {f.id for f in chosen}
        if not chosen:
            return ""
        lines = [HEADER] + [f"- {t}" for t in self._merged_lines(chosen)]
        return "\n".join(lines) + "\n"

    def injectable_claims(self):
        """The claims that would be injected now, one string each (no header)."""
        return [f.claim() for f in self._select()]

    def _select(self):
        cands = [f for f in self.facts.values() if f.injectable() and lint_ok(f.claim())]
        prio = {"form_repair": 0, "empty_probe": 1, "boundary": 2}
        cands.sort(key=lambda f: (prio.get(f.family, 9),
                                  0 if f.params.get("intent") == "run_interpreter" else 1,
                                  -f.firing_rate()))
        chosen = []
        for f in cands:
            trial = chosen + [f]
            if len(trial) > self.max_facts:
                break
            text = HEADER + "\n" + "\n".join(f"- {t}" for t in self._merged_lines(trial))
            if len(text) > self.max_tokens * 4:
                continue
            chosen = trial
        return chosen

    def _merged_lines(self, facts):
        """FormRepair facts whose working forms share a root (the same activation prefix
        or the same directory) collapse into one line: they are one environment fact."""
        lines, groups = [], {}
        for f in facts:
            if f.family == "form_repair":
                groups.setdefault(_root(f._best("b_counts") or ""), []).append(f)
            else:
                lines.append(f.claim())
        for _root_key, fs in groups.items():
            if len(fs) == 1:
                lines.insert(0, fs[0].claim())
                continue
            a_forms = _join([f"`{f.params['a']} …`" for f in fs])
            errs = sorted({_ERROR_PHRASE.get(f._best("error_counts") or "", "") for f in fs}
                          - {""})
            b_forms = _join(sorted({f"`{f._best('b_counts')} …`" for f in fs}))
            lines.insert(0, f"{a_forms} fail here ({', '.join(errs) or 'environment error'}); "
                            f"{b_forms} work.")
        return lines

    # ---- update --------------------------------------------------------------------
    def update(self, transcript_path, iid="", resolved=None, edited=None):
        try:
            digest = observe_task(transcript_path, self.vocab)
        except Exception:
            self.now += 1
            self.last_injected = set()
            return
        obs = digest["obs"]
        files = set(digest["edited"]) if edited is None else set(edited)
        injected = set(self.last_injected)
        verdicts = {}

        # ---- FormRepair: discover candidates, then fold every fact ----
        by_intent = {}
        for o in obs:
            by_intent.setdefault(o.intent, []).append(o)
        for intent in ("run_interpreter", "run_tests", "package_tool"):
            seq = by_intent.get(intent, [])
            fails = [o for o in seq if o.outcome in FORM_ERRORS]
            if intent != "run_tests":
                # An explicit path or activation that fails is a missing dependency, not a
                # resolution problem; only a bare form can be "repaired" by a fuller one.
                fails = [o for o in fails if _is_bare(o.form)]
            for a in fails:
                later_ok = [o for o in seq if o.outcome == "ran" and o.call > a.call
                            and o.form != a.form
                            and (intent == "run_tests" or not _is_bare(o.form))]
                if not later_ok:
                    continue
                fid = f"form_repair:{intent}:{a.form}"
                f = self.facts.get(fid)
                if f is None:
                    f = EnvFact(fid, "form_repair", high=False,
                                params={"intent": intent, "a": a.form,
                                        "b_counts": {}, "error_counts": {}})
                    self.facts[fid] = f
                for o in later_ok:
                    f.params["b_counts"][o.form] = f.params["b_counts"].get(o.form, 0) + 1
                f.params["error_counts"][a.outcome] = f.params["error_counts"].get(a.outcome, 0) + 1
        for f in [x for x in self.facts.values() if x.family == "form_repair"]:
            intent, a_form = f.params["intent"], f.params["a"]
            seq = by_intent.get(intent, [])
            exercised = bool(seq)
            a_failed = any(o.form == a_form and o.outcome in FORM_ERRORS for o in seq)
            b_forms = set(f.params["b_counts"])
            b_ran = any(o.form in b_forms and o.outcome == "ran" for o in seq) or \
                any(o.outcome == "ran" and not _is_bare(o.form) for o in seq)
            # Contradiction: the bare form ran the TEST RUNNER without an environment error.
            # A bare interpreter one-liner succeeding is not evidence (pure-Python packages
            # import from the working tree under any interpreter).
            a_runner_ok = any(o.form == a_form and o.outcome == "ran"
                              for o in by_intent.get("run_tests", []))
            fired = a_failed or (f.id in injected and b_ran and not a_failed)
            if a_failed or (b_ran and not a_runner_ok):
                verdict = "confirm"
            elif a_runner_ok:
                verdict = "contradict"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[f.id] = (verdict, fired, exercised)

        # ---- Boundary: hosts refused ----
        fetches = by_intent.get("fetch_remote", [])
        failed_hosts = Counter(h for o in fetches if o.outcome in NETWORK_ERRORS for h in o.hosts)
        ok_hosts = {h for o in fetches if o.outcome == "ran" for h in o.hosts}
        fid = "boundary:fetch_remote"
        f = self.facts.get(fid)
        if f is None and failed_hosts:
            f = EnvFact(fid, "boundary", high=True, params={"hosts": {}})
            self.facts[fid] = f
        if f is not None:
            for h, n in failed_hosts.items():
                f.params["hosts"][h] = f.params["hosts"].get(h, 0) + n
            known = set(f.params["hosts"])
            exercised = any(o.hosts for o in fetches)
            fired = bool(failed_hosts)
            if ok_hosts & known:
                verdict = "contradict"
            elif failed_hosts:
                verdict = "confirm"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[fid] = (verdict, fired, exercised)

        # ---- EmptyProbe: refs beyond the checkout ----
        probes = [o for o in by_intent.get("probe_vcs_refs", []) if o.outcome in ("hit", "empty")]
        fid = "empty_probe:vcs_refs"
        f = self.facts.get(fid)
        if f is None and probes:
            f = EnvFact(fid, "empty_probe", high=True, params={"kinds": {}})
            self.facts[fid] = f
        if f is not None:
            for o in probes:
                f.params["kinds"][o.probe] = f.params["kinds"].get(o.probe, 0) + 1
            exercised = bool(probes)
            fired = exercised
            if any(o.outcome == "hit" for o in probes):
                verdict = "contradict"
            elif probes:
                verdict = "confirm"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[fid] = (verdict, fired, exercised)

        self.last_digest = {"iid": iid, "resolved": resolved, "injected": sorted(injected),
                            "verdicts": verdicts, "n_calls": digest["n_calls"]}
        self.last_injected = set()
        self.now += 1

    # ---- persistence -------------------------------------------------------------
    @staticmethod
    def _path(root, repo):
        d = os.path.join(root, STATE_SUBDIR)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, repo.replace("/", "__") + ".json")

    def save(self, root):
        with open(self._path(root, self.repo), "w", encoding="utf-8") as fh:
            json.dump({"repo": self.repo, "now": self.now, "opt_tokens": 0.0,
                       "vocab": self.vocab.name,
                       "facts": {k: f.to_dict() for k, f in self.facts.items()}}, fh, indent=2)

    @classmethod
    def load(cls, repo, root, runner=None, model="agent", vocab=None, max_facts=None,
             max_tokens=None):
        self = cls(repo, vocab=vocab, max_facts=max_facts, max_tokens=max_tokens)
        p = self._path(root, repo)
        if not os.path.exists(p):
            return self
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        self.now = d.get("now", 0)
        self.facts = {k: EnvFact.from_dict(v) for k, v in d.get("facts", {}).items()}
        return self

    # ---- telemetry ---------------------------------------------------------------
    def debug_row(self):
        row = {}
        for k, f in self.facts.items():
            row[k] = {"state": f.state, "fire": f"{f.life.fire_num}/{f.life.fire_den}",
                      "rate": round(f.firing_rate(), 2), "inject": f.injectable(),
                      "claim": f.claim()[:120], "family": f.family}
        row["_profile"] = "envfacts"
        row["_vocab"] = self.vocab.name
        if self.last_digest:
            row["_task"] = self.last_digest
        return row
