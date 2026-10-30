#!/usr/bin/env python3
"""Compare one arm's cell against a stored baseline cell, instance by instance.

    python3 scripts/compare_arms.py \
        --baseline $TE_DATA/results/<tree>/baseline/scikit_learn_s0 \
        --arm      $TE_DATA/results/<tree>/codex-inject/scikit_learn_s0

Cost is the honest amortized basis (`pricing.effective_tokens` + `ledger.overhead`, so an
arm's own mechanism spend is charged to it). The comparison is PAIRED on instance id -- a
task missing from either side is reported and excluded -- and reports the pooled mean, the
median, the paired per-task delta, and resolve counts. It also summarises the injection
telemetry the arm's rows carry (how many tasks received a prefix, which facts were active),
so a saving can be read next to the mechanism activity that produced it.

The two cells may live in different trees; rows price by their billed model id, so effective
tokens stay comparable across trees, while the endpoint of each side (a Copilot subscription
or a BYOK provider, for example) is printed so nobody pools them by accident. Stdlib only.
"""
import argparse
import glob
import json
import os
import statistics as st
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_harness; the core at the root
from tokeneconomizer import ledger                    # noqa: E402
from tokeneconomizer import pricing            # noqa: E402


def load_cell(cell):
    """{instance: row} -- the LATEST run dir per instance (a re-run archives beside the old)."""
    rows, cfgs = {}, {}
    for rowf in sorted(glob.glob(os.path.join(cell, "20*", "row.json"))):
        try:
            r = json.load(open(rowf, encoding="utf-8"))
        except ValueError:
            continue
        inst = r.get("instance") or r.get("instance_id")
        if not inst:
            continue
        rows[inst] = r
        cf = os.path.join(os.path.dirname(rowf), "run_config.json")
        if os.path.exists(cf):
            try:
                cfgs[inst] = json.load(open(cf, encoding="utf-8"))
            except ValueError:
                pass
    return rows, cfgs


def cost(r):
    return pricing.effective_tokens(r) + ledger.overhead(r)


def percent_delta(arm, baseline):
    """A zero baseline has no defined relative change."""
    return f"{100 * (arm - baseline) / baseline:+.1f}%" if baseline else "n/a"


def describe(rows, cfgs, label):
    r0 = next(iter(rows.values()), {})
    c0 = next(iter(cfgs.values()), {})
    routing = c0.get("routing") or {}
    print(f"  {label:9s} n={len(rows):3d} agent={r0.get('agent')} model={r0.get('model')} "
          f"effort={r0.get('effort')} endpoint={routing.get('base_url')} "
          f"cli={r0.get('cli_version')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--json", help="write the comparison here")
    args = ap.parse_args()

    base, bcfg = load_cell(args.baseline)
    arm, acfg = load_cell(args.arm)
    print("== cells ==")
    describe(base, bcfg, "baseline")
    describe(arm, acfg, "arm")

    common = sorted(set(base) & set(arm))
    only_b, only_a = sorted(set(base) - set(arm)), sorted(set(arm) - set(base))
    if only_b or only_a:
        print(f"  unpaired: {len(only_b)} baseline-only, {len(only_a)} arm-only "
              f"(excluded from the paired figures)")
    inc = [i for i in common if base[i].get("usage_incomplete") or arm[i].get("usage_incomplete")]
    if inc:
        print(f"  usage_incomplete on {len(inc)} paired tasks (excluded): {inc[:5]}")
    pairs = [i for i in common if i not in inc]
    if not pairs:
        sys.exit("no paired, complete tasks to compare")

    bc = [cost(base[i]) for i in pairs]
    ac = [cost(arm[i]) for i in pairs]
    deltas = [(a - b) / b for a, b in zip(ac, bc) if b]
    delta_mean = st.mean(deltas) if deltas else None
    delta_median = st.median(deltas) if deltas else None
    br = sum(1 for i in pairs if base[i].get("resolved") is True)
    ar = sum(1 for i in pairs if arm[i].get("resolved") is True)
    flips_lost = [i for i in pairs if base[i].get("resolved") is True and arm[i].get("resolved") is not True]
    flips_won = [i for i in pairs if arm[i].get("resolved") is True and base[i].get("resolved") is not True]
    oh = sum(ledger.overhead(arm[i]) for i in pairs)

    print(f"\n== paired comparison over {len(pairs)} tasks ==")
    print(f"  {'':22s} {'baseline':>12s} {'arm':>12s} {'delta':>9s}")
    print(f"  {'resolved':22s} {br:12d} {ar:12d} {ar - br:+9d}")
    print(f"  {'mean eff tokens':22s} {st.mean(bc):12,.0f} {st.mean(ac):12,.0f} "
          f"{percent_delta(st.mean(ac), st.mean(bc)):>9s}")
    print(f"  {'median eff tokens':22s} {st.median(bc):12,.0f} {st.median(ac):12,.0f} "
          f"{percent_delta(st.median(ac), st.median(bc)):>9s}")
    print(f"  {'pooled eff tokens':22s} {sum(bc):12,.0f} {sum(ac):12,.0f} "
          f"{percent_delta(sum(ac), sum(bc)):>9s}")
    print(f"  {'mean steps':22s} {st.mean(base[i].get('num_turns') or 0 for i in pairs):12.1f} "
          f"{st.mean(arm[i].get('num_turns') or 0 for i in pairs):12.1f}")
    delta_summary = (f"mean {100 * delta_mean:+.1f}%, median {100 * delta_median:+.1f}%"
                     if deltas else "mean n/a, median n/a")
    print(f"  paired per-task delta: {delta_summary}; "
          f"{len(pairs) - len(deltas)} zero baseline tasks excluded from relative deltas; "
          f"arm cheaper on {sum(a < b for a, b in zip(ac, bc))}/{len(pairs)} tasks")
    print(f"  mechanism overhead charged to the arm: {oh:,.0f} eff tokens")
    print(f"  resolve flips: lost {len(flips_lost)} {flips_lost[:6]}  won {len(flips_won)} {flips_won[:6]}")

    # ---- injection telemetry on the arm ----
    with_prefix = sum(1 for i in pairs if (acfg.get(i, {}).get("injection") or {}).get("present"))
    chars = [acfg[i]["injection"]["chars"] for i in pairs
             if (acfg.get(i, {}).get("injection") or {}).get("present")]
    print(f"\n== injection telemetry (arm) ==")
    print(f"  tasks with a prefix: {with_prefix}/{len(pairs)}; prefix chars mean "
          f"{st.mean(chars) if chars else 0:,.0f} (~{(st.mean(chars) / 4) if chars else 0:,.0f} tokens)")
    if with_prefix:
        wp = [i for i in pairs if (acfg.get(i, {}).get("injection") or {}).get("present")]
        wo = [i for i in pairs if i not in set(wp)]
        for name, grp in (("with prefix", wp), ("without", wo)):
            if grp:
                print(f"  {name:12s} n={len(grp):3d} arm mean={st.mean(cost(arm[i]) for i in grp):,.0f} "
                      f"baseline mean={st.mean(cost(base[i]) for i in grp):,.0f} "
                      f"delta={percent_delta(st.mean(cost(arm[i]) for i in grp), st.mean(cost(base[i]) for i in grp))} "
                      f"resolved arm={sum(1 for i in grp if arm[i].get('resolved') is True)} "
                      f"base={sum(1 for i in grp if base[i].get('resolved') is True)}")

    # ---- learned environment facts (envfacts profile state saved beside the cell) ----
    for state in glob.glob(os.path.join(args.arm, "envfacts_state", "*.json")):
        d = json.load(open(state, encoding="utf-8"))
        print(f"\n== learned facts ({os.path.basename(state)}, {d.get('now')} tasks folded) ==")
        for fid, f in sorted(d.get("facts", {}).items()):
            life = f["life"]
            print(f"  {life['state']:9s} fire={life['fire_num']}/{life['fire_den']} "
                  f"promoted@{life.get('promoted_at')} retired@{life.get('retired_at')}  {fid}")
        # the block that would be rendered next, from the saved state
        from tokeneconomizer.microskills import EnvFactsDistiller
        dist = EnvFactsDistiller.load(d["repo"], args.arm)
        block = dist.render()
        print("  current block:" + ("\n    " + block.replace("\n", "\n    ") if block else " (none)"))
    # ---- per-task telemetry from the stream: when did the block first appear ----
    for sp in glob.glob(os.path.join(args.arm, "_streams", "*.jsonl")):
        first = None
        with open(sp, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                mf = row.get("manual_facts") or {}
                if first is None and any(isinstance(v, dict) and v.get("inject") for v in mf.values()):
                    first = i
        print(f"  first task index with an injectable fact (stream): {first}")

    if args.json:
        json.dump({"pairs": len(pairs), "baseline_resolved": br, "arm_resolved": ar,
                   "baseline_mean": st.mean(bc), "arm_mean": st.mean(ac),
                   "baseline_median": st.median(bc), "arm_median": st.median(ac),
                   "paired_delta_mean": delta_mean, "paired_delta_median": delta_median,
                   "paired_delta_tasks": len(deltas),
                   "flips_lost": flips_lost, "flips_won": flips_won,
                   "tasks_with_prefix": with_prefix}, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()
