"""The spiral guard, one tool call at a time.

`vendor.spiral.NudgeGuard` is the live controller the experiments ran: it folds each completed
tool call of a streamed rollout into a `SpiralState`, asks `assess_abort` (cheap gate -> sampled
judge -> envelope fallback) whether the trajectory is stuck, and answers the first stuck
detection with a nudge and a later one with an abort. It does that inside one process that owns
the stream. Here every tool call arrives as a separate PostToolUse hook process, so `TurnGuard`
is the same control flow with its state on disk between calls. The decisions are the vendored
ones -- `SpiralState.update`, `is_stuck`, `assess_abort`, `nudge_text`, the cumulative backstop,
`nudge_on_ambiguous` -- not a re-implementation of their thresholds.

Three things differ, each because the setting does:

  * The unit is a PROMPT-TURN (one user prompt to the agent's stop), not a benchmark task. In
    interactive use the person already splits work into short turns (median 5 tool calls on the
    local replay), and a whole session folded as one trajectory looks stuck merely by being long.
  * The nudge is delivered mid-turn, attached to the tool result, instead of by killing the agent
    and resuming it with a continuation prompt. The per-segment reset that gives the agent a fair
    re-assessment after a nudge is kept: `after_nudge` starts a fresh `SpiralState`, exactly as
    `NudgeGuard.continuation` does.
  * There is no grader, so the per-repository envelope learns from turns that ENDED GREEN: the
    agent stopped on its own, the guard did not abort it, and no verification after its last
    edit failed (`resolved_proxy`). The envelope is the same `EnvelopeTracker`, fed that proxy.
"""
import re
import shlex
import time

from tokeneconomizer.vendor import spiral

_SLOTS = spiral.SpiralState.__slots__

# What counts as running the project's own checks, across ecosystems. Broader than the vendored
# `_TEST_RUN_RE` (which is the SWE-bench/Python vocabulary the guard's thresholds were tuned
# on); used ONLY for the ended-green proxy, never to change what `SpiralState` counts.
VERIFY_RE = re.compile(
    r"(?i)(?:^|[\s;&|(])(?:pytest|py\.test|tox|nox|python\d?(?:\.\d+)?\s+-m\s+(?:pytest|unittest)|"
    r"manage\.py\s+test|runtests(?:\.py)?|jest|vitest|mocha|ava|playwright\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|go\s+test|cargo\s+(?:test|nextest)|"
    r"(?:mvn|mvnw|\./mvnw)\s+(?:\S+\s+)*test|(?:gradle|gradlew|\./gradlew)\s+(?:\S+\s+)*test|"
    r"make\s+(?:test|check)|ctest|rspec|phpunit|dotnet\s+test|swift\s+test|mix\s+test|"
    r"deno\s+test|bazel\s+test|(?:\./)?bin/test)\b")
_EDIT = {"edit", "create"}


# ---- edits made through the shell ----------------------------------------------------------
# The vendored SpiralState counts an edit only when an edit TOOL makes it (Edit/Write/
# str_replace). An agent that edits through Bash -- `sed -i`, `perl -pi`, a redirect or `tee`
# into a source file, an inline Python script that writes the file back, `patch`/`git apply` --
# looked like one that had edited nothing. Sonnet 5.5 edits that way; in the user-simulation
# pilot the paid judge's digest read `edit_churn=0`, and its nudge told the agent "no files have
# actually been edited" when four had. Such edits are folded in AFTER the vendored update, so its
# counts and thresholds are untouched and the call still counts once.
_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_OPS = {"&&", "||", ";", ";;", "|", "|&", "&", "\n", "(", ")"}
_OUT = {">", ">>", ">|", "&>", "&>>"}                  # redirects that write their operand
_SKIP_OPERAND = {"<", "<<", "<<<", "<&", ">&", "<>"}   # input, heredoc tags, fd duplication
# Redirect and `tee` targets count only when they look like source, so `> out.log` or
# `| tee /tmp/run.txt` -- capturing output -- is not mistaken for an edit.
_SOURCE_FILE = re.compile(
    r"\.(?:py|pyi|pyx|pxd|js|jsx|mjs|cjs|ts|tsx|rs|go|java|kt|kts|scala|c|cc|cpp|cxx|h|hh|hpp|"
    r"rb|php|cs|swift|m|mm|sh|bash|zsh|lua|jl|ex|exs|erl|hs|ml|clj|sql|toml|ya?ml|json|cfg|ini|"
    r"conf|md|rst|html?|css|scss|vue|svelte|xml|gradle|properties|in)$", re.I)
_PYTHON = re.compile(r"(?:^|[\s/;&|(])python\d?(?:\.\d+)?\b")
_PY_OPEN_LIT = re.compile(r"open\(\s*(['\"])([^'\"\n]+)\1\s*,\s*(['\"])([^'\"]*)\3")
_PY_OPEN_VAR = re.compile(r"open\(\s*([A-Za-z_]\w*)\s*,\s*(['\"])([^'\"]*)\2")
_PY_WRITE_PATH_LIT = re.compile(
    r"(?:pathlib\.)?Path\(\s*(['\"])([^'\"\n]+)\1\s*\)\.write_(?:text|bytes)\(")
_PY_WRITE_PATH_VAR = re.compile(r"\b([A-Za-z_]\w*)\.write_(?:text|bytes)\(")


def _not_scratch(path):
    return bool(path) and not path.startswith(("/dev/", "/proc/", "/tmp/"))


def _unquote(t):
    return t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t


def _split_heredocs(cmd):
    """(the command's shell text without heredoc bodies, the bodies)."""
    shell, bodies, tag, body = [], [], None, []
    for line in cmd.split("\n"):
        if tag is not None:
            if line.strip() == tag:
                bodies.append("\n".join(body))
                tag, body = None, []
            else:
                body.append(line)
            continue
        shell.append(line)
        m = _HEREDOC_START.search(line)
        if m:
            tag = m.group(2)
    if tag is not None:
        bodies.append("\n".join(body))
    return "\n".join(shell), bodies


def _tokens(text):
    """Shell words and operators. Quotes stay on quoted words, so a quoted `'>'` or `'|'` is
    never read as an operator; ValueError on unbalanced quotes."""
    lex = shlex.shlex(text, posix=False, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    return list(lex)


def _commands(shell):
    """(simple commands as unquoted word lists, the files their redirects write)."""
    try:
        streams = [_tokens(shell)]
    except ValueError:                          # one bad line must not hide the others
        streams = []
        for line in shell.split("\n"):
            try:
                streams.append(_tokens(line) + ["\n"])
            except ValueError:
                continue
    cmds, writes = [], []
    for toks in streams:
        cur, i = [], 0
        while i < len(toks):
            t = toks[i]
            if t in _OPS:
                if cur:
                    cmds.append(cur)
                cur = []
            elif t in _OUT or t in _SKIP_OPERAND:
                if cur and cur[-1].isdigit():   # `2>`: the fd number is not an operand
                    cur.pop()
                if t in _OUT and i + 1 < len(toks) and toks[i + 1] not in _OPS:
                    writes.append(_unquote(toks[i + 1]))
                i += 1
            else:
                cur.append(_unquote(t))
            i += 1
        if cur:
            cmds.append(cur)
    return cmds, writes


def _inplace_files(tokens, inplace_flag, script_flag):
    """File operands of an in-place editor (`sed -i`, `perl -pi`), or [] if not in place."""
    if not any(inplace_flag(t) for t in tokens[1:]):
        return []
    files, have_script, skip = [], False, False
    for t in tokens[1:]:
        if skip:
            skip, have_script = False, True
            continue
        if not t or t.startswith("-"):
            if t and script_flag(t):
                skip = True                     # the next token is the script
            continue
        if not have_script:
            have_script = True                  # the first operand is the script
            continue
        files.append(t)
    return files


def _py_writes(text):
    """Paths an inline Python script writes; "(script)" for a write whose path this cannot
    resolve."""
    out = []
    for m in _PY_OPEN_LIT.finditer(text):
        if set(m.group(4)) & set("wax+"):
            out.append(m.group(2))
    variables = [m.group(1) for m in _PY_OPEN_VAR.finditer(text) if set(m.group(3)) & set("wax+")]
    out += [m.group(2) for m in _PY_WRITE_PATH_LIT.finditer(text)]
    variables += [m.group(1) for m in _PY_WRITE_PATH_VAR.finditer(text)]
    for var in dict.fromkeys(variables):
        found = re.findall(rf"\b{re.escape(var)}\s*=\s*(?:(?:pathlib\.)?Path\(\s*)?(['\"])"
                           rf"([^'\"\n]+)\1", text)
        loop = re.search(rf"\bfor\s+{re.escape(var)}\s+in\s+[\[(]([^\])]*)[\])]", text)
        paths = [p for _, p in found]
        if loop:
            paths += re.findall(r"['\"]([^'\"\n]+)['\"]", loop.group(1))
        out += paths or ["(script)"]
    return out


def shell_edit_targets(cmd):
    """Basenames of the files a shell command writes, in order, without repeats."""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    shell, bodies = _split_heredocs(cmd)
    cmds, writes = _commands(shell)
    found = [w for w in writes if _SOURCE_FILE.search(w)]
    for tokens in cmds:
        while tokens and (tokens[0] in ("sudo", "command", "env", "time", "nohup")
                          or re.match(r"^\w+=", tokens[0])):
            tokens = tokens[1:]
        if not tokens:
            continue
        head = tokens[0].rsplit("/", 1)[-1]
        if head == "sed":
            found += _inplace_files(
                tokens, lambda t: bool(re.match(r"^(?:-[a-zA-Z]*i|--in-place)", t)),
                lambda t: t in ("-e", "-f", "--expression", "--file"))
        elif head == "perl":
            found += _inplace_files(tokens, lambda t: bool(re.match(r"^-[a-zA-Z]*i", t)),
                                    lambda t: bool(re.match(r"^-[a-zA-Z]*e$", t)))
        elif head == "tee":
            found += [t for t in tokens[1:] if not t.startswith("-") and _SOURCE_FILE.search(t)]
        elif head == "patch" or (head == "git" and len(tokens) > 1 and tokens[1] == "apply"):
            found.append("(patch)")
        elif head == "git" and len(tokens) > 2 and tokens[1] in ("checkout", "restore"):
            rest = (tokens[tokens.index("--") + 1:] if "--" in tokens
                    else tokens[2:] if tokens[1] == "restore" else [])
            found += [t for t in rest if not t.startswith("-")]
    if _PYTHON.search(shell):
        found += _py_writes("\n".join([shell] + bodies))
    names = [p if p.startswith("(") else spiral._basename(p)
             for p in found if p.startswith("(") or _not_scratch(p)]
    return list(dict.fromkeys(n for n in names if n))


def fold_shell_edits(st, names):
    """Count `names` as edits in a SpiralState, and say so in its action log."""
    for b in names:
        st.edit_order.append(b)
        st.edit_counts[b] = st.edit_counts.get(b, 0) + 1
    if names and st.recent:
        shown = ", ".join(names[:4]) + (", ..." if len(names) > 4 else "")
        st.recent[-1] = f"{st.recent[-1]}  [edits {shown}]"


def state_to_dict(st):
    return {k: getattr(st, k) for k in _SLOTS}


def state_from_dict(d):
    st = spiral.SpiralState()
    for k in _SLOTS:
        if k in (d or {}):
            setattr(st, k, d[k])
    return st


class TurnGuard:
    """`NudgeGuard` for one prompt-turn, persisted between hook calls via `to_dict`."""

    JUDGE_BASE_GAP = 5
    JUDGE_MAX_GAP = 20
    # A background verdict that never came back (its job died) stops blocking the next one
    # after this long; a side call is capped at 180 s.
    JUDGE_STALE_S = 300

    def __init__(self, turn_key="", data=None):
        d = data or {}
        self.turn_key = d.get("turn_key", turn_key)
        self.st = state_from_dict(d.get("st"))          # this segment; reset after a nudge
        self.whole = state_from_dict(d.get("whole"))    # the whole turn; never reset
        self.cum_turns = int(d.get("cum_turns", 0))
        self.segment = int(d.get("segment", 0))
        self.nudges_done = int(d.get("nudges_done", 0))
        self.aborted = bool(d.get("aborted", False))
        self.abort_delivered = bool(d.get("abort_delivered", False))
        self.events = list(d.get("events", []))
        self.judge_last = int(d.get("judge_last", -999))
        self.judge_gap = int(d.get("judge_gap", self.JUDGE_BASE_GAP))
        self.judge_calls = int(d.get("judge_calls", 0))
        self.judge_tokens = float(d.get("judge_tokens", 0) or 0)
        self.writer_tokens = float(d.get("writer_tokens", 0) or 0)
        self.last_edit = int(d.get("last_edit", 0))
        self.last_verify = int(d.get("last_verify", 0))
        self.last_verify_failed = bool(d.get("last_verify_failed", False))
        self.edits = int(d.get("edits", 0))
        self.verifications = int(d.get("verifications", 0))
        self.judge_pending = d.get("judge_pending") or None   # {"id", "since", "segment"}
        self.nudge_ready = d.get("nudge_ready") or None       # a redirect the judge wrote

    def to_dict(self):
        return {"turn_key": self.turn_key, "st": state_to_dict(self.st),
                "whole": state_to_dict(self.whole),
                "cum_turns": self.cum_turns, "segment": self.segment,
                "nudges_done": self.nudges_done, "aborted": self.aborted,
                "abort_delivered": self.abort_delivered, "events": self.events[-20:],
                "judge_last": self.judge_last, "judge_gap": self.judge_gap,
                "judge_calls": self.judge_calls, "judge_tokens": self.judge_tokens,
                "writer_tokens": self.writer_tokens, "last_edit": self.last_edit,
                "last_verify": self.last_verify, "last_verify_failed": self.last_verify_failed,
                "edits": self.edits, "verifications": self.verifications,
                "judge_pending": self.judge_pending, "nudge_ready": self.nudge_ready}

    # ---- outcome tracking for the ended-green proxy ----------------------------------
    def _track(self, name, args, result, exit_code, shell_edits=()):
        canon = spiral._canon_tool(name)                 # the vendored tool-name mapping
        if canon in _EDIT:
            self.last_edit = self.cum_turns
            self.edits += 1
            return
        cmd = (args or {}).get("command") if canon == "bash" else None
        verifies = isinstance(cmd, str) and bool(VERIFY_RE.search(cmd))
        if shell_edits:
            # `sed -i ... && pytest`: the edit comes first, so the run verifies it.
            self.last_edit = self.cum_turns - (1 if verifies else 0)
            self.edits += 1
        if verifies:
            self.last_verify = self.cum_turns
            self.verifications += 1
            if exit_code is not None:
                self.last_verify_failed = exit_code != 0
            else:                                         # Claude Code reports no exit code
                self.last_verify_failed = bool(spiral._ERR_RE.search(result or ""))

    def resolved_proxy(self, stopped_normally=True):
        """Did this turn END GREEN? The stand-in for the grader the envelope cannot have."""
        if not stopped_normally or self.aborted or self.cum_turns == 0:
            return False
        return not (self.last_verify_failed and self.last_verify > self.last_edit)

    # ---- what the judge is told -------------------------------------------------------
    # The vendored digest says "Tests are still NOT green." whenever it has an envelope: on the
    # benchmark the guard only ever watched an unfinished task. Here the guard knows how the
    # turn's last verification went, so the judge -- and the redirect it writes -- is told that.
    _NOT_GREEN = "Tests are still NOT green."

    def verification_state(self):
        if not self.verifications:
            return "No test or verification run has been seen in this turn."
        state = "failed" if self.last_verify_failed else "passed"
        since = " There have been edits since." if self.last_edit > self.last_verify else ""
        return f"The last verification run {state}.{since}"

    def live_digest(self, digest):
        return (digest or "").replace(self._NOT_GREEN, self.verification_state())

    # ---- the judge, rate-limited with backoff exactly as NudgeGuard._sampled_judge ----
    def _backoff(self, v):
        if v and v.get("stuck") is False:
            self.judge_gap = min(self.JUDGE_MAX_GAP, self.judge_gap + self.JUDGE_BASE_GAP)
        else:
            self.judge_gap = self.JUDGE_BASE_GAP

    def _sampled(self, judge):
        def call(digest):
            if self.st.turns - self.judge_last < self.judge_gap:
                return None
            self.judge_last = self.st.turns
            self.judge_calls += 1
            try:
                v = judge(self.live_digest(digest))
            except Exception:
                v = None
            if v:
                self.judge_tokens += v.get("_tokens") or 0
            self._backoff(v)
            return v
        return call

    def _background(self, judge):
        """`_sampled` for a judge that runs in the background (`te_live.judge.AsyncJudge`):
        a verdict is taken whenever one has come back, a new job starts under the same rate
        limit, and while one is out the judge answers nothing -- which `assess_abort` reads as
        throttled, the same as a call the rate limit skipped."""
        def call(digest):
            found, v = judge.poll(self)
            if found:
                self.judge_pending = None
                if v:
                    self.nudge_ready = (v.get("_nudge") or "").strip() or None
                self._backoff(v)
                return v or None
            if self.judge_pending:
                if time.time() - float(self.judge_pending.get("since") or 0) < self.JUDGE_STALE_S:
                    return None
                self.judge_pending = None                 # its job never answered
            if self.st.turns - self.judge_last < self.judge_gap:
                return None
            self.judge_last = self.st.turns
            self.judge_calls += 1
            job = judge.start(self, self.live_digest(digest))
            if job:
                self.judge_pending = {"id": job, "since": time.time(), "segment": self.segment}
            return None
        return call

    # ---- one completed tool call ------------------------------------------------------
    def fold(self, name, args, result, exit_code=None, *, envelope=None, thr=None,
             judge=None, nudge_budget=1):
        """Fold one call; return None, ("nudge", reason) or ("abort", reason).

        The body is `NudgeGuard.monitor` for a single completed call."""
        thr = thr or spiral.DEFAULT_THRESHOLDS
        shell_edits = (shell_edit_targets((args or {}).get("command"))
                       if spiral._canon_tool(name) == "bash" else [])
        self.st.update(name, args or {}, result or "")
        self.whole.update(name, args or {}, result or "")
        fold_shell_edits(self.st, shell_edits)
        fold_shell_edits(self.whole, shell_edits)
        self.cum_turns += 1
        self._track(name, args, result, exit_code, shell_edits)
        if self.aborted:
            return None

        backstop = thr.get("backstop_turns")
        if backstop is None and envelope is not None:
            backstop = envelope * thr["backstop_mult"]
        if (backstop is not None and self.cum_turns >= backstop
                and spiral.is_stuck(self.st, thr)):
            # The ladder is nudge, THEN stop. In the experiments the envelope was ~30 calls, so
            # its 2.5x backstop lay well past the stuck rule's 20-call floor and the nudge came
            # first. An interactive repository's envelope is ~4-10 calls: its backstop falls
            # INSIDE that floor, so the moment a turn first counts as stuck the backstop has
            # tripped too, and it pre-empted every nudge -- found by replaying a stuck trace
            # before a live test (2026-10-07). An unspent nudge goes first; after it, a fresh
            # segment must be stuck again before the backstop stops anything.
            reason = f"cum-backstop:{self.cum_turns}>={backstop:.0f}t"
            if self.nudges_done < nudge_budget:
                return self._stop("nudge", reason, envelope)
            return self._stop("abort", reason, envelope)

        wired = None
        if judge is not None:
            wired = self._background(judge) if hasattr(judge, "poll") else self._sampled(judge)
        abort, reason = spiral.assess_abort(self.st, envelope=envelope, judge=wired, thr=thr)
        if not abort:
            if (thr.get("nudge_on_ambiguous") and reason == "judge-ambiguous-spared"
                    and self.nudges_done < nudge_budget):
                return self._stop("nudge", "ambiguous-past-envelope", envelope)
            return None
        if self.nudges_done < nudge_budget:
            return self._stop("nudge", reason, envelope)
        return self._stop("abort", reason, envelope)

    def _stop(self, action, reason, envelope):
        self.events.append({"segment": self.segment, "action": action,
                            "turn_in_segment": self.st.turns, "cum_turns": self.cum_turns,
                            "envelope": envelope, "reason": reason,
                            "snapshot": self.st.snapshot()})
        if action == "abort":
            self.aborted = True
        return action, reason

    def nudge_body(self, envelope=None, writer=None):
        """The redirect text: the one a background judge already wrote with its verdict, else
        an LLM-written one when a writer is wired, else the vendored count template
        (`spiral.nudge_text`), which mirrors only the agent's own actions."""
        if self.nudge_ready:
            text, self.nudge_ready = self.nudge_ready, None
            return text
        if writer is not None:
            try:
                out = writer(self.live_digest(self.st.recent_digest(envelope=envelope)))
            except Exception:
                out = None
            if out:
                self.writer_tokens += out.get("_tokens") or 0
                text = (out.get("text") or "").strip()
                if text:
                    return text
        return spiral.nudge_text(self.st)

    def after_nudge(self):
        """Start the post-nudge segment with a fresh state: `NudgeGuard.continuation`'s reset,
        which is the agent's fair re-assessment window."""
        self.nudges_done += 1
        self.segment += 1
        self.st = spiral.SpiralState()
        self.judge_last = -999
        self.judge_gap = self.JUDGE_BASE_GAP
        self.judge_pending = None          # a verdict on the old segment is about the old one
        self.nudge_ready = None

    def summary(self, stopped_normally=True):
        return {"turn_key": self.turn_key, "tool_calls": self.cum_turns,
                "nudges": self.nudges_done, "aborted": self.aborted,
                "resolved_proxy": self.resolved_proxy(stopped_normally),
                "edits": self.edits, "verifications": self.verifications,
                "judge_tokens": self.judge_tokens, "writer_tokens": self.writer_tokens}


# ---- the per-repository envelope ---------------------------------------------------------
ENVELOPE_HISTORY = 500      # recent ended-green turns kept for the percentile


# Overrides that shape the envelope itself, passed to the tracker; the rest are pinned over the
# thresholds it returns.
_ENVELOPE_KEYS = {"envelope_margin": "margin", "envelope_pct": "pct",
                  "envelope_min_samples": "min_samples"}


def load_envelope(store, level="default", overrides=None):
    d = store.read("envelope.json") or {}
    ov = overrides or {}
    tracker = spiral.EnvelopeTracker(level=level, **{
        arg: ov[key] for key, arg in _ENVELOPE_KEYS.items() if key in ov})
    tracker.resolved_turns = [int(x) for x in d.get("resolved_turns", []) if x]
    tracker.resolved_snaps = [s for s in d.get("resolved_snaps", []) if isinstance(s, dict)]
    return tracker


def thresholds(tracker, overrides=None):
    """The thresholds a turn is checked against: the preset, what this repository's history
    learned on top of it, then a person's pinned values (`spiral.<name>`), which win."""
    thr = tracker.thresholds(max_turns=None)
    for key, value in (overrides or {}).items():
        if key not in _ENVELOPE_KEYS:
            thr[key] = value
    if "backstop_mult" in (overrides or {}) and tracker.envelope is not None:
        thr["backstop_turns"] = tracker.envelope * thr["backstop_mult"]
    return thr


def base_thresholds(level="default", overrides=None):
    """The thresholds before any learning: the preset with a person's pinned values on top.
    What `te config show` reports, so the numbers a setting replaces are visible."""
    thr = spiral.scaled_thresholds(level)
    thr.update(overrides or {})
    thr.pop("restart_budget", None)
    return thr


def save_envelope(store, tracker):
    store.write("envelope.json", {
        "level": tracker.level,
        "resolved_turns": tracker.resolved_turns[-ENVELOPE_HISTORY:],
        "resolved_snaps": tracker.resolved_snaps[-ENVELOPE_HISTORY:]})


def record_turn(store, guard, level="default", stopped_normally=True, overrides=None):
    """Fold a finished turn into the envelope -- under the store lock, since two sessions in
    one repository share it. Only turns that ended green feed it, as only resolved tasks fed
    the experiments' envelope; a turn's own outcome gates only later turns."""
    # The snapshot is of the WHOLE turn (`guard.whole`), not the post-nudge segment: the
    # experiments snapshot a resolved task's full trajectory (`spiral_snapshot(transcript)`).
    with store.locked("envelope"):
        tracker = load_envelope(store, level, overrides)
        tracker.update(guard.cum_turns, guard.resolved_proxy(stopped_normally),
                       snapshot=guard.whole.snapshot())
        save_envelope(store, tracker)
    return tracker
