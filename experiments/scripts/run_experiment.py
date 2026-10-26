#!/usr/bin/env python3
"""Reproducible driver for the TokenEconomizer main experiments.

One invocation runs ONE arm over ONE repo stream and writes the same results layout the
aggregator + figures consume (``<results-root>/<arm>/<repo>_s<seed>/<run>/row.json`` plus a
per-repo ``_streams/*.jsonl``). It is the single source of truth mapping each *proven* arm
to an exact ``EconomizerConfig`` -- the `ARMS` table below reproduces the flag sets the
original launches used:

    baseline        (no mechanisms)                                -> native cost basis
    baseline-low    baseline at TE_EFFORT_LEVEL=low     (arm-owned, see Arm.env)
    baseline-medium baseline at TE_EFFORT_LEVEL=medium
    inject-only     --mode manual4                                 (micro-skill only)
    untool-only     --untool-evidence 3 --untool-judge              (untooling only)
    spiral-only     --spiral-guard --spiral-judge --spiral-nudge    (spiral only)
    spiral-only-web spiral-only + --spiral-nudge-llm --spiral-frozen-envelope 30
    trimech         inject-only + untool-only + spiral-only         (Claude / Copilot)
    trimech-web     trimech + --spiral-nudge-llm --spiral-frozen-envelope 30
                                                                    (short Web-Bench streams)
    codex-untool    --codex-recent-adaptive-untool-v7               (Codex untooling)
    codex-spiral    --codex-spiral-resume --codex-spiral-profile defensive
    codex-spiral-postgreen   the shipped post-green-v3 finalize alone (ablation)
    codex-combo     codex-untool + codex-spiral (co-activated)
    codex-trimech   codex-inject + codex-untool + codex-spiral (all three)
    codex-trimech-rtk  codex-trimech + rtk in its INSTRUCTION mode (no hook on Codex;
                    the row carries rtk_compliance)
    codex-trimech-repomap  codex-trimech + aider's repo map as the project AGENTS.md
                    (SWE-bench only; the row carries map_ok / map_chars)

    rtk / serena / repomap     an external tool baseline, no mechanism active
    trimech-rtk / -repomap     trimech co-activated with one of them

The single-mechanism arms use the SAME parameters trimech co-activates, so each is a true
ablation of it rather than a separately tuned arm. The external tool scaffolds are
implemented in `experiments/te_bench/baselines/` and install into the task container; a
misconfigured one fails at runner construction rather than producing rows that merely look
like a scaffold result.

The arm tables live next to the code they configure -- `experiments/te_bench/mechanism_arms.py`
for everything the economizer drives (including the two `trimech-*` integrations, which sit
beside `trimech`) and `experiments/te_bench/baselines/arms.py` for the pure-baseline arms. They
are assembled in `experiments/te_bench/arms.py` and re-exported here, so this module stays the
entry point the docs point at.

The heavy task-execution harness (Docker + agent CLI + official gold grading) lives BELOW
the ``te_bench.runner.AgentRunner`` seam, in the repo's ``te_harness`` package -- the
one the published rows came from, so ``--runner te_harness.swebench:make_runner`` reproduces
them as committed. Any other harness works the same way: pass
``--runner module:factory`` (a zero-arg callable returning an AgentRunner). ``--runner stub``
selects the built-in offline StubRunner used by the smoke test -- it writes a synthetic
transcript and a plausible row so the full facade -> aggregate -> figures pipeline can be
exercised with no Docker, no datasets, and no LLM.

Usage:
    python scripts/run_experiment.py --arm trimech --agent claude --repo pydata/xarray \\
        --seed 0 --results-root "$TOKENECONOMIZER_RESULTS/claude_swebench" \\
        --tasks-file "$TOKENECONOMIZER_SWEBENCH_TASKS" --runner te_harness.swebench:make_runner

Placeholders (env vars): the driver reads a --tasks-file JSON list per domain
(TOKENECONOMIZER_SWEBENCH_TASKS / _WEBBENCH_TASKS / _CREDDATA_TASKS / _REPOSVUL_TASKS); the
adapters + harness separately read the dataset roots (_WEBBENCH_PROJECTS / _CREDDATA_ROOT /
_REPOSVUL_ROOT). See README.md. The results root is git-ignored.
"""
import argparse
import collections
import importlib
import inspect
import json
import os
import sys
import time

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),  # _env, sweep_plan
                _EXPERIMENTS,                               # te_harness
                os.path.dirname(_EXPERIMENTS)]              # the core, at the repository root
from _env import load_env                                           # noqa: E402
from te_bench.facade import Economizer                              # noqa: E402
from tokeneconomizer import EconomizerConfig                        # noqa: E402
from tokeneconomizer import ledger                                  # noqa: E402
from tokeneconomizer.config import needs_optimizer               # noqa: E402


# ---- arm registry ------------------------------------------------------------------------
# Re-exported so this module stays the entry point the docs and every existing import point
# at. The definitions moved next to the code they configure:
#
#   experiments/te_bench/mechanism_arms.py  naive, effort variants, single-mechanism ablations,
#                                       trimech, trimech-web, the two trimech-<scaffold>
#                                       integrations (siblings of trimech), codex-*
#   experiments/te_bench/baselines/arms.py   rtk / serena / repomap -- external tool only
#   experiments/te_bench/arms.py             assembles both in canonical order
#
# `Arm`, `cfg` and `arm` come from te_bench._armspec; `_cfg`/`_arm` remain as aliases
# because tests and older callers refer to them by those names.
from te_bench.arms import ARMS, SCAFFOLD_ARMS                # noqa: E402,F401
from te_bench._armspec import Arm, arm as _arm, cfg as _cfg  # noqa: E402,F401


# ---- built-in offline StubRunner (smoke test only) ---------------------------------------
class StubRunner:
    """A harness stand-in that runs NO agent. It writes a synthetic transcript in the
    agent's schema and returns a plausible cache-heavy row, so the mechanisms' post-task
    hooks and the aggregator/figures pipeline can be exercised end-to-end offline.

    It is NOT a benchmark: `resolved` is a fixed deterministic pattern, not a real grade.
    """
    def __init__(self, agent):
        self.agent = agent
        self._codex = agent == "codex"

    def optimizer_call(self, prompt, model="agent"):
        # Benign, parseable optimizer output; ~40 effective tokens per call.
        return '{"extractors": [], "facts": []}', 40.0

    def _write_transcript(self, run_dir_path):
        path = os.path.join(run_dir_path, "transcript.jsonl")
        if self._codex:
            items = [{"type": "command_execution", "command": "python -m pytest -q"},
                     {"type": "command_execution", "command": "sed -n '1,40p' setup.py"}]
            lines = []
            for i, item in enumerate(items):
                it = {"id": f"i{i}", "status": "completed"}
                it.update(item)
                lines.append({"type": "item.completed", "item": it})
        else:
            calls = [("bash", {"command": "python -m pytest -q"}, "ok"),
                     ("read", {"file_path": "setup.py"}, "...")]
            lines = []
            for i, (name, inp, res) in enumerate(calls):
                tid = f"t{i}"
                lines.append({"message": {"content": [
                    {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
                lines.append({"message": {"content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": res}]}})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(x) for x in lines))

    def run_task(self, task, *, arm, injected, exclude_tools, run_dir_path, **run_kwargs):
        os.makedirs(run_dir_path, exist_ok=True)
        self._write_transcript(run_dir_path)
        idx = int(task.get("_index", 0))
        # A real billed model id so the vendored pricing table resolves a weight; the
        # originals billed claude-opus-4-8 (Claude Code / Copilot) and gpt-5.6-sol (Codex).
        billed = run_kwargs.get("model") or ("gpt-5.6-sol" if self._codex
                                             else "claude-opus-4-8")
        if billed == "agent":
            billed = "gpt-5.6-sol" if self._codex else "claude-opus-4-8"
        row = {
            # cache-heavy token breakdown pricing.effective_tokens() reads
            "input_tokens": 900, "cache_read_input_tokens": 7000,
            "cache_creation_input_tokens": 400, "output_tokens": 350,
            "tokens_total": 8650,
            "resolved": (idx % 3 != 0),          # deterministic, NOT a real grade
            "num_turns": 24 + idx, "is_error": False,
            "instance": task.get("instance_id", ""),
            "repo": task.get("repo", ""),
            "model": billed,
            "codex_spiral_nudges": 0,
        }
        return row, run_dir_path


def load_runner(spec, agent, model=None, scaffold=None):
    """`spec` is 'stub' or 'module.path:factory' -> AgentRunner.

    The factory is offered the `--agent` label first and called zero-arg only if it does
    not accept one. That fallback exists for older harnesses, and it is the reason the
    label is ALSO exported as TE_AGENT: a factory that silently ignores which agent was
    asked for will run whatever it hardcodes while the driver files the rows under
    `--agent`'s name -- plausible numbers attributed to an agent that never ran, which is
    worse than a crash. A harness that cannot honour the label should raise.

    `scaffold` (from the arm) is offered the same way. A harness that accepts NEITHER the
    keyword nor TE_SCAFFOLD would run NATIVE and file the rows under a scaffold's name --
    the same class of error as the agent label -- so that case raises rather than runs.
    """
    if spec == "stub":
        if scaffold:
            # The stub runs no agent and installs nothing, so a "scaffold" row from it
            # would be indistinguishable from a real one once it is sitting in results/.
            print(f"[runner stub] scaffold {scaffold!r} NOT installed -- "
                  f"synthetic rows, for wiring checks only")
        return StubRunner(agent)
    if ":" not in spec:
        raise SystemExit(f"--runner must be 'stub' or 'module:factory', got {spec!r}")
    mod_name, fac_name = spec.split(":", 1)
    factory = getattr(importlib.import_module(mod_name), fac_name)
    os.environ.setdefault("TE_AGENT", agent or "")
    try:
        sig = inspect.signature(factory)
    except (TypeError, ValueError):          # builtins / C callables: no signature
        return factory()
    takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                       for p in sig.parameters.values())
    kw = {}
    if "agent" in sig.parameters or takes_kwargs:
        kw["agent"] = agent
    # `--model` defaults to the sentinel "agent" (= "whatever the agent is configured to
    # use"), which is NOT a model id -- forwarding it would make the CLI ask for a model
    # called "agent". Only a real id is passed through; otherwise the runner falls back to
    # its own env var (ANTHROPIC_MODEL / COPILOT_MODEL), which is the historical behaviour.
    if model and model != "agent" and ("model" in sig.parameters or takes_kwargs):
        kw["model"] = model
    if scaffold:
        if "scaffold" in sig.parameters or takes_kwargs:
            kw["scaffold"] = scaffold
        else:
            raise SystemExit(
                f"runner {spec!r} does not accept a scaffold, but this arm needs "
                f"{scaffold!r}. A harness that ignored it would run NATIVE and file the "
                f"rows under the scaffold's name. Add a `scaffold=` parameter (see "
                f"experiments/te_bench/baselines/) or pick an arm without one.")
        os.environ.setdefault("TE_SCAFFOLD", scaffold)
    return factory(**kw) if kw else factory()


def load_adapter(name, tasks_file, repo=""):
    from te_bench.adapters import (
        SwebenchAdapter, WebBenchAdapter, CredDataAdapter, ReposVulAdapter)
    if name == "swebench":
        return SwebenchAdapter(tasks_path=tasks_file)
    if name == "webbench":
        # the Web-Bench project (= one stream) is selected by --repo; the per-task judge
        # then reads <projects>/<project>/tasks.jsonl. Defaults to 'dom' if --repo is unset.
        return WebBenchAdapter(repo.split("/")[-1] or "dom")
    return {"creddata": CredDataAdapter, "reposvul": ReposVulAdapter}[name]()


def workload_ids(workload_file, repo):
    """The set of task ids named by a `workloads/<domain>.json` manifest.

    With `repo` set, restrict to that group (SWE-bench repo slug / Web-Bench project).
    Single-group domains (CredData, ReposVul) ignore `repo` and use their only group. An
    unrecognized group is an error, never a silent widening."""
    with open(workload_file, encoding="utf-8") as fh:
        groups = json.load(fh)["groups"]
    if repo and repo in groups:
        return set(groups[repo])
    if len(groups) == 1:
        return set(next(iter(groups.values())))
    if repo:
        raise SystemExit(f"--repo {repo!r} is not a group in {workload_file}; "
                         f"expected one of {sorted(groups)}")
    return {i for ids in groups.values() for i in ids}


def _same_group(task, repo):
    """True when `task` belongs to the group `repo` names, by its own `repo` label.

    Matches the full slug and the bare repo name, so both a SWE-bench `pydata/xarray`
    and a Web-Bench `zustand` label resolve against the same --repo value."""
    label = str(task.get("repo") or "")
    if not label:
        return False
    return label == repo or label.split("/")[-1] == repo.split("/")[-1]


def load_tasks(tasks_file, repo, limit, workload=None):
    """Task stream as a list of {instance_id, problem_statement, repo}.

    With `workload` set (a `workloads/<domain>.json` manifest), keep exactly the ids it
    names — the reproducible path, and the one that survives repo slugs whose org differs
    from the repo name. Otherwise fall back to the legacy `repo` prefix match."""
    with open(tasks_file, encoding="utf-8") as fh:
        tasks = json.load(fh)
    if workload:
        wanted = workload_ids(workload, repo)
        tasks = [t for t in tasks if str(t.get("instance_id", "")) in wanted]
        # Ids are only unique WITHIN a group in some domains: every Web-Bench project
        # numbers its stream `init`, `task-1`..`task-20`, so an id match alone pulls in
        # all five projects' tasks interleaved -- and a Web-Bench stream is sequentially
        # dependent, so a mixed stream is not a smaller run but a meaningless one. When
        # the task file labels its rows with a group, narrow to the requested one.
        # SWE-bench ids are globally unique and its rows carry the exact repo slug, so
        # this is a no-op there; an unlabeled task file falls through unchanged.
        if repo:
            grouped = [t for t in tasks if _same_group(t, repo)]
            if grouped:
                tasks = grouped
        if not tasks:
            raise SystemExit(
                f"no tasks left after applying workload {workload}"
                + (f" (repo={repo})" if repo else "")
                + f"; {tasks_file} does not contain any of its {len(wanted)} ids")
    elif repo:
        tok = repo.split("/")[-1].replace("-", "_")
        alt = repo.split("/")[-1]
        tasks = [t for t in tasks
                 if str(t.get("instance_id", "")).replace("-", "_").startswith(tok)
                 or str(t.get("instance_id", "")).startswith(alt)] or tasks
    for t in tasks:
        t.setdefault("repo", repo)
    return tasks[:limit] if limit else tasks


def lift_overhead(row, srow):
    """Copy mechanism-overhead fields a mechanism reported on `srow` into `row`.

    row.json is the only artifact aggregate.py reads, so an overhead field that exists
    solely on the stream row is invisible to the cost basis. `untool_judge_tokens` is one:
    `tooling.after_task` writes the per-task instruction-aware tool judge's spend to `srow`
    alone, so without the lift every arm with judge=True -- untool-only and all four
    trimech variants -- would drop that line item from `cost_effective_tokens`. The bias
    would be one-directional: a baseline runs no judge, so only the arms under test would
    be flattered. Same shape as usage_incomplete in aggregate.py.

    A value the runner already put on `row` WINS: the harness sets spiral_judge_tokens
    itself during run_task, and `srow` may carry a stale or zeroed copy of it.

    Driven by ledger.OVERHEAD_KEYS so a newly added overhead field cannot silently open
    the same gap. Returns `row` for convenience.
    """
    for k in ledger.OVERHEAD_KEYS:
        if k not in row and srow.get(k) is not None:
            row[k] = srow[k]
    return row


def main():
    # Credentials + data roots come from the git-ignored repo-root .env (see .env.example);
    # anything already exported in the shell wins. Harmless for --runner stub, which needs
    # no credentials at all.
    load_env()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", required=True, choices=sorted(ARMS),
                    help="arm to run; %s additionally install an external tool scaffold "
                         "(experiments/te_bench/baselines/)"
                         % ", ".join(sorted(SCAFFOLD_ARMS)))
    ap.add_argument("--agent", default="claude", choices=["claude", "copilot", "codex"],
                    help="the sealed agent (selects transcript schema + generic-vs-codex "
                         "mechanisms)")
    ap.add_argument("--repo", default="", help="repo slug, e.g. pydata/xarray")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="cap tasks (0 = all)")
    ap.add_argument("--adapter", default="swebench",
                    choices=["swebench", "webbench", "creddata", "reposvul"])
    ap.add_argument("--tasks-file", required=True,
                    help="JSON list of {instance_id, problem_statement, repo} (a placeholder "
                         "you point at your dataset export; see README.md)")
    ap.add_argument("--workload", default="",
                    help="workloads/<domain>.json manifest naming the exact evaluated "
                         "instances; --tasks-file is filtered to it (and to --repo's group). "
                         "Committed in this repo — the reproducible way to pin a run.")
    ap.add_argument("--results-root", required=True,
                    help="<domain> results root; the arm subdir is created under it")
    ap.add_argument("--runner", default="stub",
                    help="'stub' (offline) or 'module:factory' returning an AgentRunner, "
                         "e.g. te_harness.swebench:make_runner (the published harness)")
    ap.add_argument("--model", default="agent")
    # An AGENT LABEL, not a model id. The between-task optimizer runs on the agent's own
    # model by construction (see the harness runners), so naming a model here would be the
    # one remaining way to charge mechanism overhead to a cheaper model than the arm was
    # measured on -- which would flatter the economized arm against a baseline that made
    # no such calls. A model id is refused rather than quietly diverting the ledger.
    ap.add_argument("--optimizer-model", default="agent",
                    help="agent label whose model runs the between-task optimizer "
                         "(agent|claude|copilot|codex...); NOT a model id")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any saved cross-task state (start the stream cold)")
    args = ap.parse_args()

    if "-" in args.optimizer_model and any(
            args.optimizer_model.startswith(p)
            for p in ("claude-", "gpt-", "gemini-", "o1-", "o3-", "o4-")):
        raise SystemExit(
            f"--optimizer-model {args.optimizer_model!r} looks like a model id, but this "
            "flag takes an AGENT LABEL. The between-task optimizer runs on the agent's "
            "own model so mechanism overhead is priced on the same basis as the task "
            "rows; pass --optimizer-model agent (or the agent's name).")

    arm = ARMS[args.arm]
    mode, cfg_factory = arm.mode, arm.cfg
    # An arm-owned environment overlay (reasoning effort) is applied BEFORE the runner is
    # built, because the harness reads it at construction. The arm WINS over an inherited
    # value -- the name is the authority for what the cell contains -- so say so when it
    # overrides something the caller had set, rather than changing it silently.
    for key, value in arm.env.items():
        prior = os.environ.get(key)
        if prior is not None and prior != value:
            print(f"[arm {args.arm}] {key}: overriding inherited {prior!r} with {value!r}")
        os.environ[key] = value
    if arm.env:
        print(f"[arm {args.arm}] env: " +
              " ".join(f"{k}={v}" for k, v in sorted(arm.env.items())))
    if arm.scaffold:
        print(f"[arm {args.arm}] external tool scaffold: {arm.scaffold}")
    config = cfg_factory()
    # Tell the harness whether any mechanism on this arm will make a paid between-task
    # call, BEFORE the runner is built. A harness whose mechanism transport needs a live
    # preflight (te_harness.copilot's Messages endpoint) must not bill one, or depend on
    # one, for `baseline` -- which never calls it. Same launcher->harness seam as TE_AGENT
    # / TE_SCAFFOLD / TE_EFFORT_APPLIES; unset means "assume it might", which is the safe
    # default for any caller that does not set it.
    os.environ["TE_OPTIMIZER_PREFLIGHT"] = "1" if needs_optimizer(config) else "0"
    adapter = load_adapter(args.adapter, args.tasks_file, args.repo)
    runner = load_runner(args.runner, args.agent, args.model, arm.scaffold)
    tasks = load_tasks(args.tasks_file, args.repo, args.limit, args.workload)
    if not tasks:
        raise SystemExit("no tasks after filtering")

    repo_tag = (args.repo.split("/")[-1] or "repo").replace("-", "_")
    cell = f"{repo_tag}_s{args.seed}"
    arm_root = os.path.join(args.results_root, args.arm, cell)
    streams_dir = os.path.join(arm_root, "_streams")
    os.makedirs(streams_dir, exist_ok=True)

    econ = Economizer(config, agent=args.agent, repo=args.repo or repo_tag,
                      results_root=arm_root, fresh=args.fresh,
                      optimizer_model=args.optimizer_model, max_turns=args.max_turns,
                      runner=runner, adapter=adapter)

    # Optional stream-level seam. Most harnesses own nothing beyond a single task, but
    # some own the whole stream: Web-Bench's evaluator drives its project's 20 sequentially
    # dependent tasks itself, so it has to be told the stream before the first task and be
    # allowed to shut down after the last. A harness without these hooks is unaffected.
    if hasattr(runner, "begin_stream"):
        runner.begin_stream(tasks, results_root=arm_root, arm=args.arm,
                            repo=args.repo, seed=args.seed)

    stream_path = os.path.join(streams_dir, f"{repo_tag}__{mode}__stream.jsonl")
    n_resolved = 0
    try:
        with open(stream_path, "w", encoding="utf-8") as stream:
            for i, task in enumerate(tasks):
                task["_index"] = i
                injected = econ.injection_prefix(task)
                exclude_tools = econ.build_tools(task, i)
                run_kwargs = dict(econ.run_kwargs(task))
                run_kwargs.setdefault("model", args.model)

                # Run-dir name: UTC timestamp first, then the stream index. The stamp
                # makes the directory unique, so re-running a cell ARCHIVES the previous
                # attempt beside the new one instead of overwriting its transcript in
                # place -- traces are the primary artifact and are not reproducible after
                # the fact. Timestamp-first also matches the historical results trees and
                # sorts chronologically; the index keeps stream order explicit and breaks
                # ties when two tasks land in the same second (an unreached Web-Bench task
                # costs no wall-clock at all). `mode` stays in the name because
                # aggregate.py recovers it from there.
                stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                run_name = (f"{stamp}__{i:03d}__{args.agent}"
                            f"__{task.get('instance_id','inst')}"
                            f"__{args.arm}__s{args.seed}__{mode}")
                run_dir = os.path.join(arm_root, run_name)
                row, saved_dir = runner.run_task(
                    task, arm=args.arm, injected=injected, exclude_tools=exclude_tools,
                    run_dir_path=run_dir, **run_kwargs)

                srow = {"resolved": row.get("resolved"), "num_turns": row.get("num_turns"),
                        "instance": task.get("instance_id", "")}
                opt_tokens, reason = econ.after_task(task, row, saved_dir, srow)
                row.setdefault("seed", args.seed)
                row.setdefault("run_dir", saved_dir)
                row["optimizer_tokens"] = row.get("optimizer_tokens", 0) + (opt_tokens or 0)
                lift_overhead(row, srow)

                with open(os.path.join(saved_dir, "row.json"), "w", encoding="utf-8") as fh:
                    json.dump(row, fh, indent=2)
                stream.write(json.dumps({**srow, **{k: row.get(k) for k in (
                    "input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens", "tokens_total",
                    "optimizer_tokens", "codex_spiral_nudges") + ledger.OVERHEAD_KEYS
                    if row.get(k) is not None or k in srow}}) + "\n")
                n_resolved += 1 if row.get("resolved") is True else 0
    finally:
        # A harness holding a stream-level resource (an evaluator process, a container
        # pool) must be able to release it even when the loop died mid-stream.
        if hasattr(runner, "end_stream"):
            runner.end_stream()

    print(f"arm={args.arm} agent={args.agent} repo={args.repo or repo_tag} "
          f"seed={args.seed} tasks={len(tasks)} resolved={n_resolved} -> {arm_root}")
    print(f"stream -> {stream_path}")


if __name__ == "__main__":
    main()
