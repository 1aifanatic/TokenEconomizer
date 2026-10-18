"""Spiral mechanism: kill or nudge a stuck rollout in-flight, learning a per-repo
resolved-turn envelope so the intervention is causal and resolve-safe.

Two paths, chosen by agent:

  * GENERIC spiral guard (Claude / Copilot) -- `SpiralConfig.guard`:
      Streams each rollout; a cheap LLM-free gate, an optional sampled LLM trace-judge
      (`spiral_judge`), and a per-repo p90 resolved-turn envelope decide the cheapest
      sufficient action. Rung 1 NUDGES (re-invokes in the same container with a
      leakage-safe redirect built from the agent's own repeated actions; with
      `nudge_llm`, an LLM writes the redirect); a second stuck detection ABORTS. The
      envelope + learned thresholds advance AFTER each task (this task's outcome only
      gates FUTURE tasks). Judge / nudge-writer tokens fold into the honest ledger.

  * CODEX-NATIVE post-green finalize (Codex) -- `SpiralConfig.codex_spiral`:
      Uses Codex's deterministic signals (exit codes, thread ids, per-file change events)
      to fire ONE same-thread finalize nudge once a task's own success check has passed
      UNFILTERED and every ever-failed scope has been re-greened, past the learned
      envelope. Never terminally aborts. Policy = codex_spiral.SHIPPED_POLICY at the
      repo's task-frozen threshold (a `CodexEnvelopeTracker`).

The Stage supplies each path's params to the runner via `run_kwargs`; the runner owns the
actual in-flight stream/kill/resume. `after_task` records telemetry and advances the
envelope unconditionally once the task has run.
"""
import os

from tokeneconomizer.loopguard import EnvelopeTracker, llm_nudge_writer, llm_stuck_judge
from tokeneconomizer.stage import Stage
from . import codex_spiral as cs

_CODEX_AGENTS = ("codex",)


class SpiralStage(Stage):
    name = "spiral"

    def __init__(self, cfg, max_turns, runner=None, optimizer_model="agent",
                 adapter=None, agent=None):
        self.cfg = cfg
        self.max_turns = max_turns
        self.adapter = adapter
        self.agent = agent

        # ---- generic guard: per-repo envelope + host-side LLM closures ----
        self.envelope = EnvelopeTracker(
            level=cfg.aggr, frozen_envelope=getattr(cfg, "frozen_envelope", None))
        self.spiral_judge = None
        self.spiral_nudge_writer = None
        if cfg.guard and cfg.judge:
            self.spiral_judge = (
                lambda digest, _r=runner, _m=optimizer_model:
                llm_stuck_judge(digest, _r, model=_m))
        if cfg.nudge and cfg.nudge_llm:
            self.spiral_nudge_writer = (
                lambda digest, _r=runner, _m=optimizer_model:
                llm_nudge_writer(digest, _r, model=_m))

        # ---- Codex-native post-green finalize: per-repo threshold tracker ----
        self.codex_tracker = None
        if cfg.codex_spiral and agent in _CODEX_AGENTS:
            self.codex_tracker = cs.CodexEnvelopeTracker()

    def run_kwargs(self, task):
        out = {}
        if self.cfg.guard:
            out.update({
                "spiral_guard": self.cfg.guard,
                "abort_envelope": self.envelope.envelope,
                "spiral_thr": self.envelope.thresholds(max_turns=self.max_turns),
                "spiral_judge": self.spiral_judge,
                "spiral_nudge": self.cfg.nudge,
                "spiral_nudge_writer": self.spiral_nudge_writer,
            })
        if self.codex_tracker is not None:
            threshold = self.codex_tracker.threshold
            out["codex_spiral_policy"] = (
                cs.profile_policy(self.cfg.codex_spiral_profile, threshold)
                if threshold is not None else None)
        return out

    def after_task(self, task, row, run_dir_path, srow):
        cfg = self.cfg
        transcript = os.path.join(run_dir_path, "transcript.jsonl")

        # ---- generic guard telemetry + envelope advance (causal) ----
        if cfg.guard:
            srow["aborted"] = bool(row.get("aborted"))
            srow["spiral_judge_tokens"] = row.get("spiral_judge_tokens") or 0
            if row.get("spiral_abort"):
                srow["spiral_abort"] = row["spiral_abort"]
            if cfg.nudge:
                srow["spiral_nudges"] = row.get("spiral_nudges", 0)
                if row.get("spiral_events"):
                    srow["spiral_events"] = row["spiral_events"]
                if cfg.nudge_llm:
                    srow["spiral_nudge_writer_tokens"] = (
                        row.get("spiral_nudge_writer_tokens") or 0)
            snap = None
            if row.get("resolved") and self.adapter is not None:
                try:
                    snap = self.adapter.spiral_snapshot(transcript)
                except Exception:
                    snap = None
            self.envelope.update(row.get("num_turns") or 0,
                                 bool(row.get("resolved")), snapshot=snap)

        # ---- Codex-native post-green telemetry + envelope advance ----
        if self.codex_tracker is not None:
            srow["codex_spiral_active"] = True
            srow["codex_spiral_profile"] = cs.profile_version(self.cfg.codex_spiral_profile)
            srow["codex_spiral_nudges"] = row.get("codex_spiral_nudges", 0)
            if row.get("codex_spiral_events"):
                srow["codex_spiral_events"] = row["codex_spiral_events"]
            srow["spiral_nudge_writer_tokens"] = (
                row.get("codex_spiral_paid_judge_tokens") or 0)
            # Advance the envelope in the unit the TRIGGER uses: the guard's own
            # command+edit count, not the row's `num_turns` (agent steps = messages + tool
            # calls, ~1.45x larger). Feeding steps raised the gate ~45% and left the
            # controller unable to fire on most tasks; `CodexEnvelopeTracker` documents the
            # tool-event unit and now actually receives it. Fall back to `num_turns` only
            # when a row predates the field, and say so through the telemetry rather than
            # silently mixing units.
            tool_turns = row.get("codex_spiral_tool_turns")
            srow["codex_spiral_envelope_unit"] = (
                "tool_events" if tool_turns is not None else "steps_fallback")
            srow["codex_spiral_tool_turns"] = tool_turns
            srow["codex_spiral_threshold"] = self.codex_tracker.threshold
            self.codex_tracker.update(
                int(tool_turns if tool_turns is not None else (row.get("num_turns") or 0)),
                bool(row.get("resolved")))
        return 0
