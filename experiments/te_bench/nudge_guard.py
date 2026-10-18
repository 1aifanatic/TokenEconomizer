"""`NudgeGuard`: the loop guard as the benchmark harness ran it, over a streamed rollout.

The rollout is single-shot (`claude -p`, `copilot -p`), so a redirect cannot be injected into the
running context. The guard folds each streamed line, STOPS the stream the moment the trajectory
looks stuck (the container keeps the agent's edits), and the harness re-invokes the agent with
a continuation prompt: the original task + `RESUME_HEADER` + the redirect. The plugin's
`tokeneconomizer.loopguard.TurnGuard` is the same control flow, one hook call at a time.
"""
from tokeneconomizer.loopguard.rules import DEFAULT_THRESHOLDS, assess_abort, is_stuck, nudge_text
from tokeneconomizer.loopguard.state import SpiralState, completed_calls

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
