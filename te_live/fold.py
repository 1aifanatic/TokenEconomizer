"""Folding a finished session into what the plugin knows about its repository.

This is the live counterpart of the experiments' `Economizer.after_task`, and it runs the same
classes: `PrefixStage` learns and renders the injected manual (envfacts, plus the paid proposer
when allowed), and `ToolingStage` accrues evidence untooling's used-tool and unused-streak state.
A session is one "task" of the repository's stream for both, the unit they were built around.

Two adaptations, both forced by the setting:

  * The stage objects lived for a whole stream in one process; here each fold is a fresh
    process. `PrefixStage` already persists its distillers; `ToolingStage`'s streaks were
    in-memory only, so they are saved to `untool.json` and restored before each fold.
  * Folding runs OFF the agent's critical path. SessionEnd hooks are short (Codex caps them at
    three seconds), and parsing a long transcript -- or a paid proposer tick -- takes longer, so
    the hook spawns `te fold` detached and returns at once. A session whose end was never seen
    (a killed terminal) is folded by the next SessionStart once its transcript has gone quiet.

Each session is folded once, at its first end, keyed by session id; a resumed session is not
re-folded, because counting the same trajectory twice would hand the fact lifecycle confirmations
it did not earn.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import accounting, sidecall, transcripts, untool
from . import adapter as live_adapter
from .adapter import LiveAdapter, load_catalog

STALE_S = 600           # an unfolded session whose transcript is quiet this long is over
# A fold never runs in a hook -- it is a detached process, or `te fold` -- so it can wait for
# another fold to finish, up to one paid side call, and must never proceed without the lock:
# two folds of one session would both find it unfolded and count it twice, in every mechanism's
# state and in the ledger. That happened whenever a session started while the previous one's
# paid fold was still in its side call (the next SessionStart re-folds whatever is not yet
# marked folded). A fold that still cannot take the lock skips: the holder finishes the
# session, or, if it died, the session stays pending and the next SessionStart folds it.
FOLD_LOCK_WAIT_S = sidecall.TIMEOUT_S + 60


def _run_dir(workdir, trace_path):
    run = os.path.join(workdir, "run")
    os.makedirs(run, exist_ok=True)
    link = os.path.join(run, "transcript.jsonl")
    try:
        os.symlink(trace_path, link)
    except OSError:
        shutil.copyfile(trace_path, link)
    return run


def _fold_injection(ctx, adapter, run_dir, session_id):
    profile = ctx.config.inject_effective
    if profile == "off":
        return None
    from tokeneconomizer.config import InjectionConfig
    from tokeneconomizer.mechanisms.injection import PrefixStage
    icfg = InjectionConfig(enabled=True, propose_every=ctx.config.propose_every, profile=profile,
                           max_facts=ctx.config.inject_max_facts,
                           max_tokens=ctx.config.inject_max_tokens)
    runner = sidecall.runner_for(ctx.host.name, ctx.config) if icfg.paid else None
    stage = PrefixStage(icfg, ctx.repo.label, ctx.mech_dir, False, runner=runner,
                        optimizer_model="agent", adapter=adapter, agent=ctx.host.name)
    srow = {"resolved": None}
    opt_tokens, reason = stage.after_task({"instance_id": session_id}, {}, run_dir, srow)
    rendered = stage.render() or ""
    prior = ctx.store.read("injection.json") or {}
    ctx.store.write("injection.json", {
        "profile": profile, "reason": reason, "rendered": rendered,
        "facts": srow.get("manual_facts"), "updated": time.time(),
        "sessions": int(prior.get("sessions", 0)) + 1})
    return {"optimizer_tokens": float(opt_tokens or 0), "rendered_chars": len(rendered)}


def _fold_untool(ctx, adapter, run_dir, session_id, transcript=None, workdir=None):
    if not untool.supports(ctx.host.name) or ctx.config.untool == "off":
        return None
    if ctx.host.name == "codex":
        return _fold_untool_codex(ctx, adapter, session_id, transcript, workdir)
    host = ctx.host.name
    from tokeneconomizer.config import ToolingConfig
    from tokeneconomizer.mechanisms.tooling import ToolingStage
    st = ctx.store.read("untool.json") or {}
    stage = ToolingStage(ToolingConfig(evidence=ctx.config.untool_evidence), host, "agent",
                         adapter=adapter, repo=ctx.repo.label, results_root=ctx.mech_dir,
                         fresh=True)
    stage.used_tools = set(st.get("used_tools", []))
    stage.tool_unused_streaks = {k: int(v) for k, v in (st.get("streaks") or {}).items()}
    stage.catalog_tools = set(st.get("catalog_tools", []))
    stage.catalog_desc = dict(st.get("catalog_desc") or {})
    applied = untool.applied(ctx.store)
    stage._exclude_tools = list(applied)        # the mask this session actually ran under
    # A tool that has since become protected (the web tools, from this version on) comes back:
    # remove the plugin's OWN deny rule for it. Only rules it recorded adding, never one the
    # person wrote, and only in the direction of giving a tool back.
    protected = adapter.protected_tools()
    restored = []
    if any(t in protected for t in applied) and not ctx.config.shadow:
        try:
            restored = untool.restore(ctx.repo.root, ctx.store,
                                      [t for t in applied if t in protected], host=host)
        except RuntimeError:            # an unreadable or redirected settings file: leave it
            restored = []
    if restored:
        applied = untool.applied(ctx.store)
    # The stage warns once per STREAM when no catalog parses -- a harness defect signal there.
    # Here a stage lives for one fold, and a session that called no tool legitimately has no
    # observed catalog; the source (or its absence) is recorded in untool.json instead.
    stage._warned_no_catalog = True
    srow = {}
    stage.after_task({"instance_id": session_id}, {"untooled_excluded": applied}, run_dir, srow)
    sessions = int(st.get("sessions", 0)) + 1
    raw = stage.build_tools({"instance_id": f"after-{session_id}"}, sessions) or []
    doc = {"used_tools": sorted(stage.used_tools), "streaks": stage.tool_unused_streaks,
           "catalog_tools": sorted(stage.catalog_tools), "catalog_desc": stage.catalog_desc,
           # A session run under an applied mask parses no catalog (the stage skips it, as the
           # masked list is not the full one); the last source still describes the catalog.
           "catalog_source": srow.get("untool_catalog_source") or st.get("catalog_source"),
           "sessions": sessions, "updated": time.time()}
    if host == "copilot":
        # Copilot's only persistent lever is a whole MCP server (`untool` module docstring).
        servers = transcripts.copilot_mcp_servers(ctx.env)
        proposal = [s for s in untool.copilot_server_proposal(
            raw, stage.catalog_tools, stage.used_tools, servers) if s not in applied]
        doc["servers"] = _server_summary(stage, servers)
    else:
        proposal = [t for t in untool.filter_proposal(raw, stage.tool_unused_streaks,
                                                      ctx.config.untool_evidence)
                    if t not in applied]
    doc["proposal"] = proposal
    doc["new_in_proposal"] = sorted(set(proposal) - set(st.get("proposal", [])))
    added = []
    if ctx.config.untool == "auto" and proposal and not ctx.config.shadow:
        try:
            added = untool.apply(ctx.repo.root, proposal, ctx.store, host=host)
            doc["proposal"], doc["auto_applied"] = [], added
        except RuntimeError as e:          # an unreadable or redirected file: keep proposing
            doc["auto_apply_refused"] = str(e)
    if restored:
        doc["restored_protected"] = restored
    ctx.store.write("untool.json", doc)
    return {"proposal": doc["proposal"], "applied_now": added, "restored": restored}


def _server_summary(stage, servers):
    """{server: {tools, used, unused_sessions}} -- the Copilot evidence at the unit it acts on."""
    out = {}
    for t in stage.catalog_tools:
        s = transcripts.mcp_server_of(t, servers)
        if not s:
            continue
        e = out.setdefault(s, {"tools": 0, "used": False, "unused_sessions": None})
        e["tools"] += 1
        e["used"] = e["used"] or t in stage.used_tools
        n = int(stage.tool_unused_streaks.get(t, 0))
        e["unused_sessions"] = n if e["unused_sessions"] is None else min(e["unused_sessions"], n)
    return out


def _fold_untool_codex(ctx, adapter, session_id, transcript, workdir):
    """Codex: the experiments' capability controller (`codex_untool.CodexUntoolProfile`, driven
    through `ToolingStage`) decides which packages went unused, over a trace that carries the
    model's own `exec` calls (`transcripts.codex_capability_trace`). Two live adaptations:

      * the session ran under the capabilities the PERSON applied, not the profile's own mask
        (a plugin launches nothing), so the observation is recorded against the applied set;
      * a package is proposed only once the profile has demoted it AND it went unused in
        `untool_evidence` sessions (x3 for image/media, the high-risk package), with no use
        ever -- the profile alone demotes collaboration after one unused task, which was right
        for a sealed benchmark stream and is too quick for a person's sessions."""
    from tokeneconomizer.config import ToolingConfig
    from tokeneconomizer.mechanisms.tooling import ToolingStage
    if not transcript or not workdir:
        return None
    cap_dir = os.path.join(workdir, "capabilities")
    os.makedirs(cap_dir, exist_ok=True)
    cap_trace = os.path.join(cap_dir, "trace.jsonl")
    if transcripts.codex_capability_trace(transcript, cap_trace) is None:
        return None
    run_dir = _run_dir(cap_dir, cap_trace)
    stage = ToolingStage(ToolingConfig(codex_untool=True), "codex", "agent", adapter=adapter,
                         repo=ctx.repo.label, results_root=ctx.mech_dir, fresh=False)
    stage._warned_no_catalog = True
    stage.build_tools({"instance_id": session_id}, 0)
    applied = untool.applied(ctx.store)
    stage.codex_profile._pending["logical_capabilities"] = sorted(applied)
    srow = {}
    stage.after_task({"instance_id": session_id}, {}, run_dir, srow)
    state = stage.codex_profile.state
    ever_used = set(state.get("ever_completed") or ())
    loaded = state.get("loaded_exposures") or {}
    proposal = [c for c in untool.CODEX_LIVE_CAPABILITIES
                if c in (state.get("current_pruned") or ()) and c not in ever_used
                and c not in applied
                and int(loaded.get(c, 0)) >= ctx.config.untool_evidence * (
                    live_adapter.HIGH_RISK_FACTOR if c in CODEX_HIGH_RISK else 1)]
    prior = ctx.store.read("untool.json") or {}
    doc = {"sessions": int(state.get("tasks_completed") or 0), "catalog_source": "capabilities",
           "capabilities": {c: {"loaded_sessions": int(loaded.get(c, 0)),
                                "unused_sessions": int((state.get("zero_attempt_exposures")
                                                        or {}).get(c, 0)),
                                "used": c in ever_used}
                            for c in untool.CODEX_LIVE_CAPABILITIES},
           "proposal": proposal, "updated": time.time(),
           "new_in_proposal": sorted(set(proposal) - set(prior.get("proposal", [])))}
    added = []
    if ctx.config.untool == "auto" and proposal and not ctx.config.shadow:
        try:
            added = untool.apply(ctx.repo.root, proposal, ctx.store, host="codex")
            doc["proposal"], doc["auto_applied"] = [], added
        except RuntimeError as e:          # a committed or unparseable file: keep proposing
            doc["auto_apply_refused"] = str(e)
    ctx.store.write("untool.json", doc)
    return {"proposal": doc["proposal"], "applied_now": added, "restored": []}


# Rarely used, but expensive to lose when a session needs it -- the Codex counterpart of
# adapter.HIGH_RISK (NotebookEdit on Claude Code).
CODEX_HIGH_RISK = frozenset({"image_media"})


def fold_session(ctx, session_id, transcript_path=None):
    """Fold one finished session. Returns the ledger record, or None if already folded."""
    sess_name = os.path.join("sessions", f"{session_id}.json")
    with ctx.store.locked("fold", timeout=FOLD_LOCK_WAIT_S) as got:
        if not got:
            return None
        folded = ctx.store.read("folded.json") or {}
        if session_id in folded:
            return None
        sess = ctx.store.read(sess_name) or {}
        transcript = transcript_path or sess.get("transcript")
        record = {"kind": "session", "session_id": session_id, "host": ctx.host.name,
                  "repo": ctx.repo.label, "folded_at": time.time(),
                  "started": sess.get("started"), "ended": sess.get("ended"),
                  "shadow": ctx.config.shadow}
        workdir = tempfile.mkdtemp(prefix="te-fold-")
        try:
            trace_path = ctx.host.mechanism_trace(transcript, workdir) if transcript else None
            if not trace_path:
                record["error"] = "transcript unreadable"
            else:
                run_dir = _run_dir(workdir, trace_path)
                adapter = LiveAdapter(ctx.host, ctx.repo.root, vocabulary=ctx.config.vocabulary,
                                      catalog=load_catalog(ctx.root, ctx.host.name))
                with ctx.store.locked("mechanisms"):
                    inj = _fold_injection(ctx, adapter, run_dir, session_id)
                    unt = _fold_untool(ctx, adapter, run_dir, session_id,
                                       transcript=transcript, workdir=workdir)
                record["injection"] = inj
                record["untool"] = unt
                ctx.host.settle(transcript)
                usage = ctx.host.session_usage(transcript)
                eff, priced_by = accounting.effective(usage)
                record.update({k: usage.get(k) for k in (
                    "model", "requests", "input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens")})
                record["effective_tokens"], record["priced_by"] = eff, priced_by
            turns = sess.get("turns") or []
            record.update({
                "turns": len(turns),
                "nudges": int(sess.get("nudges", 0)), "aborts": int(sess.get("aborts", 0)),
                "injected_chars": int(sess.get("injected_chars", 0)),
                "untool_applied": untool.applied(ctx.store),
                "overhead": {
                    "optimizer_tokens": ((record.get("injection") or {}).get("optimizer_tokens")
                                         or 0),
                    "spiral_judge_tokens": float(sess.get("judge_tokens", 0) or 0),
                    "spiral_nudge_writer_tokens": float(sess.get("writer_tokens", 0) or 0)}})
            ctx.store.append("ledger.jsonl", record)
            folded[session_id] = {"at": time.time(), "transcript": transcript}
            ctx.store.write("folded.json", folded)
            if sess:
                sess["folded"] = True
                ctx.store.write(sess_name, sess)
            return record
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def pending_sessions(ctx, exclude=(), stale_s=STALE_S, now=None):
    """Recorded sessions that ended (or went quiet) without being folded."""
    now = now or time.time()
    folded = ctx.store.read("folded.json") or {}
    out = []
    sess_dir = ctx.store.path("sessions")
    if not os.path.isdir(sess_dir):
        return out
    for name in sorted(os.listdir(sess_dir)):
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
        if sid in folded or sid in exclude:
            continue
        sess = ctx.store.read(os.path.join("sessions", name)) or {}
        tp = sess.get("transcript") or ""
        try:
            quiet = now - os.path.getmtime(tp) >= stale_s
        except OSError:
            quiet = False
        if sess.get("ended") or quiet:
            out.append(sid)
    return out


def spawn_fold(ctx, session_ids):
    """Fold sessions in a detached process (inline when TE_FOLD_SYNC=1, for tests)."""
    if not session_ids:
        return None
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return [fold_session(ctx, sid) for sid in session_ids]
    # The directory holding both packages: the checkout, or the bundle's lib/.
    core = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(ctx.env)
    env["PYTHONPATH"] = core + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, "-m", "te_live.cli", "fold",
           "--host", ctx.host.name, "--cwd", ctx.repo.root] + [
               a for sid in session_ids for a in ("--session", sid)]
    ctx.store.ensure()
    log = open(ctx.store.path("fold.log"), "a")
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         cwd=ctx.repo.root, env=env, start_new_session=True)
    finally:
        log.close()
    return True
