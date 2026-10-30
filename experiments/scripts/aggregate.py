#!/usr/bin/env python3
"""Flatten a results tree into per-task and per-cell CSV tables.

Walks a results ROOT of the shape ``<arm>/<repo>_s<seed>/**/row.json`` (the layout the
runner writes and the curated exports keep) and emits, next to it:

  * all_rows.csv        -- one row per task-run (every field the comparisons read)
  * summary_by_cell.csv -- one row per (arm, repo, seed) with resolve rate + summed cost

Cost is the HONEST amortized basis (cache-aware effective execution + mechanism overhead),
computed via the vendored pricing table and the ledger, so a method that mainly prunes
cheap cached re-reads cannot look better than its real spend:

    cost_effective_tokens = pricing.effective_tokens(row) + ledger.overhead(row)

Usage:
    python scripts/aggregate.py <results_root> [--arms baseline trimech ...] [--out DIR]

`--arms` defaults to the immediate sub-directories of the results root that contain any
`row.json`. It narrows the REWRITE, not just the reading, so naming a subset of the tree
while writing to the canonical `<root>/index` is REFUSED -- it would leave the index the
comparisons read describing fewer arms than the tree holds. Pass `--out` to put a subset
index somewhere of its own. The results tree itself is git-ignored (regenerated per run);
this script and its output are placeholders you point at your own runs.
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_harness; the core at the root
from tokeneconomizer import ledger                     # noqa: E402
from tokeneconomizer import pricing             # noqa: E402

FIELDS = [
    "arm", "repo", "seed", "instance", "repo_full", "mode",
    "resolved",
    # Web-Bench grades a task with up to `retry` attempts, so it carries two resolve
    # metrics: pass@1 (first attempt -- Web-Bench's own headline) and pass@N (any attempt).
    # Empty on SWE-bench, which is single-attempt.
    "resolved_p1", "resolved_p2", "webbench_attempts",
    "is_error", "aborted", "num_turns",
    # A row whose token fields could not be read in full (the agent was killed before its
    # terminal usage event, or its debug log was unparseable). Such a row is a LOWER BOUND
    # on cost, so it must be visible in the CSV the comparisons read -- an unflagged zero
    # is indistinguishable from a genuinely free task.
    "usage_incomplete", "usage_incomplete_reason", "output_tokens_missing",
    # A row whose eval script never RAN (the grading `docker exec` failed -- usually the
    # container went away under us). `resolved` is False on such a row, which is
    # indistinguishable from a graded miss, so the distinction has to be its own column:
    # a not-graded row must be dropped from a resolve rate, not counted as a failure.
    "not_graded", "not_graded_reason",
    "tokens_total", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
    # `cost_usd` is the VENDOR's own dollar figure for the task, read from the
    # `cost_usd_vendor` the runners write. A row that ran but carries no figure is a
    # silent under-count when the column is summed -- its tokens land in the denominator
    # while its dollars do not -- so the absence gets its own column rather than reading
    # as a free task.
    "cost_usd", "vendor_cost_missing", "vendor_cost_missing_reason",
    "effective_tokens", "overhead_tokens", "cost_effective_tokens",
    "wall_clock_s", "patch_bytes", "model", "effort",
    "run_dir", "source_path",
]

# USD per effective (fresh-input-equivalent) token, for a $5/1M-output-tier model. Adjust
# for your billed model; it only scales the reported dollar figure, not the comparison.
USD_PER_EFFECTIVE_TOKEN = 5.0 / 1_000_000


def discover_arms(root):
    arms = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and glob.glob(os.path.join(d, "**", "row.json"),
                                          recursive=True):
            arms.append(name)
    return arms


def cell_of(path, root, arm):
    """(repo, seed) from a row.json path under <root>/<arm>/<repo>_s<seed>/..."""
    rel = os.path.relpath(path, os.path.join(root, arm))
    cell = rel.split(os.sep)[0]
    if "_s" in cell:
        repo, seed = cell.rsplit("_s", 1)
        return repo, seed
    return cell, ""


def normalize_repo(rs, inst):
    rs = rs or (inst.rsplit("-", 1)[0] if inst else "")
    rs = rs.replace("scikit-learn", "sklearn").replace("pydata__xarray", "xarray")
    return rs.split("__")[0] if "__" in rs else rs


_STAMP_RE = re.compile(r"^(\d{8}T\d{6}Z)__")


def _run_order(rj):
    """Sort key putting the NEWEST run of a task last.

    Run dirs are named `<UTC>__<index>__...`, so the stamp orders them directly; a dir from
    an older layout with no stamp falls back to its mtime. Both are compared as strings
    against a fixed-width stamp, so an unstamped dir never outranks a stamped one by
    accident -- it is ordered by its own mtime instead."""
    name = os.path.basename(os.path.dirname(rj))
    m = _STAMP_RE.match(name)
    if m:
        return (1, m.group(1), name)
    try:
        return (0, str(int(os.path.getmtime(rj))), name)
    except OSError:
        return (0, "", name)


def usage_incomplete(d):
    """(is_incomplete, reason) for one row: are its token fields a LOWER BOUND?

    Two sources, because the flag has to cover rows from a harness that does not set it:

      * The harness says so itself. `te_harness` sets `usage_incomplete` when the agent was
        killed before its terminal usage event and the row had to be rebuilt from the
        transcript (input/cache exact, `output_tokens` unrecoverable), or when Copilot's
        debug log could not be parsed.
      * A row with no flag is recognised by its signature (`legacy_zero_usage`): a flat
        zero for every token field, yet the task demonstrably ran (it resolved, or it left
        a patch). The alternative reading, a task that really cost nothing and still
        produced a diff, is not a thing that happens. Only guarded arms kill a rollout
        mid-stream, so leaving such rows unmarked would bias every economized arm's mean
        DOWNWARD.

    `pricing.effective_tokens` is deliberately left alone: this reports the lower bound, it
    does not paper over it. A recovered figure is a floor and gets labelled as one.
    """
    if d.get("usage_incomplete"):
        return True, d.get("usage_incomplete_reason") or "harness_flagged"
    ran = bool(d.get("resolved")) or (d.get("patch_bytes") or 0) > 0
    if ran and not (d.get("tokens_total") or 0) and not (d.get("input_tokens") or 0):
        return True, "legacy_zero_usage"
    return False, ""


def not_graded(d):
    """(never_graded, reason) for one row: did the official eval script actually run?

    `_grade` flags `eval_exec_failed` when the grading `docker exec` came back nonzero
    with nothing on stdout -- the eval did not run, so the patch was never scored. The
    row still carries `resolved: False`, because there is no third value for it, and that
    is precisely the trap: an ungraded row read as a graded miss depresses a resolve rate
    with a task nobody evaluated. Counted separately and excluded from the denominator.
    """
    # A stopped sequential Web-Bench stream emits placeholder rows for later tasks.
    # Their resolved=False is not a verdict: no attempt or evaluation took place.
    if d.get("unreached") is True:
        return True, "unreached"
    rep = d.get("eval_report") or {}
    if isinstance(rep, dict) and rep.get("eval_exec_failed"):
        return True, (rep.get("eval_exec_error")
                      or f"eval exec rc={rep.get('eval_exec_rc')}")
    if isinstance(rep, dict) and rep.get("grading_error"):
        return True, f"grading_error: {rep['grading_error']}"
    return False, ""


def vendor_usd(d):
    """The vendor's own dollar figure for one task, or "" when the row carries none.

    The runners write it as `cost_usd_vendor` (Claude Code's `total_cost_usd`, copied
    from its final `result` event); only the older curated exports carry it under the
    plain name `cost_usd`. Reading just `cost_usd` would leave the column EMPTY for every
    tree this repo generates. The COLUMN is still named `cost_usd` so its consumers keep
    working; only the source key differs.
    """
    for k in ("cost_usd_vendor", "cost_usd"):
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return ""


# Agents that never report a per-task dollar figure at all: Copilot meters premium
# requests and Codex bills a subscription, so a null `cost_usd_vendor` on those lanes is
# the normal case, not a defect. Matched on the agent prefix,
# so a suffixed label is covered without a new entry.
AGENTS_WITHOUT_VENDOR_USD = ("copilot", "codex")


def vendor_cost_missing(d):
    """(missing, reason) for one row: did a task that RAN lose its vendor dollar figure?

    When the agent's final `result` event arrives without `total_cost_usd` the row stores
    None, and nothing but this flag marks it. The row is not otherwise damaged, which
    is what makes it dangerous: token accounting is complete (so `usage_incomplete` stays
    False and the effective-token basis is exact) and the task grades normally, so the
    row looks healthy from every other column. Summing `cost_usd` across the cell then
    UNDER-counts -- the row's tokens are in the denominator while its dollars are not.

    It concentrates in the longest trajectories, i.e. the most expensive tasks, so a few
    such rows can move an arm's billed total enough to change its rank in a comparison.

    The effective-token basis is immune, which is the argument for reporting on it; this
    flag is what keeps the vendor column honest when someone sums it anyway.
    """
    agent = str(d.get("agent") or "")
    if agent.startswith(AGENTS_WITHOUT_VENDOR_USD):
        return False, ""
    if vendor_usd(d) != "":
        return False, ""
    # "Ran" on the same evidence usage_incomplete() uses: a verdict, a patch, or tokens.
    ran = (d.get("resolved") is True or (d.get("patch_bytes") or 0) > 0
           or (d.get("tokens_total") or 0) > 0 or (d.get("input_tokens") or 0) > 0)
    if not ran:
        return False, ""
    return True, f"{agent or 'agent'} reported no total_cost_usd"


def collect(root, arms):
    rows = []
    for arm in arms:
        for rj in glob.glob(os.path.join(root, arm, "**", "row.json"), recursive=True):
            try:
                d = json.load(open(rj))
            except Exception as e:
                print("SKIP (unreadable):", rj, e)
                continue
            repo_cell, seed = cell_of(rj, root, arm)
            inst = d.get("instance") or d.get("instance_id", "")
            repo = normalize_repo(repo_cell, inst)
            rd = d.get("run_dir", "")
            mode = ("manual4" if "manual4" in rd
                    else "standalone" if "standalone" in rd else "")
            eff = pricing.effective_tokens(d)
            oh = ledger.overhead(d)
            incomplete, reason = usage_incomplete(d)
            ungraded, ungraded_reason = not_graded(d)
            vmissing, vreason = vendor_cost_missing(d)
            rows.append({
                "arm": arm, "repo": repo, "seed": d.get("seed", seed),
                "instance": inst, "repo_full": d.get("repo", ""), "mode": mode,
                "resolved": d.get("resolved", ""),
                # Web-Bench grades a task with up to `retry` attempts, so it has two
                # resolve metrics: pass@1 (first attempt -- Web-Bench's own headline) and
                # pass@N (any attempt). SWE-bench is single-attempt and leaves these empty,
                # where they fall back to `resolved` in the cell rollup below.
                "resolved_p1": d.get("resolved_p1", ""),
                "resolved_p2": d.get("resolved_p2", ""),
                "webbench_attempts": d.get("webbench_attempts", ""),
                "is_error": d.get("is_error", ""),
                "aborted": d.get("aborted", ""), "num_turns": d.get("num_turns", ""),
                "tokens_total": d.get("tokens_total", ""),
                "input_tokens": d.get("input_tokens", ""),
                "output_tokens": d.get("output_tokens", ""),
                "cache_read_input_tokens": d.get("cache_read_input_tokens", ""),
                "cache_creation_input_tokens": d.get("cache_creation_input_tokens", ""),
                "cost_usd": vendor_usd(d),
                "vendor_cost_missing": vmissing,
                "vendor_cost_missing_reason": vreason,
                "effective_tokens": round(eff, 1),
                "overhead_tokens": round(oh, 1),
                "cost_effective_tokens": round(eff + oh, 1),
                "wall_clock_s": d.get("wall_clock_s", ""),
                "patch_bytes": d.get("patch_bytes", ""),
                "model": d.get("model", ""), "effort": d.get("effort", ""),
                "usage_incomplete": incomplete,
                "usage_incomplete_reason": reason,
                "not_graded": ungraded,
                "not_graded_reason": ungraded_reason,
                "output_tokens_missing": d.get("output_tokens_missing", ""),
                "run_dir": rd, "source_path": os.path.relpath(rj, root),
                "_order": _run_order(rj),
            })
    # One row per (arm, cell, instance): keep the NEWEST run and leave the earlier
    # attempts on disk. Re-running a cell adds timestamped run dirs beside the old ones, so
    # without this a re-run would double the cell's task count and average a task's cost
    # with a superseded attempt of itself. A tree that was never re-run is unaffected.
    newest = {}
    superseded = 0
    for r in rows:
        key = (r["arm"], str(r["repo"]), str(r["seed"]), r["instance"])
        prev = newest.get(key)
        if prev is None:
            newest[key] = r
        else:
            superseded += 1
            if r["_order"] > prev["_order"]:
                newest[key] = r
    if superseded:
        print(f"superseded runs skipped: {superseded} "
              f"(older attempts of a re-run task; still on disk)")
    rows = list(newest.values())
    for r in rows:
        r.pop("_order", None)
    rows.sort(key=lambda r: (r["arm"], str(r["repo"]), str(r["seed"]), r["instance"]))
    return rows


def summarize(rows):
    agg = collections.OrderedDict()
    for r in rows:
        key = (r["arm"], r["repo"], str(r["seed"]))
        a = agg.setdefault(key, {"n": 0, "graded": 0, "resolved": 0, "errors": 0,
                                 "eff": 0.0, "turns": 0, "incomplete": 0,
                                 "ungraded": 0, "p1": 0, "p2": 0, "graded_pn": 0,
                                 "vendor_missing": 0})
        a["n"] += 1
        if r.get("usage_incomplete") is True:
            a["incomplete"] += 1
        if r.get("vendor_cost_missing") is True:
            a["vendor_missing"] += 1
        if r.get("not_graded") is True:
            # The eval never ran. Not a miss -- an absence. Leaving it in `graded` would
            # let a lost container look like the model failing the task.
            a["ungraded"] += 1
        elif r["resolved"] != "":
            a["graded"] += 1
            if r["resolved"] is True:
                a["resolved"] += 1
            # pass@1 / pass@N. A row with no per-attempt verdicts (SWE-bench, or a
            # Web-Bench row written before this field existed) falls back to `resolved`,
            # which for a single-attempt domain is the same number. An UNREACHED row has
            # resolved_p1 = None: never attempted, so it counts in neither numerator nor
            # denominator -- scoring it as a miss would blame the agent for a task the
            # evaluator never ran.
            p1, p2 = r.get("resolved_p1", ""), r.get("resolved_p2", "")
            att = r.get("webbench_attempts", "")
            if p1 == "" and p2 == "":
                if att != "":
                    # A Web-Bench row written before resolved_p1/p2 existed. The metrics
                    # are still RECOVERABLE: the runner breaks out of the attempt loop on
                    # the first pass, so a resolved row that took 2 attempts necessarily
                    # failed attempt 1. Deriving them beats falling back to `resolved`,
                    # which would silently report pass@N under the pass@1 column.
                    att = int(att or 0)
                    if att == 0:
                        p1 = p2 = None          # unreached: never attempted
                    else:
                        p2 = r["resolved"] is True
                        p1 = p2 and att == 1
                else:
                    # Single-attempt domain (SWE-bench): one attempt, so the two coincide.
                    p1 = p2 = r["resolved"]
            if p1 is not None and p1 != "":
                a["graded_pn"] += 1
                if p1 is True:
                    a["p1"] += 1
                if p2 is True:
                    a["p2"] += 1
        if r["is_error"] is True:
            a["errors"] += 1
        try:
            a["eff"] += float(r["cost_effective_tokens"])
        except (TypeError, ValueError):
            pass
        try:
            a["turns"] += int(r["num_turns"])
        except (TypeError, ValueError):
            pass
    return agg


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", help="results tree root (<arm>/<repo>_s<seed>/**/row.json)")
    ap.add_argument("--arms", nargs="*", default=None, help="arm dirs (default: auto)")
    ap.add_argument("--out", default=None, help="output dir (default: <root>/index)")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    present = discover_arms(root)
    if not present:
        ap.error(f"no arm dirs with row.json under {root}")
    arms = args.arms or present
    out = args.out or os.path.join(root, "index")

    # `--arms` limits the REWRITE, not just the reading. The two CSVs are written whole
    # every time, so narrowing the arm list while writing to the canonical <root>/index
    # replaces an index describing the tree with one describing a subset -- at the path
    # compare_arms.py reads, under a name that says nothing about being partial. A lane
    # that runs one arm per invocation (the Codex lane) is the easy way to do it. So a
    # subset write to the canonical path is refused rather than warned about -- pass --out
    # to put the subset somewhere of its own, or drop --arms and aggregate the tree.
    if args.arms and args.out is None:
        dropped = [a for a in present if a not in arms]
        if dropped:
            ap.error(
                "--arms would rewrite the canonical index with a SUBSET of the tree.\n"
                f"  index:   {os.path.join(root, 'index')}\n"
                f"  keeping: {' '.join(arms)}\n"
                f"  DROPPED: {' '.join(dropped)}\n"
                "  The index is what compare_arms.py reads, so this would\n"
                "  silently discard those arms' rows. Either\n"
                f"    aggregate the whole tree:  {os.path.basename(sys.argv[0])} {args.root}\n"
                "    or write the subset apart: --out <dir>")

    unknown = [a for a in arms if a not in present]
    if unknown:
        print(f"!! --arms named {len(unknown)} dir(s) with no row.json under {root}: "
              f"{' '.join(unknown)}")

    os.makedirs(out, exist_ok=True)

    rows = collect(root, arms)
    with open(os.path.join(out, "all_rows.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    agg = summarize(rows)
    with open(os.path.join(out, "summary_by_cell.csv"), "w", newline="") as f:
        w = csv.writer(f)
        # n_usage_incomplete: how many of this cell's rows are a cost LOWER BOUND. A cell
        # with any is not comparable to one without on cost until they are accounted for,
        # so the count travels with the mean instead of being a separate audit step.
        w.writerow(["arm", "repo", "seed", "n_tasks", "n_graded", "n_resolved",
                    "resolve_rate",
                    "n_attempted", "n_resolved_p1", "resolve_rate_p1",
                    "n_resolved_p2", "resolve_rate_p2",
                    "n_errors", "n_usage_incomplete", "n_not_graded",
                    "n_vendor_cost_missing",
                    "sum_effective_tokens",
                    "sum_cost_usd", "sum_turns", "mean_effective_tokens",
                    "mean_cost_usd"])
        for (arm, repo, seed), a in agg.items():
            n, g = a["n"], a["graded"]
            cost = a["eff"] * USD_PER_EFFECTIVE_TOKEN
            gp = a["graded_pn"]
            w.writerow([arm, repo, seed, n, g, a["resolved"],
                        round(a["resolved"] / g, 4) if g else "",
                        gp, a["p1"], round(a["p1"] / gp, 4) if gp else "",
                        a["p2"], round(a["p2"] / gp, 4) if gp else "",
                        a["errors"], a["incomplete"], a["ungraded"],
                        a["vendor_missing"],
                        round(a["eff"], 1), round(cost, 4),
                        a["turns"], round(a["eff"] / n, 1) if n else "",
                        round(cost / n, 6) if n else ""])

    print(f"arms: {arms}")
    bad = [r for r in rows if r.get("usage_incomplete") is True]
    if bad:
        by_arm = collections.Counter(r["arm"] for r in bad)
        print(f"!! usage_incomplete: {len(bad)} of {len(rows)} rows have token fields that "
              f"are a LOWER BOUND, not a measurement -- their cost is understated and "
              f"their cells' means with it")
        for arm, k in sorted(by_arm.items()):
            print(f"     {arm}: {k}")
        print("   see the usage_incomplete_reason column in all_rows.csv")
    vbad = [r for r in rows if r.get("vendor_cost_missing") is True]
    if vbad:
        by_arm = collections.Counter(r["arm"] for r in vbad)
        print(f"!! vendor_cost_missing: {len(vbad)} of {len(rows)} rows RAN but carry no "
              f"vendor dollar figure -- summing the cost_usd column under-counts those "
              f"cells, while their effective_tokens are complete. Compare on "
              f"cost_effective_tokens, or re-run those tasks")
        for arm, k in sorted(by_arm.items()):
            print(f"     {arm}: {k}")
        print("   see the vendor_cost_missing_reason column in all_rows.csv")
    never = [r for r in rows if r.get("not_graded") is True]
    if never:
        by_arm = collections.Counter(r["arm"] for r in never)
        print(f"!! not_graded: {len(never)} of {len(rows)} rows were NEVER GRADED -- their "
              f"task was not reached or evaluation did not run, so `resolved` is an absence, not a miss. They are "
              f"excluded from n_graded/resolve_rate; re-run those cells")
        for arm, k in sorted(by_arm.items()):
            print(f"     {arm}: {k}")
        print("   see the not_graded_reason column in all_rows.csv")
    print(f"all_rows.csv: {len(rows)} task-runs -> {out}")
    print(f"summary_by_cell.csv: {len(agg)} cells -> {out}")


if __name__ == "__main__":
    main()
