"""The main-experiment sweep: its arms are the ablation they claim to be, offline.

The sweep's whole argument is comparative -- one mechanism against three against none --
so the arms have to differ in exactly the way their names say. Nothing here runs an agent;
these are assertions about the registry `scripts/run_experiment.py` maps an arm name onto,
which is the only place that mapping exists.

Guarded specifically:
  * a single-mechanism arm activates ONE mechanism, and with trimech's own parameters, so
    it is an ablation of trimech rather than a separately tuned arm;
  * trimech is exactly the union of the three, so "3-mech active" means what it says;
  * the reasoning-effort arms differ from naive ONLY in the harness env, since an effort
    level is invisible in an EconomizerConfig and a mislabelled cell would be undetectable
    after the fact;
  * every arm the sweep plan names is registered, and the scaffold arms agree with
    tokeneconomizer/baselines' own arm->scaffold map -- an arm that named a scaffold the harness
    does not have, or carried none where the harness expects one, would run NATIVE and file
    the rows under the scaffold's name.
"""
import importlib.util
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RX = _load("_run_experiment", os.path.join(_REPO, "scripts", "run_experiment.py"))
SP = _load("_sweep_plan", os.path.join(_REPO, "scripts", "sweep_plan.py"))


def _active(cfg):
    """The set of mechanism names this config switches on."""
    return {name for name in ("injection", "tooling", "spiral")
            if getattr(cfg, name).enabled}


# ---- one mechanism means one mechanism --------------------------------------------------
def test_single_mech_arms_activate_exactly_one():
    for arm, mech in (("inject-only", "injection"),
                      ("untool-only", "tooling"),
                      ("spiral-only", "spiral"),
                      ("spiral-only-web", "spiral")):
        assert _active(RX.ARMS[arm].cfg()) == {mech}, arm


def test_baseline_arms_activate_nothing():
    for arm in ("baseline", "baseline-medium", "baseline-low"):
        assert _active(RX.ARMS[arm].cfg()) == set(), arm


def test_single_mech_arms_reuse_trimechs_parameters():
    """An ablation of trimech, not a retuned arm: each single-mech config must equal
    trimech's corresponding stage exactly."""
    tri = RX.ARMS["trimech"].cfg()
    assert RX.ARMS["inject-only"].cfg().injection == tri.injection
    assert RX.ARMS["untool-only"].cfg().tooling == tri.tooling
    assert RX.ARMS["spiral-only"].cfg().spiral == tri.spiral


def test_trimech_is_the_union_of_the_three():
    tri = RX.ARMS["trimech"].cfg()
    assert tri.injection == RX.ARMS["inject-only"].cfg().injection
    assert tri.tooling == RX.ARMS["untool-only"].cfg().tooling
    assert tri.spiral == RX.ARMS["spiral-only"].cfg().spiral
    assert _active(tri) == {"injection", "tooling", "spiral"}


def test_web_arms_differ_from_generic_only_in_the_spiral_stage():
    """Web-Bench's short streams change the spiral stage (LLM nudge + frozen envelope) and
    nothing else -- a `-web` arm that also moved injection or untooling would make the two
    domains' numbers incomparable for reasons unrelated to stream length."""
    web, gen = RX.ARMS["trimech-web"].cfg(), RX.ARMS["trimech"].cfg()
    assert web.injection == gen.injection and web.tooling == gen.tooling
    assert web.spiral != gen.spiral
    assert web.spiral.nudge_llm is True and web.spiral.frozen_envelope == 30.0
    assert web.spiral == RX.ARMS["spiral-only-web"].cfg().spiral


def test_the_web_scaffold_arm_is_the_web_tuning_plus_the_scaffold():
    """`trimech-rtk-web` must be to `trimech-rtk` what `trimech-web` is to `trimech`.

    Web-Bench swaps every spiral-bearing arm for its `-web` tuning because a within-stream
    p90 envelope never warms up over 20 tasks. Planning plain `trimech-rtk` there would
    spiral-tune it differently from `trimech-web` -- the one arm it exists to be compared
    against -- so the comparison would measure the envelope, not the scaffold.
    """
    web, gen = RX.ARMS["trimech-rtk-web"].cfg(), RX.ARMS["trimech-rtk"].cfg()
    assert web.injection == gen.injection and web.tooling == gen.tooling
    assert web.spiral == RX.ARMS["trimech-web"].cfg().spiral
    assert RX.ARMS["trimech-rtk-web"].scaffold == RX.ARMS["trimech-rtk"].scaffold == "rtk"
    assert RX.ARMS["trimech-rtk-web"].mode == RX.ARMS["trimech-web"].mode
    # The planner must never cross the two spellings, whichever it plans. `trimech-rtk-web`
    # is currently OUT OF SCOPE on Web-Bench (see SWEEP_ARMS_WEBBENCH_OUT_OF_SCOPE) rather
    # than unavailable, so what is pinned here is the pairing, not its presence: the generic
    # arm must never be scheduled on Web-Bench, nor the -web one on SWE-bench.
    assert "trimech-rtk" not in SP.SWEEP_ARMS["webbench"]
    assert "trimech-rtk" in SP.SWEEP_ARMS["swebench"]
    assert "trimech-rtk-web" not in SP.SWEEP_ARMS["swebench"]
    assert "trimech-rtk-web" in SP.SWEEP_ARMS_WEBBENCH_OUT_OF_SCOPE


# ---- effort arms are naive plus an env overlay, nothing else ----------------------------
def test_effort_arms_are_baseline_plus_env():
    base = RX.ARMS["baseline"]
    assert base.env == {}, "naive must inherit the harness default effort, not pin one"
    for arm, level in (("baseline-medium", "medium"), ("baseline-low", "low")):
        entry = RX.ARMS[arm]
        assert entry.cfg() == base.cfg(), f"{arm} must differ from baseline only in env"
        assert entry.mode == base.mode
        assert entry.env == {"TE_EFFORT_LEVEL": level}


def test_effort_levels_are_ones_the_harness_accepts():
    """The level is validated at harness construction; catching a typo here means catching
    it offline instead of at task 0 of a paid lane."""
    accepted = {"low", "medium", "high", "xhigh", "max"}
    for entry in RX.ARMS.values():
        level = entry.env.get("TE_EFFORT_LEVEL")
        if level is not None:
            assert level in accepted, level


# ---- the plan and the registry cannot drift ---------------------------------------------
def test_every_planned_arm_is_registered():
    for domain, arms in SP.SWEEP_ARMS.items():
        for arm in arms:
            assert arm in RX.ARMS, f"{domain}: {arm}"


def test_webbench_plan_excludes_the_scaffold_arms_it_cannot_host():
    """Planning an unhostable scaffold arm for Web-Bench would enumerate cells that cannot
    run. `rtk` IS hostable there and is deliberately not on this list."""
    for arm in SP.SWEEP_ARMS_WEBBENCH_UNAVAILABLE:
        assert arm not in SP.SWEEP_ARMS["webbench"], arm
        assert arm in SP.SWEEP_ARMS["swebench"], arm


def test_webbench_scaffold_arms_agree_with_the_harness():
    """`sweep_plan` and `te_harness.webbench` must name the same hostable baselines.

    Two places decide which external baselines Web-Bench can run: the planner (which
    enumerates the cells) and the runner factory (which refuses one it cannot install). If
    they disagree, the planner either schedules cells that die at construction or silently
    drops an arm the harness would have run.
    """
    from te_harness.webbench import WEBBENCH_SCAFFOLDS

    planned = {a for a in SP.SWEEP_ARMS["webbench"] if a in RX.SCAFFOLD_ARMS}
    assert planned, "expected Web-Bench to plan at least one scaffold arm"
    for arm in planned:
        assert RX.SCAFFOLD_ARMS[arm] in WEBBENCH_SCAFFOLDS, (
            f"webbench plans {arm!r} (scaffold {RX.SCAFFOLD_ARMS[arm]!r}) but the harness "
            f"hosts only {sorted(WEBBENCH_SCAFFOLDS)}")
    # ... and the refused list is exactly the PLANNED scaffold arms whose scaffold is NOT
    # hostable. The planner's universe is the generic sweep; Codex arms are launched by the
    # run book's own loops, and the Codex Web-Bench runner's refusal of an unhostable
    # scaffold is pinned in tests/test_codex_webbench.py.
    universe = set().union(*SP.SWEEP_ARMS.values())
    refused = {a for a, sc in RX.SCAFFOLD_ARMS.items()
               if a in universe and sc not in WEBBENCH_SCAFFOLDS}
    assert set(SP.SWEEP_ARMS_WEBBENCH_UNAVAILABLE) == refused, (
        f"SWEEP_ARMS_WEBBENCH_UNAVAILABLE={sorted(SP.SWEEP_ARMS_WEBBENCH_UNAVAILABLE)} "
        f"but the harness cannot host {sorted(refused)}")


def test_scaffold_arms_agree_with_the_harness_map():
    """The arm registry and tokeneconomizer/baselines must name the same pairs. Two places
    map an arm to a scaffold, and a disagreement runs native under a scaffold's name."""
    from tokeneconomizer import baselines as scaffolds
    assert RX.SCAFFOLD_ARMS == scaffolds.ARM_SCAFFOLDS


def test_every_scaffold_arm_names_a_real_scaffold():
    from tokeneconomizer import baselines as scaffolds
    for arm, name in RX.SCAFFOLD_ARMS.items():
        assert name in scaffolds.SCAFFOLDS, f"{arm} -> {name}"
        assert scaffolds.for_arm(arm).name == name


def test_scaffold_arms_are_runnable_and_others_carry_none():
    for arm, entry in RX.ARMS.items():
        if arm in RX.SCAFFOLD_ARMS:
            assert entry.scaffold == RX.SCAFFOLD_ARMS[arm]
        else:
            assert entry.scaffold is None, f"{arm} carries an unregistered scaffold"


def test_trimech_scaffold_arms_are_trimech_plus_a_scaffold():
    """A `trimech-<scaffold>` arm must be trimech EXACTLY, differing only in the scaffold
    -- otherwise its comparison against plain trimech measures two changes at once."""
    tri = RX.ARMS["trimech"]
    for arm in ("trimech-rtk", "trimech-repomap"):
        entry = RX.ARMS[arm]
        assert entry.cfg() == tri.cfg(), arm
        assert entry.mode == tri.mode and entry.env == tri.env
        assert entry.scaffold


def test_no_trimech_serena_arm():
    """Serena's contract (navigate via its MCP tools) contends with evidence untooling
    (prune unused tool schemas), so the combination is deliberately not offered."""
    assert "trimech-serena" not in RX.ARMS
    assert "serena" in RX.ARMS and RX.ARMS["serena"].scaffold == "serena"


def test_bare_scaffold_arms_activate_no_mechanism():
    """`rtk`/`serena`/`repomap` are NAIVE plus a scaffold -- the naive+X baselines."""
    for arm in ("rtk", "serena", "repomap"):
        assert _active(RX.ARMS[arm].cfg()) == set(), arm


def test_each_domain_plans_one_naive_and_one_full_stack():
    """Every domain's plan must contain the two arms the headline comparison needs."""
    for domain, arms in SP.SWEEP_ARMS.items():
        assert "baseline" in arms, domain
        assert any(a.startswith("trimech") for a in arms), domain


# ---- cell enumeration -------------------------------------------------------------------
def test_cells_are_the_full_product_seed_major():
    arms, repos, seeds = ["a", "b"], ["r1", "r2", "r3"], [0, 1]
    got = SP.cells("swebench", arms, repos, seeds)
    assert len(got) == len(arms) * len(repos) * len(seeds)
    assert len(set(got)) == len(got), "no cell may be enumerated twice"
    # seed-major: seed 0 is complete before seed 1 begins, so an interrupted sweep leaves
    # whole seeds finished rather than every seed half-done.
    assert [s for _, _, s in got] == sorted(s for _, _, s in got)


def test_estimate_scales_with_the_committed_task_counts():
    sizes = SP.group_sizes("swebench")
    repos = sorted(sizes)
    e = SP.estimate("swebench", ["baseline", "trimech"], repos, [0, 1, 2, 3, 4], 1.0, 1.0)
    assert e["n_task_runs"] == sum(sizes.values()) * 2 * 5
    assert e["usd"] == e["n_task_runs"] and e["unknown_repos"] == []


def test_estimate_flags_a_repo_outside_the_workload_record():
    e = SP.estimate("swebench", ["baseline"], ["not/a-repo"], [0], 1.0, 1.0)
    assert e["unknown_repos"] == ["not/a-repo"] and e["n_task_runs"] == 0


# ---- the planner must count what the driver runs ----------------------------------------
def test_group_sizes_match_the_manifest_the_driver_filters_to():
    """`main_sweep.sh` decides a cell is finished by comparing its stored row.json count
    against `group_sizes`, while the driver runs exactly the ids `workloads/<domain>.json`
    names. If those two numbers disagree by even one, SKIP_DONE never fires for that group
    and every resume silently re-runs completed, already-paid-for cells.

    This is not hypothetical: `workloads.task_counts()` is the PROVENANCE record and
    deliberately does not subtract SWEBENCH_EXCLUDED, so reading it here made the planner
    expect 32 scikit-learn tasks for a run that executes 60 (31 published-minus-
    excluded, plus the 29-id de-saturation extension).
    """
    import json
    for domain in SP.SWEEP_ARMS:
        path = os.path.join(_REPO, "workloads", f"{domain}.json")
        with open(path, encoding="utf-8") as fh:
            groups = json.load(fh)["groups"]
        assert SP.group_sizes(domain) == {g: len(ids) for g, ids in groups.items()}, domain


def test_group_sizes_exclude_what_a_new_run_must_skip():
    """The exclusion and extension tables are both forward-looking: an excluded id must not
    be counted, an added one must be, and task_counts() must still report the historical
    size either way."""
    from tokeneconomizer import workloads
    sizes = SP.group_sizes("swebench")
    for iid in workloads.SWEBENCH_EXCLUDED:
        group = next(g for g, ids in workloads.SWEBENCH_INSTANCES.items() if iid in ids)
        added = len(workloads.SWEBENCH_ADDED.get(group, ()))
        assert sizes[group] == len(workloads.SWEBENCH_INSTANCES[group]) - 1 + added
        assert workloads.task_counts("swebench")[group] == \
            len(workloads.SWEBENCH_INSTANCES[group])
    for group, ids in workloads.SWEBENCH_ADDED.items():
        skipped = sum(1 for i in workloads.SWEBENCH_INSTANCES[group]
                      if i in workloads.SWEBENCH_EXCLUDED)
        assert sizes[group] == (len(workloads.SWEBENCH_INSTANCES[group])
                                - skipped + len(ids))
        assert workloads.task_counts("swebench")[group] == \
            len(workloads.SWEBENCH_INSTANCES[group])


def test_mode_is_manual4_exactly_when_injection_is_enabled():
    """`mode` is written per arm, but it is not free: manual4 IS the micro-skill prefix.

    An injection arm filed as `standalone` would run without the prefix the mechanism needs;
    a non-injection arm filed as `manual4` would carry a prefix nothing uses, and both would
    still produce plausible-looking rows. The mode also lands in the run-dir name and in
    aggregate.py's `mode` column, so a drifted value silently mislabels a whole cell.

    This is what makes `trimech-rtk` / `trimech-repomap` manual4: they contain trimech,
    which contains injection -- exactly like `trimech` itself, which they are siblings of.
    """
    wrong = [(name, a.mode, a.cfg().injection.enabled)
             for name, a in RX.ARMS.items()
             if (a.mode == "manual4") != bool(a.cfg().injection.enabled)]
    assert not wrong, (
        "mode must be 'manual4' exactly when injection is enabled, else 'standalone': "
        + ", ".join(f"{n} is {m} with injection={i}" for n, m, i in wrong))



# ---- Web-Bench's out-of-scope arms are UNPLANNED, not unavailable -----------------------
# Web-Bench carries the generalization claim; the decomposition is answered on SWE-bench at
# 12 arms x 6 repos x 5 seeds. That is a scope decision, and the failure it must not become
# is silently claiming the harness cannot run those arms -- someone would then never try.

def test_webbench_out_of_scope_arms_are_real_arms_that_simply_are_not_planned():
    for arm in SP.SWEEP_ARMS_WEBBENCH_OUT_OF_SCOPE:
        assert arm in RX.ARMS, f"{arm} is out of scope but not even registered"
        assert arm not in SP.SWEEP_ARMS["webbench"], arm
        # NOT unavailable: unavailability is about what the harness cannot host.
        assert arm not in SP.SWEEP_ARMS_WEBBENCH_UNAVAILABLE, (
            f"{arm} is listed both as out-of-scope and as unhostable; those mean "
            f"different things and a reader would not know which applies")


def test_the_webbench_sweep_is_the_generalization_set():
    """4 core arms + the one hostable scaffold. The core four are what make a cost saving
    distinguishable from turning the effort knob down; rtk is the prior-art comparison."""
    assert set(SP.SWEEP_ARMS["webbench"]) == {
        "baseline", "baseline-medium", "baseline-low", "trimech-web", "rtk"}


def test_a_scaffold_arm_out_of_scope_on_webbench_is_still_hostable():
    """`trimech-rtk-web` is dropped for scope, not capability -- so if it is ever added
    back, the harness must already be able to install what it names."""
    from te_harness.webbench import WEBBENCH_SCAFFOLDS
    assert RX.SCAFFOLD_ARMS["trimech-rtk-web"] in WEBBENCH_SCAFFOLDS


# ---- the Codex sibling of trimech -------------------------------------------------------
def test_codex_trimech_is_the_union_of_the_three_codex_arms():
    """`codex-trimech` must be its three single-mechanism arms co-activated, unretuned.

    Same contract as `test_trimech_is_the_union_of_the_three`, and the same reason: an arm
    that retuned one of its mechanisms would not be a 3-mech point against three 1-mech
    ablations, it would be a fourth configuration.
    """
    tri = RX.ARMS["codex-trimech"].cfg()
    assert tri.injection == RX.ARMS["codex-inject"].cfg().injection
    assert tri.tooling == RX.ARMS["codex-untool"].cfg().tooling
    assert tri.spiral == RX.ARMS["codex-spiral-defensive"].cfg().spiral
    assert _active(tri) == {"injection", "tooling", "spiral"}
    assert RX.ARMS["codex-trimech"].mode == "manual4"      # injection is on


def test_the_codex_spiral_arms_have_the_shape_the_injection_arms_have():
    """Plain name = the configuration with a live A/B; the superseded one keeps a name.

    Settled twice, the same way. `codex-inject` is the composed `envfacts+distiller`
    profile with `codex-inject-envfacts` as the named ablation and `codex-inject-distiller`
    as the alias its stored cell was filed under. `codex-spiral` is the DEFENSIVE profile
    with `codex-spiral-postgreen` as the named ablation and `codex-spiral-defensive` as the
    alias its stored cell was filed under. The alternative -- leaving the plain name on the
    shipped post-green rule -- puts a configuration that replays as 8 fires in 204 terra
    tasks (net -0.26%) behind the name a plan reaches for, which is a baseline wearing the
    mechanism's name.
    """
    S = lambda n: RX.ARMS[n].cfg().spiral
    assert S("codex-spiral").codex_spiral_profile == "defensive"
    assert S("codex-spiral-defensive") == S("codex-spiral")          # alias
    assert S("codex-spiral-postgreen").codex_spiral_profile == "post-green"
    assert S("codex-spiral-postgreen") != S("codex-spiral")
    # the generic guard stays off on all of them: the Codex controller is what runs
    for n in ("codex-spiral", "codex-spiral-postgreen", "codex-spiral-defensive"):
        assert S(n).codex_spiral and not S(n).guard and not S(n).nudge
    from tokeneconomizer.mechanisms import codex_spiral as cs
    assert cs.CODEX_DEFAULT_PROFILE == "defensive"
    assert cs.CODEX_SHIPPED_PROFILE == "post-green"


def test_the_codex_family_relates_to_codex_trimech_as_the_generic_one_does_to_trimech():
    """The whole point of the Codex arms is that they are the SAME experiment on a second
    agent, so the two families must have the same shape -- not merely each be internally
    consistent. Checked property by property rather than by eye, because the asymmetry that
    actually occurred was invisible in the configs: every structural check passed while the
    plain name `codex-spiral` carried a spiral `codex-trimech` did not compose.
    """
    GEN = ("trimech", "inject-only", "untool-only", "spiral-only")
    CDX = ("codex-trimech", "codex-inject", "codex-untool", "codex-spiral")
    for tri, inj, unt, spi in (GEN, CDX):
        t = RX.ARMS[tri]
        c = t.cfg()
        # 1. trimech is exactly the union of the three single-mechanism arms
        assert c.injection == RX.ARMS[inj].cfg().injection
        assert c.tooling == RX.ARMS[unt].cfg().tooling
        assert c.spiral == RX.ARMS[spi].cfg().spiral
        assert _active(c) == {"injection", "tooling", "spiral"}
        # 2. each single-mechanism arm activates exactly one
        for name, mech in ((inj, "injection"), (unt, "tooling"), (spi, "spiral")):
            assert _active(RX.ARMS[name].cfg()) == {mech}, name
        # 3. the mode label follows injection, and nothing carries a scaffold or env override
        assert t.mode == RX.ARMS[inj].mode == "manual4"
        assert RX.ARMS[unt].mode == RX.ARMS[spi].mode == "standalone"
        for name in (tri, inj, unt, spi):
            assert not RX.ARMS[name].scaffold and not (RX.ARMS[name].env or {}), name
        # 4. the three appear before trimech in the canonical order
        from tokeneconomizer.arms import ARM_ORDER
        assert max(ARM_ORDER.index(n) for n in (inj, unt, spi)) < ARM_ORDER.index(tri)


def test_codex_trimech_is_a_paid_arm():
    """The proposer bills, so the endpoint preflight must fire before task 0 rather than
    the first paid call failing 40 tasks into a live lane."""
    from tokeneconomizer.config import needs_optimizer
    assert needs_optimizer(RX.ARMS["codex-trimech"].cfg()) is True
    assert RX.ARMS["codex-trimech"].cfg().injection.paid is True


# ---- the docs must name the arms the registry actually has -------------------------------
# The arms the Codex run book PUBLISHES -- one per mechanism, plus the agent-agnostic naive
# spine, the all-three arm and its two scaffold variants, and the three scaffolds this lane
# hosts (each through the channel Codex has). The registry knows more
# names than this (two aliases stored cells are filed under, and four ablations), and that is
# deliberate: they still resolve so an old cell can be re-run or re-read, but they are not
# offered, because a catalog carrying two spellings of one mechanism is a catalog in which an
# ablation can be reported as the mechanism.
CODEX_PUBLISHED_ARMS = frozenset({
    "baseline", "baseline-medium", "baseline-low", "repomap", "rtk", "serena",
    "codex-inject", "codex-untool", "codex-spiral", "codex-trimech", "codex-trimech-rtk",
    "codex-trimech-repomap",
})


def _doc(name):
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(repo, "docs", name)).read()


def _section(text, start, end=None):
    i = text.index(start)
    return text[i:text.index(end, i + 1)] if end else text[i:]


def _table_arms(sec):
    """The arm names a run-book table lists, one per row, in order."""
    import re
    return re.findall(r"^\| `([a-z0-9-]+)` \|", sec, re.M)


def _loop_arms(sec):
    """The arm names a run-book command block launches, through `ARMS=` or a `for ARM` loop."""
    import re
    names = set()
    for group in re.findall(r'ARMS="([a-z0-9\- ]+)"', sec):
        names |= set(group.split())
    for group in re.findall(r"for ARM in ([a-z0-9\- \\\n]+?);", sec):
        names |= set(group.replace("\\", " ").split())
    return names


# The run book's lanes: the Claude Code / Copilot section and the Codex section, each split
# into one swebench and one webbench subsection.
_GENERIC = ("## 4. Claude Code and Copilot", "## 5. Codex")
_CODEX = ("## 5. Codex", "## 6.")


def test_the_codex_lane_publishes_exactly_one_arm_per_mechanism():
    """Checked both ways, because both failures have happened here.

    Every published arm must appear in the catalog -- it drifted twice, once missing the
    effort baselines and once missing `repomap`. And every arm the catalog names must be
    registered and published: a doc promising an arm nobody implemented is one failure, a doc
    quietly re-offering a dropped ablation beside the mechanism it ablates is the other.
    """
    codex = _section(_doc("RUNNING.md"), *_CODEX)
    sb = _section(codex, "### swebench", "### webbench")
    named = set(_table_arms(sb))
    assert named == CODEX_PUBLISHED_ARMS, (
        f"catalog names {sorted(named)}, published set is {sorted(CODEX_PUBLISHED_ARMS)}")
    assert _loop_arms(sb) == named, "the swebench command launches a different arm set"
    wb = _section(codex, "### webbench")
    assert _loop_arms(wb) == set(_table_arms(wb)), "the webbench command and table disagree"
    invented = sorted(n for n in named | set(_table_arms(wb)) if n not in RX.ARMS)
    assert not invented, f"the catalog names arms that do not exist: {invented}"
    cfgs = {n: RX.ARMS[n].cfg() for n in named}
    one = lambda mech: sorted(
        n for n, c in cfgs.items()
        if getattr(c, mech).enabled and not any(
            getattr(c, m).enabled for m in ("injection", "tooling", "spiral") if m != mech))
    assert one("injection") == ["codex-inject"]
    assert one("tooling") == ["codex-untool"]
    assert one("spiral") == ["codex-spiral"]


def test_every_arm_the_generic_lanes_launch_is_registered_and_planned():
    """The Claude Code and Copilot lanes launch through `main_sweep.sh`, which plans cells from
    `sweep_plan.SWEEP_ARMS`. An arm named in the book but absent from the plan gets "the plan is
    empty" at launch; one absent from the registry is a doc promising something nobody
    implemented."""
    generic = _section(_doc("RUNNING.md"), *_GENERIC)
    sb = set(_table_arms(_section(generic, "### swebench", "### webbench")))
    wb = set(_table_arms(_section(generic, "### webbench", "### The frontier lane")))
    assert sb, "no swebench arms parsed"
    for arm in sb | wb:
        assert arm in RX.ARMS, f"the run book lists {arm!r}, which is not registered"
    # the swebench catalog IS the planned set, and so is the webbench one
    assert sb == set(SP.SWEEP_ARMS["swebench"]), (
        f"swebench catalog {sorted(sb)} != plan {sorted(SP.SWEEP_ARMS['swebench'])}")
    assert wb == set(SP.SWEEP_ARMS["webbench"]), (
        f"webbench catalog {sorted(wb)} != plan {sorted(SP.SWEEP_ARMS['webbench'])}")
    frontier = _loop_arms(_section(generic, "### The frontier lane"))
    for arm in frontier:
        assert arm in RX.ARMS, f"the frontier lane launches unregistered arm {arm!r}"


def test_each_lane_splits_its_commands_by_domain():
    """Both lanes present SWE-bench and Web-Bench as two separate subsections, each with its
    own table and its own command, so a reader can follow it top to bottom.

    The runner check is the other half -- a Web-Bench command carrying the SWE-bench factory
    runs the wrong lifecycle against the right ids, which the launcher refuses but a reader
    would have copied.
    """
    import re
    text = _doc("RUNNING.md")
    for lane in (_GENERIC, _CODEX):
        sec = _section(text, *lane)
        heads = [(m.start(), m.group(0)) for m in
                 re.finditer(r"^### (swebench|webbench) — \d+ arms$", sec, re.M)]
        kinds = [("webbench" if "webbench" in h else "swebench") for _, h in heads]
        assert kinds == ["swebench", "webbench"], f"{lane[0]}: got {heads}"
        for i, (start, head) in enumerate(heads):
            body = sec.index("\n", start)
            nxt = re.search(r"^#{2,3} ", sec[body:], re.M)
            stop = body + nxt.start() if nxt else len(sec)
            sub = sec[start:stop]
            domain = kinds[i]
            entries = _table_arms(sub)
            assert entries and len(entries) == len(set(entries)), f"{lane[0]}/{domain}"
            claimed = int(re.search(r"(\d+) arms", head).group(1))
            assert claimed == len(entries), f"{head!r} but {len(entries)} entries"
            wrong = "swebench" if domain == "webbench" else "webbench"
            assert f"te_harness.{domain}:make_runner" in sub, f"{lane[0]}/{domain}"
            assert f"te_harness.{wrong}:make_runner" not in sub, f"{lane[0]}/{domain}"


def test_the_harness_scaffold_table_names_every_arm_that_carries_each_scaffold():
    """docs/HARNESS.md maps scaffold -> arms. It is the table a reader consults to find out
    which arms need `aider` on the host or the staged rtk binary, so an arm missing from it
    reads as needing nothing. It went stale twice in one day, both times when a Codex
    sibling arm was added, so pin it against the registry rather than against a copy."""
    import re
    from tokeneconomizer import baselines
    text = _doc("HARNESS.md")
    table = text[text.index("| Scaffold | Arms |"):]
    table = table[:table.index("\n\n")]
    listed = {}
    for line in table.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4 or not cells[1].startswith("`"):
            continue
        scaffold = cells[1].strip("`")
        listed[scaffold] = set(re.findall(r"`([a-z0-9-]+)`", cells[2]))
    assert listed, "no scaffold rows parsed out of docs/HARNESS.md"
    for scaffold, arms in listed.items():
        real = {a for a, s in baselines.ARM_SCAFFOLDS.items() if s == scaffold}
        assert arms == real, (
            f"HARNESS.md lists {scaffold!r} arms as {sorted(arms)}, "
            f"but ARM_SCAFFOLDS says {sorted(real)}")


def test_the_published_spiral_arm_is_the_defensive_profile():
    """The one that was approved, and the one both live cells ran.

    `codex-spiral` must be the defensive profile -- every deterministic spiral signature as a
    same-thread nudge, two per task -- and must never touch reasoning effort, which the owner
    ruled out on 2026-09-12. The superseded post-green rule keeps a registry name but is not
    what the plain one runs.
    """
    from tokeneconomizer.mechanisms import codex_spiral as cs
    sp = RX.ARMS["codex-spiral"].cfg().spiral
    assert sp.codex_spiral_profile == "defensive" == cs.CODEX_DEFAULT_PROFILE
    assert cs.profile_version("defensive") == "codex-native-spiral-defensive-v1"
    pol = cs.profile_policy("defensive", 20)
    assert pol.nudge_budget == 2 and pol.runaway_enabled
    assert not pol.effort_rescue_enabled and not pol.resume_effort
    assert RX.ARMS["codex-spiral-defensive"].cfg().spiral == sp


def test_the_codex_lane_uses_the_whole_evaluated_workload_not_one_repo():
    """`codex_ab.sh` defaults to one repository, so the run book must name them all or a
    6-repo sweep quietly becomes a 1-repo one."""
    from tokeneconomizer import workloads
    codex = _section(_doc("RUNNING.md"), *_CODEX)
    for group in workloads.task_counts("swebench"):
        assert group in codex, f"{group} is in the evaluated workload but not in the run book"
    for project in workloads.task_counts("webbench"):
        assert project in codex, f"{project} is an evaluated project but not in the run book"


def test_codex_trimech_rtk_is_codex_trimech_with_the_scaffold_and_nothing_else():
    """The same contract `trimech-rtk` has with `trimech`: identical mechanism parameters,
    the scaffold named and nothing retuned -- otherwise the arm would measure a fourth
    configuration rather than the scaffold's marginal effect on the stack."""
    a, b = RX.ARMS["codex-trimech-rtk"], RX.ARMS["codex-trimech"]
    assert a.cfg() == b.cfg() and a.mode == b.mode == "manual4"
    assert a.scaffold == "rtk" and b.scaffold is None
    from tokeneconomizer import baselines
    assert baselines.ARM_SCAFFOLDS["codex-trimech-rtk"] == "rtk"
    assert RX.SCAFFOLD_ARMS["codex-trimech-rtk"] == "rtk"
    # and the plain scaffold arm is agent-agnostic: no mechanism, just the scaffold
    r = RX.ARMS["rtk"]
    assert _active(r.cfg()) == set() and r.scaffold == "rtk"


def test_codex_trimech_repomap_is_codex_trimech_with_the_scaffold_and_nothing_else():
    """The contract `trimech-repomap` has with `trimech`, on the Codex family: identical
    mechanism parameters, the scaffold named, nothing retuned."""
    a, b = RX.ARMS["codex-trimech-repomap"], RX.ARMS["codex-trimech"]
    assert a.cfg() == b.cfg() and a.mode == b.mode == "manual4"
    assert a.scaffold == "repomap" and b.scaffold is None
    from tokeneconomizer import baselines
    assert baselines.ARM_SCAFFOLDS["codex-trimech-repomap"] == "repomap"
    assert RX.SCAFFOLD_ARMS["codex-trimech-repomap"] == "repomap"
    r = RX.ARMS["repomap"]
    assert _active(r.cfg()) == set() and r.scaffold == "repomap"


def test_every_generic_trimech_scaffold_arm_has_exactly_one_codex_sibling():
    """`trimech-<s>` <-> `codex-trimech-<s>` for every scaffold the generic lane combines with
    trimech, carrying the same scaffold on both sides -- and nothing more on either: the
    generic lane refuses `trimech-serena`, so a `codex-trimech-serena` would be a Codex-only
    combination with no generic twin to compare against. `-web` is Web-Bench's spiral
    tuning of the same scaffold arm, not a scaffold of its own."""
    from tokeneconomizer import baselines
    generic = {a[len("trimech-"):]: s for a, s in baselines.ARM_SCAFFOLDS.items()
               if a.startswith("trimech-") and not a.endswith("-web")}
    codex = {a[len("codex-trimech-"):]: s for a, s in baselines.ARM_SCAFFOLDS.items()
             if a.startswith("codex-trimech-")}
    assert generic == codex == {"rtk": "rtk", "repomap": "repomap"}

