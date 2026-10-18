"""`LiveController`: what the plugin does at each hook.

    SessionStart      inject the repository's micro-skills (cached by the last fold), and
                      fold any earlier session that ended without its SessionEnd being seen;
                      with `repomap` on, refresh a stale map in the background
    UserPromptSubmit  open a prompt-turn for the loop guard; on a session's first prompt, add
                      the repository map (`repomap=on`)
    PostToolUse       fold the call into the turn's guard; nudge, or stop, when it says so
    Stop              close the turn; an ended-green turn feeds the repository's envelope
    SessionEnd        hand the session to a detached `te fold`

Every handler returns quickly and fails open. A hook that raises would break the user's session
for a cost optimization, so `hook.main` turns any exception into an empty output, and nothing
here waits on the network: injection reads a cached render, and the only paid call a hook can
cause -- the sampled stuck judge -- runs only when `paid` is on and the free gate is open, and
then in a detached job (`judge`), never in the hook itself.

Every visible action is announced in one `systemMessage` line, so a person can see why the agent
changed course; `shadow` computes and logs every decision without acting on any of them.
"""
import hashlib
import os
import time

from .. import loopguard
from . import denylist, fold, repomap
from .hosts import merge_outputs
from .paths import ensure_launcher

TURN_HISTORY = 200


class LiveController:
    def __init__(self, ctx, payload):
        self.ctx = ctx
        self.p = payload or {}
        self.host = ctx.host
        self.store = ctx.store
        self.cfg = ctx.config
        self.sid = self.host.session_id(self.p)

    # ---- plumbing -----------------------------------------------------------------------
    def _sess_name(self):
        return os.path.join("sessions", f"{self.sid}.json")

    def _session(self):
        s = self.store.read(self._sess_name())
        if not isinstance(s, dict):
            s = {"session_id": self.sid, "host": self.host.name, "started": time.time(),
                 "turns": [], "nudges": 0, "aborts": 0, "judge_tokens": 0.0,
                 "writer_tokens": 0.0}
        return s

    def _save(self, s):
        tp = self.host.transcript(self.p)
        if tp:
            s["transcript"] = tp
        s["updated"] = time.time()
        self.store.write(self._sess_name(), s)

    def _cmd(self, name):
        """How a person runs one of the plugin's commands on this host."""
        if self.host.name in ("claude", "copilot"):    # Copilot's TUI runs plugin commands too
            return f"/tokeneconomizer:{name}"
        if self.host.name == "pi":
            return f"/te {name}"                 # registered by the Pi extension
        return f"te --host {self.host.name} {name}"

    def _log(self, kind, **fields):
        self.store.append("events.jsonl", dict(
            fields, kind=kind, at=time.time(), session_id=self.sid, shadow=self.cfg.shadow))

    def handle(self):
        # A payload without a session id is not a hook call any host makes; recording it would
        # leave an "unknown" session behind.
        if not self.cfg.enabled or not self.host.session_id(self.p):
            return {}
        event = self.p.get("hook_event_name") or ""
        # PostToolUseFailure: Copilot reports a failed tool there, not in PostToolUse.
        handler = {"SessionStart": self.session_start, "UserPromptSubmit": self.user_prompt,
                   "PostToolUse": self.post_tool, "PostToolUseFailure": self.post_tool,
                   "Stop": self.stop, "SessionEnd": self.session_end}.get(event)
        return (handler() or {}) if handler else {}

    # ---- SessionStart -----------------------------------------------------------------
    def session_start(self):
        source = self.p.get("source") or "startup"
        try:
            ensure_launcher(self.ctx.env)
        except OSError:
            pass
        s = self._session()
        s["source"] = source
        s["cwd"] = self.host.cwd(self.p, self.ctx.env)
        outputs = []
        seen = self.store.read("notices.json") or {}

        if self.cfg.microskills != "off":
            inj = self.store.read("injection.json") or {}
            text = inj.get("rendered") or ""
            # A resumed or forked conversation already carries what the first start injected;
            # a cleared or compacted one has lost it. ("new" is Copilot's word for "startup".)
            if text and source in ("startup", "new", "clear", "compact"):
                n_facts = sum(1 for ln in text.splitlines() if ln.startswith("- "))
                self._log("inject", chars=len(text), facts=n_facts, source=source)
                if not self.cfg.shadow:
                    outputs.append(self.host.context("SessionStart", text))
                    s["injected_chars"] = len(text)
                    if "rendered_ids" in inj:     # which facts, for this session's fold
                        s["injected_ids"] = list(inj["rendered_ids"])
                    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
                    if seen.get("injection") != digest:
                        outputs.append(self.host.notice(
                            f"TokenEconomizer: added {n_facts} learned fact(s) about "
                            f"{self.ctx.repo.label} to this session's context "
                            f"(review with `{self._cmd('microskills')}`)."))
                        seen["injection"] = digest

        if (self.cfg.toolprune == "propose" and denylist.supports(self.host.name)
                and not self.cfg.shadow):
            prop = (self.store.read("untool.json") or {}).get("proposal") or []
            key = ",".join(prop)
            if prop and seen.get("untool") != key:
                outputs.append(self.host.notice(
                    f"TokenEconomizer: {len(prop)} {denylist.unit(self.host.name)}(s) went unused "
                    f"in this repository's recent sessions ({', '.join(prop[:4])}"
                    f"{'...' if len(prop) > 4 else ''}). Review with "
                    f"`{self._cmd('toolprune')}`; nothing changes until you apply it."))
                seen["untool"] = key

        if self.cfg.repomap != "off":
            if repomap.stale(self.ctx):
                repomap.spawn_refresh(self.ctx)
            at_start = self.cfg.repomap == "start" or not self.host.prompt_context
            if at_start and source in ("startup", "new", "clear", "compact"):
                outputs.extend(self._inject_map(s, seen, repomap.cached_block(self.ctx),
                                                "SessionStart", "cached"))
                s["repomap_done"] = True

        if outputs:
            self.store.write("notices.json", seen)
        self._save(s)
        fold.spawn_fold(self.ctx, fold.pending_sessions(self.ctx, exclude=(self.sid,)))
        return merge_outputs(*outputs)

    def _inject_map(self, s, seen, block, event, how):
        """The outputs that put the repository map in context (and say so, once per repository)."""
        self._log("repomap", chars=len(block), how=how)
        if not block or self.cfg.shadow:
            return []
        s["repomap_chars"] = len(block)
        out = [self.host.context(event, block)]
        if not seen.get("repomap"):
            out.append(self.host.notice(
                f"TokenEconomizer: added a map of {self.ctx.repo.label}'s key files and symbols "
                f"to this session's context (~{len(block) // 4} tokens; `repomap=off` to stop)."))
            seen["repomap"] = True
        return out

    # ---- prompt-turns -------------------------------------------------------------------
    def _close_turn(self, s, stopped_normally):
        data = s.get("turn")
        if not data:
            return None
        g = loopguard.TurnGuard(data=data)
        summary = g.summary(stopped_normally)
        if g.cum_turns and stopped_normally:
            loopguard.record_turn(self.store, g, level=self.cfg.loopguard_aggr,
                              overrides=self.cfg.loopguard_thresholds)
        s["turns"] = (s.get("turns") or [])[-(TURN_HISTORY - 1):] + [summary]
        s["judge_tokens"] = float(s.get("judge_tokens", 0) or 0) + g.judge_tokens
        s["writer_tokens"] = float(s.get("writer_tokens", 0) or 0) + g.writer_tokens
        s["turn"] = None
        return summary

    def user_prompt(self):
        if self.cfg.loopguard == "off" and not (self.cfg.repomap == "on"
                                             and self.host.prompt_context):
            return {}
        s = self._session()
        outputs = []
        if self.cfg.repomap == "on" and self.host.prompt_context and not s.get("repomap_done"):
            # Once per session, on its first prompt: a map ranked toward what the prompt names.
            s["repomap_done"] = True
            seen = self.store.read("notices.json") or {}
            block, how = repomap.for_prompt(self.ctx, self.p.get("prompt") or "")
            outputs = self._inject_map(s, seen, block, "UserPromptSubmit", how)
            if outputs:
                self.store.write("notices.json", seen)
        if self.cfg.loopguard != "off":
            # A turn still open here never reached Stop -- the person interrupted it. Its
            # outcome is unknown, so it is recorded but does not feed the envelope.
            self._close_turn(s, stopped_normally=False)
            s["turn"] = loopguard.TurnGuard(turn_key=self.host.turn_key(self.p)).to_dict()
        self._save(s)
        return merge_outputs(*outputs) if outputs else {}

    def stop(self):
        if self.cfg.loopguard == "off" or self.p.get("stop_hook_active"):
            return {}
        s = self._session()
        summary = self._close_turn(s, stopped_normally=True)
        if summary:
            self._log("turn", **summary)
        self._save(s)
        return {}

    # ---- PostToolUse ---------------------------------------------------------------------
    def _judge(self, thr):
        """The paid stuck judge, run in the background (`judge.AsyncJudge`), or None: the
        hook only starts a job, and a later call picks up its verdict with the redirect already
        written, so the agent never waits on it."""
        if not self.cfg.paid:
            return None
        from .judge import AsyncJudge
        return AsyncJudge(self.ctx, self.sid, thr.get("judge_confidence"))

    def post_tool(self):
        if self.cfg.loopguard == "off" or self.host.is_subagent(self.p):
            return {}
        s = self._session()
        key = self.host.turn_key(self.p)
        data = s.get("turn")
        if data and key and data.get("turn_key") and data["turn_key"] != key:
            self._close_turn(s, stopped_normally=False)     # a turn we never saw end
            data = None
        g = loopguard.TurnGuard(data=data) if data else loopguard.TurnGuard(turn_key=key)
        name, args, text, code = self.host.tool_call(self.p)
        tracker = loopguard.load_envelope(self.store, level=self.cfg.loopguard_aggr,
                                      overrides=self.cfg.loopguard_thresholds)
        envelope = tracker.envelope
        thr = loopguard.thresholds(tracker, self.cfg.loopguard_thresholds)
        decision = g.fold(name, args, text, code, envelope=envelope, thr=thr,
                          judge=self._judge(thr), nudge_budget=self.cfg.loopguard_nudges)
        out = {}
        if decision:
            action, reason = decision
            short = reason.split(":")[0]
            if action == "nudge":
                body = g.nudge_body(envelope)
                g.after_nudge()
                s["nudges"] = int(s.get("nudges", 0)) + 1
                self._log("nudge", reason=reason, tool_calls=g.cum_turns, envelope=envelope)
                if not self.cfg.shadow:
                    out = merge_outputs(
                        self.host.context("PostToolUse", body),
                        self.host.notice(f"TokenEconomizer nudged this turn ({short}): the "
                                         f"same failure kept recurring."))
            elif action == "abort" and not g.abort_delivered:
                g.abort_delivered = True
                s["aborts"] = int(s.get("aborts", 0)) + 1
                can_stop = (self.cfg.loopguard == "stop" and self.ctx.headless
                            and self.host.can_stop_mid_turn)
                self._log("abort", reason=reason, tool_calls=g.cum_turns, envelope=envelope,
                          delivered="stop" if can_stop else "notice")
                if not self.cfg.shadow:
                    if can_stop:
                        out = self.host.stop(
                            f"TokenEconomizer stopped this turn: it kept repeating the same "
                            f"failing steps after a redirect ({short}).")
                    else:
                        out = self.host.notice(
                            f"TokenEconomizer: this turn still looks stuck after a redirect "
                            f"({short}, {g.cum_turns} tool calls). Consider stepping in.")
        s["turn"] = g.to_dict()
        self._save(s)
        return out

    # ---- SessionEnd ---------------------------------------------------------------------
    def session_end(self):
        s = self._session()
        self._close_turn(s, stopped_normally=False)
        s["ended"] = time.time()
        s["end_reason"] = self.p.get("reason")
        self._save(s)
        fold.spawn_fold(self.ctx, [self.sid])
        return {}
