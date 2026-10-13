#!/usr/bin/env python3
"""
Amortized-cost aggregation for stream runs.

A stream run (stream_run.py) writes one JSONL row per task to
results/<root>/_streams/<repo>__<mode>__<ts>.jsonl. Each row records the task's
EXECUTION cost (`tokens_total`, what the agent spent solving the task — this
already includes the injected manifest/notes/carryover-log it read) and, kept in
a SEPARATE field, the per-task OPTIMIZER overhead (`optimizer_tokens`, the real
token cost of the gated/ungated note-writing call; 0 when the gate didn't fire).

The HONEST amortized cost of a mode is the sum of BOTH:

    task_cost(t)  = tokens_total(t) + optimizer_tokens(t)
    amortized(T)  = (1/T) * sum_{t<=T} task_cost(t)

Reporting only `tokens_total` flatters `full`/`self-memory` (which pay optimizer
overhead) against `standalone`/`carryover` (which don't). This module folds the two
together so every mode is compared on total tokens actually spent.

Usage:
    python summarize_stream.py results/v1/_streams/*.jsonl       # compare modes
    python summarize_stream.py --curve run.jsonl                 # per-position curve

Cost is reported CACHE-AWARE by default: per-task tokens are reweighted into
fresh-input-token equivalents (pricing.py) because `tokens_total` counts cached
re-reads (~90% of tokens) at full weight, which biases A/Bs. Pass --raw to report
the legacy full-weight `tokens_total` instead.
"""
import argparse, glob, json, os, sys

# Import the vendored pricing table + the honest ledger from the tokeneconomizer package.
_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_harness; the core at the root
from tokeneconomizer import ledger
from tokeneconomizer.vendor import pricing


def _num(x):
    return x if isinstance(x, (int, float)) else 0


_RUNDIR_CACHE = {}


def _row_breakdown(r, stream_path):
    """Return the per-task token breakdown for cache-aware costing. Prefer the fields
    on the stream row itself (new runs carry them); fall back to the per-task row.json
    in the run dir (older streams predate the inline fields); else return the row as-is
    (effective_tokens then falls back to tokens_total)."""
    if pricing.has_breakdown(r):
        return r
    run_dir = r.get("run_dir")
    if run_dir:
        # stream file lives at <root>/_streams/<file>.jsonl -> run dirs are under <root>
        root = os.path.dirname(os.path.dirname(os.path.abspath(stream_path)))
        rj = os.path.join(root, run_dir, "row.json")
        if rj in _RUNDIR_CACHE:
            return _RUNDIR_CACHE[rj] or r
        loaded = None
        if os.path.isfile(rj):
            try:
                loaded = json.load(open(rj))
            except (OSError, json.JSONDecodeError):
                loaded = None
        _RUNDIR_CACHE[rj] = loaded
        if loaded is not None:
            return loaded
    return r


def summarize(path, raw=False, exclude_init=False):
    """Read one stream JSONL and return a summary dict with per-position curves and
    cumulative totals. `task_cost = exec_cost + optimizer_tokens`. By default exec_cost
    is the CACHE-AWARE effective cost (pricing.effective_tokens); with raw=True it is the
    legacy full-weight `tokens_total`. Optimizer tokens are counted at face value (they
    are one-shot, largely uncached calls and small relative to exec).

    `exclude_init` drops the Web-Bench `init` scaffold row (instance == "init") before any
    aggregation, so every project's amortized cost normalizes over the SAME 20 graded tasks
    (calculator/table ship an `init` record; react/expressjs/zustand do not). Default False keeps
    SWE-bench byte-identical -- SWE-bench streams never contain an `init` row."""
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if exclude_init:
        rows = [r for r in rows if r.get("instance") != "init"]
    rows.sort(key=lambda r: _num(r.get("position")))

    curve, cum_exec, cum_opt, cum_resolved = [], 0, 0, 0
    cum_raw_exec = 0
    n_graded = 0
    n_with_breakdown = 0
    n_mech_effective = 0   # rows whose mechanism tokens are cache-aware effective
    n_mech_raw = 0         # legacy rows with raw (face-value) mechanism tokens present
    for r in rows:
        rec = _row_breakdown(r, path)
        raw_exec = _num(r.get("tokens_total"))
        eff_exec = pricing.effective_tokens(rec)
        if pricing.has_breakdown(rec):
            n_with_breakdown += 1
        exec_t = raw_exec if raw else eff_exec
        # OPTIMIZER + MECHANISM-INTERNAL overhead. optimizer_tokens is the note-writing
        # call; untool_classify_tokens is the one-shot tool-classifier LLM call;
        # untool_judge_tokens is the per-task instruction-aware tool judge; spiral_judge_tokens
        # is the paid live trace-judge; spiral_nudge_writer_tokens is the v6.1 targeted-nudge
        # writer. All are real mechanism-internal LLM cost that must be amortized into the
        # honest task cost. The overhead-key set is the SINGLE definition in
        # economizer.ledger.OVERHEAD_KEYS is now tokeneconomizer.ledger.OVERHEAD_KEYS,
        # shared with the emit path so the two never drift.
        # (This now INCLUDES spiral_nudge_writer_tokens, previously emitted but never summed
        # -- a latent under-count of amortized cost, fixed per the unified-ledger decision.)
        # BASIS: rows tagged mech_tokens_basis=="effective" carry cache-aware effective
        # tokens (same basis as exec) — sum directly. Legacy rows (no tag) carry RAW
        # face-value tokens: an UNDER-count of effective (mechanism calls are uncached
        # one-shots, so effective = input + 5*output >= raw). We sum them as a documented
        # floor and surface the mixed basis in the summary rather than silently blending.
        opt_t = ledger.overhead(r)
        mech_basis = r.get("mech_tokens_basis")  # "effective" for new rows, None legacy
        if opt_t > 0:
            if mech_basis == "effective":
                n_mech_effective += 1
            else:
                n_mech_raw += 1
        cum_exec += exec_t
        cum_raw_exec += raw_exec
        cum_opt += opt_t
        resolved = r.get("resolved")
        if resolved is not None:
            n_graded += 1
            if resolved:
                cum_resolved += 1
        k = len(curve) + 1
        curve.append({
            "position": r.get("position"),
            "instance": r.get("instance"),
            "exec_tokens": round(exec_t, 1),
            "optimizer_tokens": opt_t,
            "task_cost": round(exec_t + opt_t, 1),
            "cum_exec_tokens": round(cum_exec, 1),
            "cum_optimizer_tokens": cum_opt,
            "cum_total_tokens": round(cum_exec + cum_opt, 1),
            "amortized_cost_per_task": round((cum_exec + cum_opt) / k, 1),
            "resolved": resolved,
        })

    n = len(rows)
    mode = rows[0].get("mode") if rows else None
    cum_total = cum_exec + cum_opt
    return {
        "path": path,
        "mode": mode,
        "cost_basis": "tokens_total (raw)" if raw else "cache-aware effective",
        "n_tasks": n,
        "n_graded": n_graded,
        "n_with_breakdown": n_with_breakdown,
        # mechanism-token basis audit: how many costed rows carry effective vs legacy-raw
        # mechanism tokens. "mixed"/"raw" is a flag that the overhead is an under-count.
        "mech_tokens_basis": ("effective" if n_mech_raw == 0 and n_mech_effective > 0
                              else "raw" if n_mech_effective == 0 and n_mech_raw > 0
                              else "mixed" if n_mech_effective and n_mech_raw
                              else "none"),
        "n_mech_effective": n_mech_effective,
        "n_mech_raw": n_mech_raw,
        "resolved": cum_resolved,
        "resolve_rate": round(cum_resolved / n_graded, 3) if n_graded else None,
        "cum_exec_tokens": round(cum_exec, 1),
        "cum_raw_exec_tokens": cum_raw_exec,
        "cum_optimizer_tokens": cum_opt,
        "cum_total_tokens": round(cum_total, 1),
        "optimizer_overhead_frac": round(cum_opt / cum_total, 4) if cum_total else 0.0,
        "amortized_cost_per_task": round(cum_total / n, 1) if n else 0.0,
        "exec_cost_per_task": round(cum_exec / n, 1) if n else 0.0,
        "raw_exec_cost_per_task": round(cum_raw_exec / n, 1) if n else 0.0,
        "curve": curve,
    }


def print_curve(s):
    print(f"\n# {s['mode']}  ({s['n_tasks']} tasks)  {os.path.basename(s['path'])}")
    print(f"{'pos':>4} {'instance':<24} {'exec':>9} {'opt':>8} {'task':>9} "
          f"{'cum_total':>11} {'amort/task':>11} {'resolved':>8}")
    for c in s["curve"]:
        print(f"{str(c['position']):>4} {str(c['instance'])[:24]:<24} "
              f"{c['exec_tokens']:>9} {c['optimizer_tokens']:>8} {c['task_cost']:>9} "
              f"{c['cum_total_tokens']:>11} {c['amortized_cost_per_task']:>11} "
              f"{str(c['resolved']):>8}")


def print_compare(summaries):
    basis = summaries[0]["cost_basis"] if summaries else "?"
    print(f"\ncost basis: {basis}  (cache-aware = fresh-input-token equivalents; "
          f"pass --raw for legacy tokens_total)")
    print(f"\n{'mode':<14} {'tasks':>5} {'resolve':>8} {'exec/task':>11} "
          f"{'opt/task':>10} {'amort/task':>11} {'opt%':>6}")
    print("-" * 70)
    for s in summaries:
        opt_per = round(s["cum_optimizer_tokens"] / s["n_tasks"], 1) if s["n_tasks"] else 0
        rr = "n/a" if s["resolve_rate"] is None else f"{s['resolve_rate']:.3f}"
        print(f"{str(s['mode']):<14} {s['n_tasks']:>5} {rr:>8} "
              f"{s['exec_cost_per_task']:>11.0f} {opt_per:>10} "
              f"{s['amortized_cost_per_task']:>11.0f} "
              f"{s['optimizer_overhead_frac']*100:>5.1f}%")
    # warn if any run lacked the breakdown (effective cost fell back to tokens_total)
    missing = [s for s in summaries if s["n_with_breakdown"] < s["n_tasks"]
               and "raw" not in s["cost_basis"]]
    if missing:
        print("\n  NOTE: some tasks lacked a token breakdown; effective cost fell back "
              "to tokens_total for those:")
        for s in missing:
            print(f"    {os.path.basename(s['path'])}: "
                  f"{s['n_with_breakdown']}/{s['n_tasks']} tasks had a breakdown")
    print("\nexec/task   = mean execution cost (cache-aware unless --raw; incl. injected context)")
    print("opt/task    = mean optimizer overhead (face value; gating is free)")
    print("amort/task  = (exec + optimizer) / tasks  <- the HONEST amortized cost")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="stream JSONL file(s) or globs")
    ap.add_argument("--curve", action="store_true",
                    help="also print the per-position amortized-cost curve for each run")
    ap.add_argument("--raw", action="store_true",
                    help="report legacy full-weight tokens_total instead of cache-aware cost")
    ap.add_argument("--exclude-init", action="store_true",
                    help="drop the Web-Bench `init` scaffold row so cost normalizes over the "
                         "20 graded tasks (no-op on SWE-bench streams, which have no init row)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        files.extend(sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p])
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        print("no stream JSONL files matched", file=sys.stderr)
        sys.exit(1)

    summaries = [summarize(f, raw=args.raw, exclude_init=args.exclude_init) for f in files]
    if args.json:
        print(json.dumps(summaries, indent=2))
        return
    if args.curve:
        for s in summaries:
            print_curve(s)
    print_compare(summaries)


if __name__ == "__main__":
    main()
