#!/usr/bin/env python3
"""
v4.1 mechanism B + C: runtime spiral detector and escalation-ladder decision.

The cost of a SWE-bench stream is brutally heavy-tailed: the top ~10% of tasks carry
~50% of cache-aware effective cost, and that tail is dominated by long, UNRESOLVED
spirals (the agent thrashing in an edit->test->fail loop while its re-fed context
grows ~quadratically). This module:

  detect_incremental(events)   STAGE B (free) — fold the trajectory so far into a
                               compact SpiralState (edit churn, distinct edited files,
                               error density, repeated near-identical edit->test->fail
                               cycles, turn count).
  decide(state, envelope)      STAGE C — given the per-repo RESOLVED-turn envelope,
                               return the cheapest sufficient ladder action:
                                 none -> nudge -> compact -> restart -> abort.

Design constraints (learned this milestone):
  * High turn-count is REPO-DEPENDENT: matplotlib's long tasks mostly resolve, only
    xarray's are doomed. So the heavy interventions (restart/abort) are gated on the
    per-repo `envelope` = a margin beyond the longest RESOLVED task seen so far; on a
    repo where long==hard-but-solvable the envelope is high and they rarely fire.
  * The lightest rung (nudge) is leakage-safe BY CONSTRUCTION: it only mirrors the
    agent's OWN repeated actions back at it ("you tried N similar things, same
    result"); it carries no external knowledge and never says "stop", only "try
    something different" — so it dodges the effort-capping ban.
  * abort is the terminal cap (autonomous/CI only); interactive callers should map it
    to a hand-off instead.

Stdlib only. The event schema mirrors reflect.load_trajectory (Copilot CLI
tool.execution_start / tool.execution_complete), so this runs offline on saved
transcripts AND live on a streamed JSONL.
"""
import hashlib
import json
import re

# An output that RAN code to verify behavior (the test/repro step of the edit->test
# loop). Mirrors reflect.TEST_RUN_RE intent but local to keep this module standalone.
_TEST_RUN_RE = re.compile(
    r"(runtests(\.py)?|\bpytest\b|py\.test|manage\.py\s+test|python\s+-m\s+unittest|"
    r"\btox\b|\bnox\b|\btrial\b|python\d?\s+-c\b)", re.IGNORECASE)
_ERR_RE = re.compile(
    r"(Traceback \(most recent call last\)|\b\w*Error\b|\bFAILED\b|\bassert\b|"
    r"Exception|errors during collection)", re.IGNORECASE)
_EDIT_TOOLS = ("edit", "str_replace", "create")

# Normalize agent-specific tool names to the copilot-style canonical names the folding
# logic below keys on, so ONE code path handles both the Copilot stream schema and Claude
# Code's stream-json (which names tools Bash/Read/Edit/Write/Grep/Glob/MultiEdit). Unknown
# names pass through unchanged.
_TOOL_CANON = {
    "bash": "bash", "shell": "bash",
    "read": "view", "view": "view",
    "grep": "grep",
    "glob": "glob",
    "edit": "edit", "multiedit": "edit", "str_replace": "edit",
    "apply_patch": "edit",
    "write": "create", "create": "create",
}


def _canon_tool(name):
    return _TOOL_CANON.get((name or "").lower(), name or "?")


def completed_calls(line, pend):
    """Schema-agnostic fold of ONE streamed JSONL line into completed (name, args, result)
    tool calls, so the live guards work for BOTH the Copilot stream
    (tool.execution_start/complete) AND Claude Code's stream-json (assistant.tool_use /
    user.tool_result). `pend` is a caller-owned dict {callId -> {name,args}} carried across
    lines to pair a start/tool_use with its later complete/tool_result. Yields a list of
    completed calls finished on THIS line (0, 1, or — for a Claude user msg batching
    several tool_results — more). Tool names are canonicalized so downstream keying is
    uniform; unpaired results fall back to name '?'."""
    try:
        ev = json.loads(line)
    except Exception:
        return []
    t = ev.get("type")
    out = []
    # ---- Codex exec --json schema ----
    if t == "item.completed":
        item = ev.get("item") or {}
        kind = item.get("type")
        if kind == "command_execution":
            out.append(("bash", {"command": item.get("command") or ""},
                        item.get("aggregated_output") or ""))
        elif kind == "file_change":
            changes = item.get("changes") or []
            paths = [c.get("path") for c in changes
                     if isinstance(c, dict) and c.get("path")]
            out.append(("edit", {"path": paths[0] if paths else "",
                                  "paths": paths, "changes": changes}, ""))
        return out
    # ---- Copilot schema ----
    if t == "tool.execution_start":
        d = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
        pend[d.get("toolCallId")] = {"name": d.get("toolName"),
                                     "args": d.get("arguments", {}) or {}}
        return out
    if t == "tool.execution_complete":
        d = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
        info = pend.pop(d.get("toolCallId"), {"name": "?", "args": {}})
        res = d.get("result") or {}
        c = res.get("content") if isinstance(res, dict) else ""
        out.append((_canon_tool(info["name"]), info.get("args") or {},
                    c if isinstance(c, str) else ""))
        return out
    # ---- Claude Code stream-json schema ----
    if t == "assistant":
        for b in (ev.get("message", {}) or {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                pend[b.get("id")] = {"name": b.get("name"),
                                     "args": b.get("input", {}) or {}}
        return out
    if t == "user":
        for b in (ev.get("message", {}) or {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                info = pend.pop(b.get("tool_use_id"), {"name": "?", "args": {}})
                c = b.get("content")
                if isinstance(c, list):  # content can be a list of text blocks
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append((_canon_tool(info["name"]), info.get("args") or {},
                            c if isinstance(c, str) else ""))
        return out
    return out


def _basename(p):
    return p.rstrip("/").split("/")[-1] if p else (p or "")


def _failure_sig(text):
    """A normalized signature of a failing output: drop numbers/addresses/paths so two
    runs that fail the SAME way hash identically (used to detect 'no progress' — the
    same failure recurring after edits). Leakage-safe: a hash, never the content."""
    if not text:
        return None
    first = None
    for ln in text.splitlines():
        if _ERR_RE.search(ln):
            first = ln
            break
    if first is None:
        return None
    norm = re.sub(r"0x[0-9a-f]+|\d+|/\S+", "", first)
    return hashlib.md5(norm.strip().encode("utf-8", "replace")).hexdigest()[:10]


class SpiralState:
    """Compact, incremental fold of one trajectory's spiral signals. Cheap to update
    per event so it can run live."""
    __slots__ = ("turns", "edit_counts", "edit_order", "error_outputs",
                 "test_runs", "fail_sigs", "repeated_fail_sigs", "recent")

    def __init__(self):
        self.turns = 0
        self.edit_counts = {}        # basename -> #edits
        self.edit_order = []         # ordered basenames of edit targets
        self.error_outputs = 0       # tool outputs carrying an error/failure marker
        self.test_runs = 0           # code-execution verification steps
        self.fail_sigs = {}          # failure signature -> count
        self.repeated_fail_sigs = 0  # # of failing outputs that REPEAT an earlier sig
        self.recent = []             # leakage-safe rolling log of recent actions (for the
                                     # LLM trace-judge); process-only, never fix content

    # ---- derived signals ----
    @property
    def edit_churn(self):
        return max(self.edit_counts.values()) if self.edit_counts else 0

    @property
    def distinct_edit_files(self):
        return len(self.edit_counts)

    @property
    def stagnation(self):
        """Fraction of failing outputs that repeat an earlier identical failure (the
        agent edited but the SAME failure came back = no progress). 0 when no fails."""
        total = sum(self.fail_sigs.values())
        return (self.repeated_fail_sigs / total) if total else 0.0

    def update(self, tool_name, args, result_text):
        """Fold one completed tool call into the state."""
        self.turns += 1
        args = args if isinstance(args, dict) else {}
        tool_name = _canon_tool(tool_name)   # accept Copilot- or Claude-named tools
        action = tool_name or "?"
        if tool_name in _EDIT_TOOLS:
            b = _basename(args.get("path") or args.get("filePath")
                          or args.get("file_path") or "")
            if b:
                self.edit_order.append(b)
                self.edit_counts[b] = self.edit_counts.get(b, 0) + 1
                action = f"edit {b}"
        cmd = args.get("command", "") if tool_name == "bash" else ""
        if cmd:
            short = re.sub(r"\s+", " ", cmd.strip())[:90]
            action = f"$ {short}"
            if _TEST_RUN_RE.search(cmd):
                self.test_runs += 1
        elif tool_name in ("grep", "glob"):
            action = f"{tool_name} {str(args.get('pattern') or '')[:40]}"
        elif tool_name == "view":
            action = f"view {_basename(args.get('path') or args.get('file_path') or '')}"
        fail = None
        if result_text and _ERR_RE.search(result_text):
            self.error_outputs += 1
            sig = _failure_sig(result_text)
            if sig:
                fail = sig
                if sig in self.fail_sigs:
                    self.repeated_fail_sigs += 1
                self.fail_sigs[sig] = self.fail_sigs.get(sig, 0) + 1
        # rolling leakage-safe action log (tool name / command / search / error-SIGNATURE
        # only — no file contents, edit bodies, or view output), capped to recent window.
        tag = f"  [err#{fail}]" if fail else ""
        self.recent.append(f"{self.turns:>3}: {action}{tag}")
        if len(self.recent) > 40:
            self.recent = self.recent[-40:]

    def recent_digest(self, n=24, envelope=None):
        """A compact, LEAKAGE-SAFE digest of the agent's RECENT PROCESS (last n actions)
        for the LLM trace-judge: commands/searches/edited-basenames and error-SIGNATURE
        markers only — never fix content, edit bodies, view output, or the issue text.
        Mirrors reflect.trajectory_summary's leakage stance, windowed to 'now'.
        `envelope` (this repo's typical resolved-task length) is passed as CONTEXT so the
        judge can recognize an abnormally-long trajectory as a likely dead-end."""
        head = (f"turns={self.turns} edit_churn={self.edit_churn} "
                f"distinct_edit_files={self.distinct_edit_files} "
                f"error_outputs={self.error_outputs} test_runs={self.test_runs} "
                f"repeated_identical_failures={self.repeated_fail_sigs} "
                f"stagnation={self.stagnation:.2f}")
        if envelope:
            ratio = self.turns / envelope if envelope else 0
            head += (f"\nrepo_context: tasks here usually RESOLVE within ~{envelope:.0f} "
                     f"turns; this trajectory is at {self.turns} turns = {ratio:.1f}x that "
                     f"typical length. Tests are still NOT green. A trajectory far past "
                     f"the typical resolved length with recurring failures is likely a "
                     f"dead-end loop, even if it touches many files.")
        return head + "\nRecent actions (oldest->newest):\n" + "\n".join(self.recent[-n:])


    def snapshot(self):
        return {"turns": self.turns, "edit_churn": self.edit_churn,
                "distinct_edit_files": self.distinct_edit_files,
                "error_outputs": self.error_outputs, "test_runs": self.test_runs,
                "stagnation": round(self.stagnation, 3),
                "repeated_fail_sigs": self.repeated_fail_sigs}


# ---- detector thresholds -----------------------------------------------------
# The "stuck" signature (calibrated on the measured separation: spirals showed first-
# 40-turn edit_churn ~6 vs ~1.5 normal, distinct_edit_files ~2.5 vs ~1, error_outputs
# ~9 vs ~3). We require a CONJUNCTION (churn AND errors/scatter AND some repetition)
# so productive-but-hard tasks (high activity, but CHANGING failures) are spared.
DEFAULT_THRESHOLDS = {
    "min_turns": 20,          # don't judge stuck before the solve loop has run a while
    "edit_churn": 4,          # >=4 edits to one file
    "distinct_edit_files": 3,  # OR edits scattered across >=3 files
    "error_outputs": 6,       # sustained error density
    "stagnation": 0.20,       # >=20% of failures are repeats of an earlier one
    "envelope_margin": 1.25,  # restart/abort only beyond margin x the resolved-turn pctl
    "envelope_pct": 90,       # use the p90 of resolved turns (NOT max) — robust to a
                              # single very-long resolve poisoning the gate (see
                              # EnvelopeTracker). max-based envelope was disabled by one
                              # 115-turn xarray resolve; p90 ignores the lone outlier.
    "envelope_min_samples": 5,  # need >=5 resolved tasks before trusting the percentile
    "restart_budget": 2,      # max restarts before the terminal abort cap
    "judge_confidence": 0.7,  # LLM trace-judge must be >=this confident to drive an abort
    "backstop_mult": 2.5,     # HARD backstop: beyond this multiple of the envelope, a
                              # stuck trajectory aborts REGARDLESS of the judge's verdict.
                              # Empirically the judge spared xarray-7019/7179 at 5.8x the
                              # envelope (177-204 turns vs envelope 35, stagnation 0.3-0.4)
                              # by misreading multi-file thrashing as "exploration"; the
                              # judge must not be able to veto a screaming outlier.
    # ---- cheap GATE to the judge (Tier-0): permissive "worth a paid look?" trigger.
    # Sensitive on purpose (catch candidates); the judge supplies precision. Far cheaper
    # to evaluate than the judge, and far more permissive than is_stuck (which is the
    # strict heuristic DECISION used only in the no-judge fallback).
    "gate_min_turns": 18,        # don't wake the judge before the loop has had time to form
    "gate_error_outputs": 8,     # OR: a lot of errors even without an exact repeat
}


# ---- v6.1 aggressiveness knob ------------------------------------------------
# A single user-facing LEVEL scales the base thresholds the guard starts from (the per-repo
# EnvelopeTracker then learns ON TOP of this base, flooring learned signal-gates against it).
# Calibrated on the guard-active arms (v41fix/v43-xarray, n=239): the ABORT/backstop path is
# already near-right (only 1 task slipped past 2.5x envelope), but (a) the rung-1 NUDGE was
# effectively DORMANT (1/239) because it shares the abort's bias-to-spare judge gate, and
# (b) a middle band of 28 unresolved tasks ran past typical-resolved length yet under the
# backstop with no intervention. So 'aggressive' mainly WAKES THE NUDGE (lower judge
# confidence, earlier gate, and nudge_on_ambiguous) and tightens that middle band, while
# only MODESTLY pulling the backstop in (2.5x -> 1.8x). 'default' == today's behavior
# EXACTLY (empty override). 'conservative' spares harder (for churny-but-productive repos).
#   nudge_on_ambiguous: when True, a judge-AMBIGUOUS trajectory PAST the envelope fires the
#   resolve-safe NUDGE (never an abort) instead of being spared outright — safe because a
#   nudge only redirects. Only 'aggressive' sets it.
SPIRAL_LEVELS = {
    "conservative": {
        "judge_confidence": 0.85, "envelope_margin": 1.5, "backstop_mult": 3.5,
        "stagnation": 0.30, "gate_min_turns": 24, "min_turns": 28,
        "edit_churn": 5, "error_outputs": 8, "envelope_min_samples": 7,
    },
    "default": {},   # verbatim DEFAULT_THRESHOLDS — reproduces pre-knob behavior exactly
    "aggressive": {
        "judge_confidence": 0.55, "envelope_margin": 1.0, "backstop_mult": 1.8,
        "stagnation": 0.12, "gate_min_turns": 12, "min_turns": 14,
        "edit_churn": 3, "error_outputs": 4, "envelope_min_samples": 3,
        "nudge_on_ambiguous": True,
    },
}


def scaled_thresholds(level="default"):
    """Return DEFAULT_THRESHOLDS overlaid with the named LEVEL's overrides (see
    SPIRAL_LEVELS). Unknown level falls back to 'default' (verbatim defaults). This is the
    BASE the EnvelopeTracker starts from; per-repo learning floors on top of it."""
    thr = dict(DEFAULT_THRESHOLDS)
    thr.update(SPIRAL_LEVELS.get(level, SPIRAL_LEVELS["default"]))
    return thr


def gate_for_judge(state, thr=None):
    """TIER-0 (free, every turn): cheap rule-based trigger deciding whether the recent
    trace is worth ELEVATING to the paid LLM judge. Permissive — it only needs a sign of
    POSSIBLE trouble after the loop has had time to form: a repeated identical failure
    (the cheapest 'no progress' signal) OR a high error density. Productive trajectories
    (changing failures, few repeats) mostly keep this gate CLOSED, so the judge is rarely
    consulted; the judge then makes the precise stuck-vs-productive call. This separation
    is what keeps the judge from being a per-step tax."""
    thr = thr or DEFAULT_THRESHOLDS
    if state.turns < thr["gate_min_turns"]:
        return False
    return (state.repeated_fail_sigs >= 1
            or state.error_outputs >= thr["gate_error_outputs"])


def is_stuck(state, thr=None):
    """Strict heuristic 'this looks like an unproductive loop' classifier — the DECISION
    used only in the no-judge fallback. Must have run a while AND show churn/scatter AND
    repeated identical failures (stagnation): the last term is what separates a doomed
    spiral from a productive hard grind. (When the judge is wired, gate_for_judge is the
    cheap trigger and the judge is the decision; is_stuck is not used.)"""
    thr = thr or DEFAULT_THRESHOLDS
    if state.turns < thr["min_turns"]:
        return False
    churny = (state.edit_churn >= thr["edit_churn"]
              or state.distinct_edit_files >= thr["distinct_edit_files"])
    noisy = state.error_outputs >= thr["error_outputs"]
    repeating = state.stagnation >= thr["stagnation"]
    return churny and noisy and repeating


# ladder rungs, cheapest first
NONE, NUDGE, COMPACT, RESTART, ABORT = "none", "nudge", "compact", "restart", "abort"


def assess_abort(state, envelope=None, judge=None, thr=None):
    """Decide whether to abort, TRACE-FIRST, as an explicit TWO-TIER cascade. Returns
    (abort: bool, reason: str).

    TIER 0 — cheap rule-based GATE (free, every turn): gate_for_judge(). Decides only
        whether the recent trace is WORTH a paid look. Permissive; productive trajectories
        mostly keep it closed, so the judge is rarely consulted (this is what makes the
        judge not a per-step tax).
    TIER 1 — paid LLM JUDGE (only when the gate is open, and the caller additionally
        rate-limits it): reads a leakage-safe digest of the RECENT actions and decides
        stuck-vs-productive. The judge — not the step count — drives the outcome:
          - STUCK, confidence>=judge_confidence -> ABORT, even within the envelope (a
            clearly-doomed loop shouldn't burn tokens up to the envelope first);
          - PRODUCTIVE -> SPARE, even past the envelope (progress shows in the trace);
          - ambiguous -> defer to the envelope fallback.
    FALLBACK — when NO judge is wired (judge=None): the strict is_stuck() heuristic plus
        the per-repo resolved-turn envelope (the Phase-1.5 behavior), so the guard still
        works cheaply without an LLM.
    """
    thr = thr or DEFAULT_THRESHOLDS

    # HARD BACKSTOP (checked BEFORE the judge): an extreme outlier — far past the
    # envelope AND stuck by the strict heuristic — aborts regardless of any judge
    # verdict. The judge cannot veto this; it exists because the LLM judge empirically
    # rationalized 5.8x-envelope thrashing (xarray-7019/7179) as "exploration".
    # `backstop_turns` (learned, cap-aware) is preferred when the tracker supplied it, so
    # the backstop stays REACHABLE under a tight turn cap; else fall back to the
    # multiplier-on-envelope form (the Copilot-scale behavior).
    backstop_turns = thr.get("backstop_turns")
    if backstop_turns is None and envelope is not None:
        backstop_turns = envelope * thr["backstop_mult"]
    if (backstop_turns is not None
            and state.turns >= backstop_turns
            and is_stuck(state, thr)):
        return True, (f"backstop:{state.turns}>={backstop_turns:.0f}"
                      f"turns stagnation={state.stagnation:.2f}")

    if judge is not None:
        # TWO-TIER: gate -> judge. The gate opens on the cheap rule-based trigger OR when
        # the trajectory is already PAST this repo's typical resolved length (envelope) —
        # an over-envelope run is worth a paid judge look even without an exact repeated
        # failure yet, which is what lets the guard engage on a short-turn agent whose
        # spirals hit the cap before accumulating a repeat signature.
        over_envelope = envelope is not None and state.turns >= envelope
        if not (gate_for_judge(state, thr) or over_envelope):
            return False, "gate-closed"
        try:
            v = judge(state.recent_digest(envelope=envelope))   # caller rate-limits
        except Exception:
            v = None
        if not v:
            return False, "judge-throttled"
        conf = float(v.get("confidence", 0) or 0)
        if v.get("stuck") and conf >= thr["judge_confidence"]:
            return True, f"judge-stuck@{conf:.2f}:{str(v.get('reason',''))[:60]}"
        if v.get("stuck") is False:
            return False, "judge-productive"
        # judge AMBIGUOUS (stuck=None): do NOT abort on the (possibly under-calibrated,
        # early-stream) envelope alone — that killed resolvable tasks like xarray-7120 at
        # turn 28 when the p90 envelope was only 27. Ambiguity => SPARE; the hard backstop
        # (2.5x envelope AND is_stuck, checked above) remains the only envelope-driven kill,
        # and it sits well beyond any resolvable task's length.
        return False, "judge-ambiguous-spared"

    # FALLBACK (no judge): strict heuristic + envelope safety gate.
    if not is_stuck(state, thr):
        return False, "not-stuck"
    if envelope is not None and state.turns >= envelope:
        return True, f"envelope:{state.turns}>={envelope:.0f}"
    return False, "stuck-but-within-envelope"


def should_abort(state, envelope, judge=None, thr=None):
    """Backward-compatible boolean wrapper over assess_abort()."""
    ok, _reason = assess_abort(state, envelope=envelope, judge=judge, thr=thr)
    return ok


def decide(state, envelope=None, restarts_done=0, nudged=False,
           autonomous=False, thr=None):
    """STAGE C — choose the cheapest sufficient ladder action.

    `envelope` = margin * (longest RESOLVED task's turn count seen so far) for this
    repo, or None to disable the heavy rungs (then we only ever nudge). `restarts_done`
    and `nudged` track what's already been tried this task so the ladder escalates.

    Returns one of NONE / NUDGE / COMPACT / RESTART / ABORT.
    """
    thr = thr or DEFAULT_THRESHOLDS
    if not is_stuck(state, thr):
        return NONE
    # rung 1: always try the free, resolve-safe nudge first.
    if not nudged:
        return NUDGE
    # beyond the nudge we need the per-repo envelope to authorize heavier action — this
    # is what keeps us off matplotlib's productive long tasks.
    over_envelope = envelope is not None and state.turns >= envelope
    if not over_envelope:
        # still within the band where long==plausibly-productive: only compact (safe).
        return COMPACT
    if restarts_done < thr["restart_budget"]:
        return RESTART
    return ABORT if autonomous else COMPACT


def nudge_text(state):
    """The rung-1 message: mirror the agent's OWN repeated actions back at it. Contains
    nothing but a summary of what it already did — leakage-safe by construction, and
    framed to REDIRECT effort, never to stop it."""
    busiest = max(state.edit_counts.items(), key=lambda kv: kv[1])[0] \
        if state.edit_counts else "one file"
    return (
        "Process check (automated): so far you've made "
        f"{sum(state.edit_counts.values())} edits across "
        f"{state.distinct_edit_files} file(s) (most to `{busiest}`) and run "
        f"{state.test_runs} verification(s), and the same failure has recurred "
        f"{state.repeated_fail_sigs} time(s) without the tests going green. This "
        "looks like a repetitive loop. Do NOT stop — instead step back and try a "
        "MATERIALLY DIFFERENT hypothesis: re-read the failing test's expectation, "
        "consider a different file or root cause than the ones you've been editing, "
        "and verify the new hypothesis directly before editing again.")


def restart_digest(state):
    """Leakage-safe brief carried across a context RESTART: what was already tried
    (counts + the files touched, which are the agent's own actions) and that tests are
    still red. Never includes a fix, a gold pointer, or which lines to change."""
    files = ", ".join(f"{b} ({n} edits)" for b, n in
                      sorted(state.edit_counts.items(), key=lambda kv: -kv[1]))
    return (
        "Resuming this task with a fresh, compact context after a repetitive loop. "
        "What was already attempted (do NOT simply repeat it): edited " + (files or "—")
        + f"; ran {state.test_runs} verifications; the tests are still failing and the "
        f"same failure recurred {state.repeated_fail_sigs} time(s). Form a different "
        "hypothesis about the root cause, confirm it by reading the relevant code and "
        "the failing test's expectation, then make a focused change and verify.")


# ---- rung-1 NUDGE live controller --------------------------------------------
RESUME_HEADER = (
    "\n\n---\nNOTE: you are RESUMING this same task. The repository at /testbed already "
    "contains your OWN in-progress edits from your previous attempt (inspect them with "
    "`git diff`); do not discard that work, build on it. The tests are still not passing.\n")


class NudgeGuard:
    """Live rung-1 controller: run the sealed agent as a sequence of streamed SEGMENTS in
    ONE container, delivering the leakage-safe NUDGE between segments.

    Why segments: the rollout is single-shot `copilot -p`, so a nudge cannot be injected
    into the running context. Instead the guard STOPS the current stream the moment the
    trajectory looks stuck (freezing the container's working tree with the agent's edits
    intact) and the harness RE-INVOKES the agent with a continuation prompt = original task
    + RESUME_HEADER + nudge_text(). The agent resumes on top of its own on-disk edits.

    Ladder realized here (compact/restart remain P2): the FIRST stuck detection NUDGES; a
    subsequent stuck detection (or the cumulative backstop) ABORTS. Each new segment starts
    with a FRESH per-segment SpiralState, so the post-nudge behavior must independently
    re-accumulate churn/errors/stagnation past the gate before it can be judged stuck again
    — that reset IS the nudge's fair grace window (assess_abort's gate needs >=18 turns).
    A cumulative backstop (turns summed across segments) bounds total cost regardless.

    Usage (harness side):
        guard = NudgeGuard(envelope=env, judge=spiral_judge, verbose=True)
        rec, wall = run_agent(..., monitor=guard.monitor,
                              continuation=lambda: guard.continuation(prompt))
        # then read guard.nudges_done / guard.aborted / guard.events for telemetry.
    """

    def __init__(self, envelope=None, judge=None, thr=None, nudge_budget=1,
                 verbose=False, nudge_writer=None, resume_header=None):
        self.envelope = envelope
        self.judge = judge            # raw digest->verdict callable (rate-limited HERE)
        self.thr = thr or DEFAULT_THRESHOLDS
        self.nudge_budget = nudge_budget
        self.verbose = verbose
        # v6.1: optional LLM-targeted nudge writer (digest -> {"text","_tokens"}). When
        # set, continuation() uses it to build a TARGETED redirect from the same leakage-
        # safe digest the judge sees, falling back to the generic nudge_text template on
        # any empty/failed result. None => the fixed count-template (v4.3 behavior).
        self.nudge_writer = nudge_writer
        self.resume_header = resume_header if resume_header is not None else RESUME_HEADER
        self.nudge_writer_tokens = 0  # REAL token cost of paid nudge-writer calls
        # per-segment stuck-detection state (reset by continuation() after each nudge)
        self.st = SpiralState()
        self.pend = {}                # toolCallId -> {name,args} within the segment
        # cross-segment accounting
        self.cum_turns = 0            # turns summed over ALL segments (cumulative backstop)
        self.segment = 0
        self.nudges_done = 0
        self.aborted = False          # terminal abort fired
        self.pending_action = None    # 'nudge' | 'abort' set when a segment should stop
        self.events = []              # telemetry: nudge/abort decisions
        # sampled-judge cadence (mirror run_agent's _sampled_judge: backoff on productive)
        self._judge_last = -999
        self._judge_gap = 5
        self._judge_calls = 0
        self.judge_tokens = 0         # REAL token cost of all paid judge calls (mechanism
                                      # overhead folded into amortized task cost by callers)
        self._JUDGE_BASE_GAP = 5
        self._JUDGE_MAX_GAP = 20

    # ---- the paid judge, rate-limited with backoff (keyed on per-segment turns) -------
    def _sampled_judge(self, digest):
        if self.judge is None:
            return None
        if self.st.turns - self._judge_last < self._judge_gap:
            return None
        self._judge_last = self.st.turns
        self._judge_calls += 1
        try:
            v = self.judge(digest)
        except Exception:
            v = None
        if v:
            self.judge_tokens += v.get("_tokens") or 0
        if v and v.get("stuck") is False:
            self._judge_gap = min(self._JUDGE_MAX_GAP, self._judge_gap + self._JUDGE_BASE_GAP)
        else:
            self._judge_gap = self._JUDGE_BASE_GAP
        if self.verbose and v:
            print(f"  [nudge-judge] seg{self.segment} turn {self.st.turns} "
                  f"(call #{self._judge_calls}, next>={self._judge_gap}): "
                  f"stuck={v.get('stuck')} conf={v.get('confidence')} "
                  f"{str(v.get('reason', ''))[:50]}")
        return v

    # ---- STREAM monitor: fold one JSONL event, return True to STOP this segment -------
    def monitor(self, line):
        # completed_calls handles BOTH the Copilot schema (tool.execution_start/complete)
        # and Claude Code's stream-json (assistant.tool_use / user.tool_result). A single
        # Claude user message can batch several tool_results, so fold+assess EACH in order
        # and stop on the first that trips the gate.
        for name, args, result in completed_calls(line, self.pend):
            self.st.update(name, args, result)
            self.cum_turns += 1

            # cumulative hard backstop across segments (per-segment st resets, so
            # assess_abort's own backstop only sees within-segment turns; this catches a
            # task that keeps spiraling across a nudge without any single segment exceeding
            # the envelope). Prefer the learned cap-aware `backstop_turns` (so it stays
            # reachable under a tight turn cap); else fall back to backstop_mult*envelope.
            backstop_turns = self.thr.get("backstop_turns")
            if backstop_turns is None and self.envelope is not None:
                backstop_turns = self.envelope * self.thr["backstop_mult"]
            if (backstop_turns is not None
                    and self.cum_turns >= backstop_turns
                    and is_stuck(self.st, self.thr)):
                return self._stop("abort",
                                  f"cum-backstop:{self.cum_turns}>={backstop_turns:.0f}t")

            abort, reason = assess_abort(self.st, envelope=self.envelope,
                                         judge=(self._sampled_judge if self.judge else None),
                                         thr=self.thr)
            if not abort:
                # v6.1 aggressive knob: a judge-AMBIGUOUS trajectory PAST the envelope is
                # spared from ABORT by assess_abort (resolve-safety), but under this level we
                # still fire the RESOLVE-SAFE nudge on it (redirect only, never a kill) — this
                # is what wakes the otherwise-dormant nudge on the middle band of long-running
                # unresolved tasks. Never escalates to abort here.
                if (self.thr.get("nudge_on_ambiguous")
                        and reason == "judge-ambiguous-spared"
                        and self.nudges_done < self.nudge_budget):
                    return self._stop("nudge", "ambiguous-past-envelope")
                continue
            # stuck: nudge first (while budget remains), else abort.
            if self.nudges_done < self.nudge_budget:
                return self._stop("nudge", reason)
            return self._stop("abort", reason)
        return False

    def _stop(self, action, reason):
        self.pending_action = action
        rec = {"segment": self.segment, "action": action,
               "turn_in_segment": self.st.turns, "cum_turns": self.cum_turns,
               "envelope": self.envelope, "reason": reason,
               "snapshot": self.st.snapshot()}
        self.events.append(rec)
        if action == "abort":
            self.aborted = True
        if self.verbose:
            print(f"  [nudge-guard] seg{self.segment} {action.upper()} at "
                  f"turn {self.st.turns} (cum {self.cum_turns}) ({reason}) "
                  f"{self.st.snapshot()}")
        return True

    # ---- nudge body: v6.1 LLM-targeted redirect, else the generic template ------------
    def _nudge_body(self):
        """Build the rung-1 nudge text. If an LLM nudge_writer is wired, feed it the SAME
        leakage-safe recent digest the judge sees and use its targeted redirect; fold the
        paid cost into nudge_writer_tokens. Fall back to the generic nudge_text template on
        an absent writer or any empty/failed result (resolve-safe zero-LLM path)."""
        if self.nudge_writer is not None:
            try:
                out = self.nudge_writer(self.st.recent_digest(envelope=self.envelope))
            except Exception:
                out = None
            if out:
                self.nudge_writer_tokens += out.get("_tokens") or 0
                txt = (out.get("text") or "").strip()
                if txt:
                    if self.verbose:
                        print(f"  [nudge-writer] seg{self.segment} targeted redirect "
                              f"({len(txt)} chars, +{out.get('_tokens')} tok)")
                    return txt
        return nudge_text(self.st)

    # ---- CONTINUATION: called after a segment aborts; return next prompt or None ------
    def continuation(self, base_prompt):
        """Return the continuation prompt for the next segment, or None to stop for good.
        Only a 'nudge' stop continues; an 'abort' stop (or a natural end) is terminal."""
        if self.pending_action != "nudge":
            return None
        text = self._nudge_body()
        self.nudges_done += 1
        # fresh per-segment state -> the agent gets a fair, independent re-assessment.
        self.st = SpiralState()
        self.pend = {}
        self.pending_action = None
        self.segment += 1
        self._judge_last = -999
        self._judge_gap = self._JUDGE_BASE_GAP
        return base_prompt + self.resume_header + text


# ---- envelope tracking -------------------------------------------------------
def _percentile(xs, p):
    """Linear-interpolated p-th percentile of xs (0<=p<=100). xs need not be sorted."""
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    if n == 1:
        return float(xs[0])
    i = (p / 100.0) * (n - 1)
    lo = int(i)
    frac = i - lo
    if lo + 1 >= n:
        return float(xs[-1])
    return xs[lo] + frac * (xs[lo + 1] - xs[lo])


class EnvelopeTracker:
    """Per-repo resolved-turn envelope AND runtime-learned detection thresholds.

    Envelope: margin * the p<pct> of turn-counts among RESOLVED tasks seen so far. Using a
    PERCENTILE (default p90), not the max, makes the gate robust to a single very-long
    resolve — empirically one 115-turn xarray resolve set a max-based envelope to ~144 and
    disabled the guard for every 40-60-turn spiral below it; p90 ignores that lone outlier
    (envelope ~32) while staying HIGH on repos like matplotlib where long resolves are
    COMMON (so productive-long tasks stay protected). Stays None until >=min_samples
    resolved tasks exist (don't trust a percentile of a handful of points), so we never
    abort on a repo we know little about yet.

    Thresholds: `thresholds()` derives the FULL detector threshold dict from the SAME
    resolved-task history, so the guard adapts to the agent/repo's actual turn scale
    instead of the hardcoded Copilot-scale DEFAULT_THRESHOLDS (which never fired on Claude
    Code's <=80-turn runs — its backstop landed above the turn cap). Turn-scale gates
    scale DOWN to the learned distribution; signal gates keep DEFAULT as a FLOOR so the
    learned dict is never MORE trigger-happy than the hand-tuned one (resolve-safety). Fed
    only by RESOLVED tasks (same causal rule as the envelope), so a task's own outcome can
    only gate FUTURE tasks, never itself."""
    def __init__(self, margin=None, pct=None, min_samples=None, level="default",
                 frozen_envelope=None):
        # v6.1: `level` selects the aggressiveness preset (SPIRAL_LEVELS). It sets the BASE
        # thresholds this tracker starts from AND the envelope margin/percentile/min-samples;
        # explicit margin/pct/min_samples args still win over the preset when given.
        self.level = level
        self.base_thr = scaled_thresholds(level)
        self.margin = margin or self.base_thr["envelope_margin"]
        self.pct = pct if pct is not None else self.base_thr["envelope_pct"]
        self.min_samples = (min_samples if min_samples is not None
                            else self.base_thr["envelope_min_samples"])
        # FROZEN envelope (pre-registered hyperparameter): when set, `envelope` returns this
        # value from task 1, bypassing the >=min_samples warm-up. For SHORT streams (Web-Bench
        # = 20 tasks, ~5 resolved at ~25% Pass@1) the percentile never reaches min_samples, so
        # a within-stream-learned envelope would stay None the whole run and the guard would
        # never engage. A frozen scalar (calibrated once from SWE-bench/held-out, IDENTICAL
        # across all streams -- a fixed hyperparameter, NOT a prior pooled across the streams)
        # lets the guard engage while preserving stream independence. None => today's behavior.
        self.frozen_envelope = frozen_envelope
        self.resolved_turns = []   # all RESOLVED tasks' turn counts (for the percentile)
        self.resolved_snaps = []   # per-resolved-task SpiralState.snapshot() dicts (signals)

    def update(self, turns, resolved, snapshot=None):
        """Record a completed task. Only RESOLVED tasks feed the baseline. `snapshot` is an
        optional SpiralState.snapshot() dict (edit_churn/distinct_edit_files/error_outputs/
        stagnation/...) captured from that task's trajectory; when omitted only the
        turn-envelope learns (back-compat with callers that don't reconstruct the state)."""
        if resolved and turns:
            self.resolved_turns.append(turns)
            if snapshot:
                self.resolved_snaps.append(snapshot)

    @property
    def envelope(self):
        if len(self.resolved_turns) < self.min_samples:
            # cold start: fall back to the frozen pre-registered envelope if one was set
            # (short-stream case); else None == "don't trust a handful of points yet".
            return self.frozen_envelope
        return _percentile(self.resolved_turns, self.pct) * self.margin

    def thresholds(self, max_turns=None):
        """Return the detector threshold dict to hand to is_stuck/gate_for_judge/
        assess_abort. Starts from the LEVEL-scaled base (scaled_thresholds(self.level); ==
        DEFAULT_THRESHOLDS for level 'default') and, once >=min_samples RESOLVED tasks
        exist, overrides the learned keys from this stream's resolved-task distribution.
        Before warm-up (or with no data) it returns the base verbatim, so behavior is
        unchanged until the stream has learned enough — exactly like the envelope gating
        None today. Learned signal-gates floor against the SCALED base, so an 'aggressive'
        level genuinely lowers those floors (and 'conservative' raises them). Pass
        `max_turns` (the run's turn cap) to make the hard backstop REACHABLE under the cap."""
        base = self.base_thr
        thr = dict(base)
        env = self.envelope
        # cap-aware backstop: reachable even under a tight turn cap. The Copilot-scale
        # `backstop_mult * envelope` landed ABOVE Claude's 80-turn cap (~110 turns) so it
        # never fired; clamp it under 0.9*cap so an extreme outlier still aborts in-cap.
        if env is not None:
            bt = env * thr["backstop_mult"]
            if max_turns:
                bt = min(bt, 0.9 * max_turns)
            thr["backstop_turns"] = bt
        if len(self.resolved_turns) < self.min_samples:
            return thr   # cold start: level-scaled base (+ backstop_turns if env exists)

        # --- turn-scale gates: relative to the learned resolved-turn distribution. On a
        # short-turn agent (Claude p90~35) this drops the judge gate from 18 -> ~10 so the
        # guard can engage before the cap; on a long-turn agent (Copilot p90~150+) the
        # clamp keeps it at the base ceiling (no change). ---
        p90t = _percentile(self.resolved_turns, 90) or 0

        def _clamp(v, lo, hi):
            return int(max(lo, min(hi, round(v))))
        thr["gate_min_turns"] = _clamp(0.6 * p90t, 6, base["gate_min_turns"])
        thr["min_turns"] = _clamp(0.6 * p90t, 8, base["min_turns"])

        # --- signal gates: treat RESOLVED runs as the "normal" band and sit just ABOVE its
        # p90, but never BELOW the (level-scaled) base floor (so the learned dict is never
        # more trigger-happy than the level asks — protects resolve-safety on churny-but-
        # productive repos, where this RAISES the bar). Needs the per-task snapshots. ---
        if self.resolved_snaps:
            def _sig(key):
                return [s.get(key, 0) or 0 for s in self.resolved_snaps]
            import math
            thr["edit_churn"] = max(base["edit_churn"],
                                    math.ceil((_percentile(_sig("edit_churn"), 90) or 0) + 1))
            thr["distinct_edit_files"] = max(
                base["distinct_edit_files"],
                math.ceil((_percentile(_sig("distinct_edit_files"), 90) or 0) + 1))
            thr["error_outputs"] = max(
                base["error_outputs"],
                math.ceil((_percentile(_sig("error_outputs"), 90) or 0) + 1))
            thr["gate_error_outputs"] = max(base["gate_error_outputs"],
                                            thr["error_outputs"])
            thr["stagnation"] = max(base["stagnation"],
                                    round(_percentile(_sig("stagnation"), 90) or 0, 3))
        return thr


# ---- offline / shadow-mode helper --------------------------------------------
def state_from_trajectory(path, load_trajectory=None):
    """Build a final SpiralState by replaying a saved transcript.jsonl. Reuses
    reflect.load_trajectory by default (import lazily to avoid a hard dependency when
    spiral.py is used standalone). Returns a SpiralState (use .snapshot() / decide()).
    """
    if load_trajectory is None:
        from . import reflect
        load_trajectory = reflect.load_trajectory
    st = SpiralState()
    for name, args, res, _ok in load_trajectory(path):
        st.update(name, args, res)
    return st
