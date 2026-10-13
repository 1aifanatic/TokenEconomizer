"""`te` -- inspect and steer what TokenEconomizer does in your sessions.

    te status                       what it has learned for this repository, and what it did
    te facts                        the manual it injects, and the facts still forming
    te untool show                  the proposed deny list, and the evidence behind it
    te untool apply [ITEM ...]      write the proposal (or the named items) to the repository's
                                    settings: tools (Claude Code), capabilities (Codex) or
                                    MCP servers (Copilot)
    te untool restore [ITEM ...]    remove entries this plugin added
    te untool refresh-catalog       record the tool list a fresh session loads (Claude, Copilot)
    te untool measure               measure the request with and without the proposal
    te replay [--limit N] [--commit]  run the mechanisms over your existing session history
    te config show                  each mechanism on or off, its settings, the guard's thresholds
    te config set KEY=VALUE ...     e.g. `spiral=off`, `inject=on`, `spiral.min_turns=12`
                                    (this repository; `--global` for every repository)
    te config unset KEY ...         back to the next source down (the global file, then default)
    te doctor                       check the installation
    te --host copilot probe         real Copilot sessions in a throwaway repository: does a
                                    learned fact change what the model does, and what does the
                                    deny list save (makes model calls; see te_live/probe.py)

`--cwd` picks the repository (default: the current directory) and `--host` the agent
(claude | codex | copilot | pi; default: `TE_HOST`, else the agent whose shell runs `te`, else
claude). `replay` is the way to see what the mechanisms would do on
your own sessions before turning any of them on: it folds your history into a scratch state and
replays the spiral guard over every prompt-turn, with no LLM call. `--commit` keeps the result as
the repository's starting state, so learning does not begin from zero.
"""
import argparse
import glob
import json
import os
import re
import sys
import tempfile
import time

from . import accounting, fold, guard, hosts, untool
from .config import unset_config, write_config
from .context import LiveContext
from .paths import state_root
from .store import Store


def _ctx(args, env=None):
    host = hosts.HOSTS[args.host]()
    return LiveContext.build(host, args.cwd, env)


def _fmt_tokens(n):
    if n is None:
        return "n/a"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


# ---- status / facts ------------------------------------------------------------------------
def cmd_status(args):
    ctx = _ctx(args)
    cfg = ctx.config
    out = {"repo": ctx.repo.label, "repo_root": ctx.repo.root, "host": ctx.host.name,
           "state": ctx.store.base, "config": cfg.as_dict(), "warnings": cfg.warnings}
    inj = ctx.store.read("injection.json") or {}
    unt = ctx.store.read("untool.json") or {}
    env_tracker = guard.load_envelope(ctx.store, cfg.spiral_aggr, cfg.spiral_thresholds)
    ledger = accounting.summarize(ctx.store.read_lines("ledger.jsonl"))
    out.update({
        "injection": {"profile": cfg.inject_effective,
                      "injected_lines": sum(1 for ln in (inj.get("rendered") or "").splitlines()
                                            if ln.startswith("- ")),
                      "sessions_folded": inj.get("sessions", 0)},
        "untool": {"mode": cfg.untool, "sessions": unt.get("sessions", 0),
                   "catalog_source": unt.get("catalog_source"),
                   "proposal": unt.get("proposal", []),
                   "applied": untool.applied(ctx.store)},
        "spiral": {"mode": cfg.spiral, "envelope": env_tracker.envelope,
                   "ended_green_turns": len(env_tracker.resolved_turns)},
        "ledger": ledger})
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"TokenEconomizer -- {out['repo']} ({ctx.host.name})")
    print(f"  state      {out['state']}")
    print(f"  config     inject={cfg.inject_effective} untool={cfg.untool} spiral={cfg.spiral} "
          f"mode={cfg.mode} paid={cfg.paid} shadow={cfg.shadow}")
    for w in cfg.warnings:
        print(f"  warning    {w}")
    print(f"  sessions   {ledger['sessions']} folded, {_fmt_tokens(ledger['effective_tokens'])} "
          f"effective tokens, plugin overhead {_fmt_tokens(ledger['overhead_tokens'])} "
          f"({ledger['overhead_frac']:.1%})")
    i = out["injection"]
    print(f"  injection  {i['injected_lines']} fact(s) injected at session start "
          f"({i['sessions_folded']} sessions learned from)")
    u = out["untool"]
    if not untool.supports(ctx.host.name):
        print("  untooling  not available on this host")
    else:
        src = u["catalog_source"] or "none yet"
        print(f"  untooling  {u['sessions']} sessions observed, catalog: {src}; "
              f"proposed {untool.unit(ctx.host.name, plural=True)}: "
              f"{', '.join(u['proposal']) or '-'}; applied: {', '.join(u['applied']) or '-'}")
    sp = out["spiral"]
    env_s = f"{sp['envelope']:.0f} tool calls" if sp["envelope"] else "not learned yet"
    print(f"  spiral     envelope {env_s} ({sp['ended_green_turns']} ended-green turns); "
          f"{ledger['nudges']} nudge(s), {ledger['aborts']} stop(s) over {ledger['turns']} turns")
    return 0


def cmd_facts(args):
    ctx = _ctx(args)
    inj = ctx.store.read("injection.json") or {}
    text = inj.get("rendered") or ""
    print(text.rstrip() or "(nothing injected yet: no fact has cleared its firing gate)")
    facts = inj.get("facts") or {}
    rows = [(k, v) for k, v in facts.items() if isinstance(v, dict) and "state" in v]
    if rows:
        print("\nfacts tracked:")
        for k, v in sorted(rows):
            print(f"  {v.get('state', '?'):9s} fire={v.get('fire', '?'):>5s} "
                  f"{'INJECTED' if v.get('inject') else '        '} {k}")
    return 0


# ---- untool --------------------------------------------------------------------------------
def _untool_show(ctx, unt):
    host = ctx.host.name
    print(f"sessions observed: {unt.get('sessions', 0)}  catalog: "
          f"{unt.get('catalog_source') or 'none yet'}")
    print(f"applied ({untool.unit(host, plural=True)} this plugin added to "
          f"{untool.settings_path(ctx.repo.root, host)}): "
          f"{', '.join(untool.applied(ctx.store)) or '-'}")
    print(f"proposed: {', '.join(unt.get('proposal', [])) or '-'}")
    if host == "codex":
        caps = unt.get("capabilities") or {}
        if caps:
            print("capabilities (removable from a project's .codex/config.toml):")
            for c, e in sorted(caps.items()):
                print(f"  {c:14s} loaded in {e['loaded_sessions']:3d} sessions, unused in "
                      f"{e['unused_sessions']:3d}{'  USED (kept)' if e['used'] else ''}")
        trusted = untool.codex_project_trusted(ctx.repo.root)
        if trusted is False:
            print("note: Codex reads a project's .codex/config.toml only in a TRUSTED project; "
                  "trust this one in Codex first, or an applied proposal does nothing.")
        return 0
    if host == "copilot":
        servers = unt.get("servers") or {}
        if servers:
            print("MCP servers (Copilot can only remove a whole server):")
            for name, e in sorted(servers.items()):
                state = "USED (kept)" if e["used"] else f"unused in {e['unused_sessions']} sessions"
                print(f"  {name:24s} {e['tools']:3d} tools, {state}")
    streaks = unt.get("streaks") or {}
    if streaks and host == "claude":
        print("unused-session streaks (tools never used are candidates once over "
              f"{ctx.config.untool_evidence}):")
        for t, n in sorted(streaks.items(), key=lambda kv: -kv[1])[:30]:
            used = "used" if t in set(unt.get("used_tools", [])) else ""
            print(f"  {n:4d}  {t} {used}")
    if not unt.get("catalog_source") or unt.get("catalog_source") == "observed":
        print(f"note: without `te --host {host} untool refresh-catalog` only "
              + ("deferred and observed" if host == "claude" else "observed")
              + " tools are known, which leaves the always-loaded tools out of the evidence.")
    return 0


def cmd_untool(args):
    ctx = _ctx(args)
    host = ctx.host.name
    if not untool.supports(host):
        print(f"untooling is not available on {host} (it declares only a handful of tools)")
        return 1
    unt = ctx.store.read("untool.json") or {}
    action = args.action
    if action == "show":
        return _untool_show(ctx, unt)
    if action == "apply":
        tools = args.tools or unt.get("proposal", [])
        if not tools:
            print("nothing to apply")
            return 0
        if host == "codex":
            bad = [t for t in tools if t not in untool.CODEX_TOGGLES]
            if bad:
                print(f"not a removable Codex capability: {', '.join(bad)} (choose from "
                      f"{', '.join(untool.CODEX_LIVE_CAPABILITIES)})")
                return 2
        try:
            added = untool.apply(ctx.repo.root, tools, ctx.store, host=host)
        except RuntimeError as e:
            print(f"not applied: {e}")
            return 1
        print(f"added: {', '.join(added) or '(already present)'}\n"
              f"in {untool.settings_path(ctx.repo.root, host)} -- takes effect from the next "
              f"session.")
        if host == "copilot" and added:
            if untool.copilot_folder_trusted(ctx.repo.root) is False:
                print("warning: Copilot reads a repository's .github/copilot settings only in a "
                      "trusted folder, and this one is not. Trust it (Copilot asks when you "
                      "start it here), or the change does nothing.")
            left = untool.verify_copilot(ctx.repo.root, untool.applied(ctx.store))
            if left is not None:
                print("checked with a local capture: " + ", ".join(
                    f"{k} {'removed' if not v else f'still loaded ({v} tools)'}"
                    for k, v in sorted(left.items())))
        if host == "codex" and added:
            if untool.codex_project_trusted(ctx.repo.root) is False:
                print("warning: this project is not trusted in Codex, which ignores a "
                      "project's .codex/config.toml until it is. Trust it (Codex asks when "
                      "you start it here), or the change does nothing.")
            state = untool.verify_codex(ctx.repo.root, untool.applied(ctx.store))
            if state:
                print("codex features list: " + ", ".join(
                    f"{k}={'on' if v else 'off'}" for k, v in sorted(state.items())))
        return 0
    if action == "restore":
        try:
            removed = untool.restore(ctx.repo.root, ctx.store, args.tools or None, host=host)
        except RuntimeError as e:
            print(f"not restored: {e}")
            return 1
        print(f"removed: {', '.join(removed) or '-'} (from the next session)")
        return 0
    if action == "refresh-catalog":
        if host == "codex":
            print("Codex needs no catalog: its removable capabilities are a fixed set "
                  f"({', '.join(untool.CODEX_LIVE_CAPABILITIES)}), read from each session.")
            return 0
        path, tools = untool.refresh_catalog(state_root(), host=host)
        print(f"{len(tools)} tools recorded in {path}:\n  " + ", ".join(tools))
        return 0
    if action == "measure":
        tools = args.tools or unt.get("proposal", []) or untool.applied(ctx.store)
        if not tools:
            print("no proposal to measure")
            return 1
        res = untool.measure(tools, host=host)
        print(json.dumps(res, indent=2))
        return 0
    return 2


# ---- config / doctor / fold ----------------------------------------------------------------
def _config_view(cfg):
    """The settings as a person reads them: each mechanism on or off, its knobs, and the guard's
    thresholds with the numbers a `spiral.<name>` setting would replace."""
    d = cfg.as_dict()
    pinned = d.pop("spiral_thresholds")
    return {
        "enabled": d.pop("enabled"),
        "facts (inject)": {"state": "off" if cfg.inject == "off" else "on",
                           "inject": d.pop("inject"),
                           "inject_max_facts": d.pop("inject_max_facts"),
                           "inject_max_tokens": d.pop("inject_max_tokens"),
                           "propose_every": d.pop("propose_every")},
        "unused tools (untool)": {"state": "off" if cfg.untool == "off" else "on",
                                  "untool": d.pop("untool"),
                                  "untool_evidence": d.pop("untool_evidence")},
        "stuck-turn guard (spiral)": {
            "state": "off" if cfg.spiral == "off" else "on",
            "spiral": d.pop("spiral"), "spiral_aggr": d.pop("spiral_aggr"),
            "spiral_nudges": d.pop("spiral_nudges"),
            "thresholds": guard.base_thresholds(cfg.spiral_aggr, pinned),
            "pinned": sorted(pinned)},
        "other": d,
        "warnings": cfg.warnings,
    }


def cmd_config(args):
    ctx = _ctx(args)
    if args.action == "show":
        print(json.dumps(_config_view(ctx.config), indent=2))
        return 0
    path = (os.path.join(ctx.root, "config.json") if args.globally
            else os.path.join(os.path.dirname(ctx.store.base), "config.json"))
    if args.action == "unset":
        try:
            cur = unset_config(path, [k.strip() for k in args.pairs])
        except ValueError as e:
            print(f"rejected: {e}")
            return 2
        print(f"{path}: {json.dumps(cur, sort_keys=True)}")
        return 0
    updates = {}
    for kv in args.pairs:
        if "=" not in kv:
            print(f"expected KEY=VALUE, got {kv!r}")
            return 2
        k, v = kv.split("=", 1)
        updates[k.strip()] = v.strip()
    if not updates:
        print("nothing to set: give KEY=VALUE (see `te config show` for the keys)")
        return 2
    try:
        cur = write_config(path, updates)
    except ValueError as e:
        print(f"rejected: {e}")
        return 2
    print(f"{path}: {json.dumps(cur, sort_keys=True)}")
    print("applies from the next hook call; a new fact-size cap applies once the next session "
          "has been folded")
    return 0


def cmd_fold(args):
    ctx = _ctx(args)
    sids = args.session or fold.pending_sessions(ctx)
    for sid in sids:
        rec = fold.fold_session(ctx, sid)
        if rec is not None and args.verbose:
            print(json.dumps(rec, default=str))
    return 0


def cmd_judge(args):
    """The detached half of the paid stuck judge (see te_live/judge.py); not for people."""
    from . import judge
    judge.run_job(_ctx(args), args.session, args.job)
    return 0


def cmd_probe(args):
    if args.host != "copilot":
        print("the probe runs on Copilot only so far: `te --host copilot probe`")
        return 2
    from . import probe
    return probe.run(sessions=args.sessions, model=args.model, keep=args.keep)


def cmd_doctor(args):
    ok = sys.version_info >= (3, 10)
    print(f"python      {sys.version.split()[0]} {'ok' if ok else '(needs >= 3.10)'}")
    root = state_root()
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".write-test")
        open(probe, "w").close()
        os.remove(probe)
        print(f"state root  {root} (writable)")
    except OSError as e:
        print(f"state root  {root} NOT writable: {e}")
        ok = False
    for host in ("claude", "copilot"):
        cat = os.path.join(root, "hosts", host, "catalog.json")
        print(f"{host:11s} catalog {'present' if os.path.exists(cat) else 'not probed'}")
    from urllib.parse import urlparse
    from .sidecall import copilot_model
    base = os.environ.get("COPILOT_PROVIDER_BASE_URL")
    route = (f"BYOK {os.environ.get('COPILOT_PROVIDER_TYPE') or 'openai'} at "
             f"{urlparse(base).netloc or base}" if base else "Copilot subscription")
    print(f"copilot     {route}; side calls and `probe` use model "
          f"{copilot_model() or '(Copilot default)'}")
    ctrust = untool.copilot_folder_trusted(args.cwd)
    if ctrust is not None:
        print(f"copilot     this folder is {'trusted' if ctrust else 'NOT trusted'} (an untrusted "
              f"folder's .github/copilot settings are ignored)")
    trusted = untool.codex_project_trusted(args.cwd)
    if trusted is not None:
        print(f"codex       this project is {'trusted' if trusted else 'NOT trusted'} "
              f"(an untrusted project's .codex/config.toml is ignored)")
    log = os.path.join(root, "errors.log")
    if os.path.exists(log):
        tail = open(log, encoding="utf-8", errors="replace").read().splitlines()[-6:]
        print("errors.log (tail):\n  " + "\n  ".join(tail))
    else:
        print("errors.log  none")
    return 0 if ok else 1


# ---- replay --------------------------------------------------------------------------------
def _claude_projects_dir():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def _within(path, root):
    path, root = os.path.realpath(path or ""), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def _first_cwd(path, limit=200):
    for i, line in enumerate(open(path, encoding="utf-8", errors="replace")):
        if i > limit:
            break
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d.get("cwd"), str):
            return d["cwd"]
        p = d.get("payload")
        if isinstance(p, dict) and isinstance(p.get("cwd"), str):
            return p["cwd"]
    return None


def find_history(ctx, limit=0):
    """[(session_id, transcript)] of this repository's past sessions, oldest first."""
    found = []
    if ctx.host.name == "claude":
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(ctx.repo.root))
        for d in glob.glob(os.path.join(_claude_projects_dir(), "*")):
            if not os.path.basename(d).startswith(slug):
                continue
            for f in glob.glob(os.path.join(d, "*.jsonl")):
                if _within(_first_cwd(f), ctx.repo.root):
                    found.append((os.path.splitext(os.path.basename(f))[0], f))
    elif ctx.host.name == "pi":
        from .transcripts import pi_session_header
        agent = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(
            os.path.expanduser("~"), ".pi", "agent")
        for f in glob.glob(os.path.join(agent, "sessions", "*", "*.jsonl")):
            head = pi_session_header(f)
            if head.get("id") and _within(head.get("cwd"), ctx.repo.root):
                found.append((head["id"], f))
    elif ctx.host.name == "copilot":
        from .transcripts import copilot_session_meta
        for f in glob.glob(os.path.join(hosts.copilot_home(), "session-state", "*",
                                        "events.jsonl")):
            meta = copilot_session_meta(f)
            if meta.get("session_id") and _within(meta.get("cwd"), ctx.repo.root):
                found.append((meta["session_id"], f))
    else:
        home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
        for f in glob.glob(os.path.join(home, "sessions", "**", "rollout-*.jsonl"),
                           recursive=True):
            if _within(_first_cwd(f), ctx.repo.root):
                found.append((os.path.basename(f)[len("rollout-"):-len(".jsonl")], f))
    found.sort(key=lambda sf: os.path.getmtime(sf[1]))
    return found[-limit:] if limit else found


def _claude_turns(path):
    """Prompt-turns of a Claude Code transcript: [(calls, effective_tokens)], where calls are
    (name, args, result, exit). Usage is counted once per assistant message id, as the ledger
    counts it (the transcript repeats a message's usage on every content block)."""
    from tokeneconomizer.vendor import spiral
    turns, cur, pend, usage = [], None, {}, {}

    def close():
        if cur is not None and (cur or usage):
            eff = sum(accounting.effective(u)[0] or 0 for u in usage.values())
            turns.append((cur, eff))

    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "user" and not d.get("isMeta") and not d.get("isSidechain"):
            c = (d.get("message") or {}).get("content")
            kinds = ({b.get("type") for b in c if isinstance(b, dict)}
                     if isinstance(c, list) else set())
            if (isinstance(c, str) and c.strip() and not c.startswith("<")) or (
                    "text" in kinds and "tool_result" not in kinds):
                close()
                cur, pend, usage = [], {}, {}
                continue
        if cur is None:
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        if d.get("type") == "assistant" and isinstance(msg.get("usage"), dict):
            usage[msg.get("id") or len(usage)] = dict(msg["usage"], model=msg.get("model"))
        for name, args, result in spiral.completed_calls(line, pend):
            cur.append((name, args, result, None))
    close()
    return turns


def _codex_turns(path):
    from .transcripts import _rollout_item_to_exec
    turns, order = {}, []
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if ev.get("type") != "event_msg" or p.get("type") != "item_completed":
            continue
        item = _rollout_item_to_exec(p.get("item") or {}) or {}
        tid = p.get("turn_id") or "?"
        if tid not in turns:
            turns[tid] = []
            order.append(tid)
        if item.get("type") == "command_execution":
            turns[tid].append(("bash", {"command": item.get("command") or ""},
                               item.get("aggregated_output") or "", item.get("exit_code")))
        elif item.get("type") == "file_change":
            paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
            turns[tid].append(("apply_patch", {"path": paths[0] if paths else ""}, "", None))
    return [(turns[t], None) for t in order if turns[t]]


def _session_turns(host, path):
    """Prompt-turns of one session log, whichever host wrote it."""
    if host.name == "claude":
        return _claude_turns(path)
    if host.name == "copilot":
        from .transcripts import copilot_turns
        return [(calls, None) for calls in copilot_turns(path)]
    if host.name == "pi":
        from .transcripts import pi_session_to_claude
        with tempfile.TemporaryDirectory(prefix="te-pi-") as d:
            out = os.path.join(d, "trace.jsonl")
            return _claude_turns(out) if pi_session_to_claude(path, out) is not None else []
    return _codex_turns(path)


def cmd_replay(args):
    real = _ctx(args)
    history = find_history(real, args.limit)
    if not history:
        print(f"no {real.host.name} session history found for {real.repo.root}")
        return 1
    scratch = None
    ctx = real
    if not args.commit:
        scratch = tempfile.mkdtemp(prefix="te-replay-")
        ctx = LiveContext(host=real.host, repo=real.repo, root=real.root,
                          store=Store(os.path.join(scratch, real.host.name)),
                          config=real.config, env=dict(real.env, TE_FOLD_SYNC="1"))
    t0 = time.time()
    totals = {"turns": 0, "calls": 0, "nudge_turns": 0, "stop_turns": 0, "post_fire_calls": 0}
    turn_cost, turn_tokens = [], []
    for i, (sid, path) in enumerate(history):
        fold.fold_session(ctx, sid, transcript_path=path)
        turns = _session_turns(real.host, path)
        for calls, eff in turns:
            if eff is not None:
                turn_tokens.append(eff)
            if not calls:
                continue
            g = guard.TurnGuard(turn_key=f"{sid}:{totals['turns']}")
            ov = real.config.spiral_thresholds
            tracker = guard.load_envelope(ctx.store, real.config.spiral_aggr, ov)
            env_v, thr = tracker.envelope, guard.thresholds(tracker, ov)
            fired_at = None
            for j, (name, a, r, code) in enumerate(calls):
                d = g.fold(name, a, r, code, envelope=env_v, thr=thr,
                           nudge_budget=real.config.spiral_nudges)
                if d and d[0] == "nudge":
                    g.after_nudge()
                    totals["nudge_turns"] += 1 if fired_at is None else 0
                    fired_at = j if fired_at is None else fired_at
                elif d and d[0] == "abort":
                    totals["stop_turns"] += 1
                    fired_at = j if fired_at is None else fired_at
            if fired_at is not None:
                totals["post_fire_calls"] += len(calls) - fired_at - 1
            totals["turns"] += 1
            totals["calls"] += len(calls)
            turn_cost.append(len(calls))
            if g.cum_turns:
                guard.record_turn(ctx.store, g, level=real.config.spiral_aggr,
                                  overrides=real.config.spiral_thresholds)
        if args.verbose:
            print(f"  [{i + 1}/{len(history)}] {sid} {len(turns)} turns")
    led = accounting.summarize(ctx.store.read_lines("ledger.jsonl"))
    inj = ctx.store.read("injection.json") or {}
    unt = ctx.store.read("untool.json") or {}
    tracker = guard.load_envelope(ctx.store, real.config.spiral_aggr,
                                  real.config.spiral_thresholds)
    facts = {k: v for k, v in (inj.get("facts") or {}).items() if isinstance(v, dict)}
    injected = [k for k, v in facts.items() if v.get("inject")]
    forming = [k for k, v in facts.items() if v.get("state") in ("CANDIDATE", "ACTIVE")
               and not v.get("inject")]
    span = (time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(history[0][1]))),
            time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(history[-1][1]))))
    print(f"TokenEconomizer replay -- {real.repo.label} ({real.host.name}), "
          f"{len(history)} sessions, {span[0]} -> {span[1]} ({time.time() - t0:.0f}s)")
    tail = ""
    if turn_tokens and sum(turn_tokens):
        top = sorted(turn_tokens, reverse=True)[:max(1, len(turn_tokens) // 10)]
        tail = (f"; the costliest 10% of prompt-turns carry "
                f"{sum(top) / sum(turn_tokens):.0%} of it")
    print(f"  cost        {_fmt_tokens(led['effective_tokens'])} effective tokens over "
          f"{led['priced_sessions']} priced sessions{tail}")
    print(f"  injection   {len(injected)} fact(s) would be injected now"
          + (f": {', '.join(injected)}" if injected else "")
          + (f"; {len(forming)} forming: {', '.join(forming[:5])}" if forming else ""))
    if untool.supports(real.host.name):
        print(f"  untooling   catalog {unt.get('catalog_source') or 'none'}; would propose "
              f"{untool.unit(real.host.name, plural=True)}: "
              f"{', '.join(unt.get('proposal') or []) or '-'}")
    turns = max(totals["turns"], 1)
    print(f"  spiral      {totals['turns']} prompt-turns (median "
          f"{sorted(turn_cost)[len(turn_cost) // 2] if turn_cost else 0} tool calls); guard "
          f"would nudge {totals['nudge_turns']} ({totals['nudge_turns'] / turns:.1%}) and stop "
          f"{totals['stop_turns']}; {totals['post_fire_calls']} tool calls came after a fire "
          f"({totals['post_fire_calls'] / max(totals['calls'], 1):.1%} of all calls)")
    env_s = f"{tracker.envelope:.0f}" if tracker.envelope else "n/a"
    print(f"              learned envelope {env_s} tool calls from "
          f"{len(tracker.resolved_turns)} ended-green turns (heuristic gate; no judge)")
    if scratch:
        print(f"  (scratch state in {scratch}; `te replay --commit` keeps it as the starting "
              f"state)")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="te", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--host", default=hosts.default_host(), choices=sorted(hosts.HOSTS))
    # The same two options are accepted after the subcommand too (`te fold --host codex ...`).
    # SUPPRESS keeps a subcommand that omits them from overwriting the top-level values.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", default=argparse.SUPPRESS)
    common.add_argument("--host", default=argparse.SUPPRESS, choices=sorted(hosts.HOSTS))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name):
        return sub.add_parser(name, parents=[common])

    p = add("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    add("facts").set_defaults(fn=cmd_facts)
    p = add("untool")
    p.add_argument("action", nargs="?", default="show",
                   choices=("show", "apply", "restore", "refresh-catalog", "measure"))
    p.add_argument("tools", nargs="*")
    p.set_defaults(fn=cmd_untool)
    p = add("replay")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--commit", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_replay)
    p = add("config")
    p.add_argument("action", nargs="?", default="show", choices=("show", "set", "unset"))
    p.add_argument("pairs", nargs="*")
    p.add_argument("--global", dest="globally", action="store_true")
    p.set_defaults(fn=cmd_config)
    p = add("fold")
    p.add_argument("--session", action="append")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_fold)
    p = add("judge")
    p.add_argument("--session", required=True)
    p.add_argument("--job", required=True)
    p.set_defaults(fn=cmd_judge)
    add("doctor").set_defaults(fn=cmd_doctor)
    p = add("probe")
    p.add_argument("--sessions", type=int, default=6)
    p.add_argument("--model", default=None)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(fn=cmd_probe)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    finally:
        from .paths import secure_root
        secure_root()


if __name__ == "__main__":
    sys.exit(main())
