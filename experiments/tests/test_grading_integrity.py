"""A row must never claim a grade the eval script did not produce.

Two defects sat behind one symptom here, and both were silent:

  * The task container was named `te-<instance>-<arm>`, carrying no agent, model or seed,
    while both SWE-bench runners open with `docker rm -f <name>`. Two lanes on the same
    (instance, arm) -- exactly what you get running the Claude and Copilot lanes for
    the same arm at once -- resolved to one name, and the second lane deleted the first
    one's container mid-grade.
  * `_grade` then wrote an empty `eval.log` and handed it to `get_eval_report`, which
    scored the absent APPLY_PATCH_PASS marker as `patch_successfully_applied: false,
    resolved: false`. The lane exited 0. The row was indistinguishable from a model that
    tried the task and missed.

So the container name must be unique per task-run, and a grading exec that did not run
must be reported as an absence rather than a miss -- and excluded from the denominator of
any resolve rate, because a task nobody evaluated is not a task the agent failed.
"""
import importlib.util
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGG = _load("_aggregate", os.path.join(_REPO, "scripts", "aggregate.py"))
RX = _load("_run_experiment_gi", os.path.join(_REPO, "scripts", "run_experiment.py"))


class _NameOnly:
    """Just enough of a runner to exercise the naming rule without Docker or a dataset."""

    def __init__(self, agent, model):
        self.agent = agent
        self.model = model

    _container_name = None      # bound below from the real implementation


def _namer():
    import te_harness.swebench as sw
    _NameOnly._container_name = sw.ClaudeSwebenchRunner._container_name
    return _NameOnly


# ---- container naming ----------------------------------------------------------------

def test_container_name_differs_by_agent():
    """A Claude lane and a Copilot lane must not share a container name."""
    R = _namer()
    a = R("claude", "claude-opus-4.8")._container_name(
        "django__django-10097", "baseline", "/r/claude/baseline/django_s0/x")
    b = R("copilot", "claude-opus-4.8")._container_name(
        "django__django-10097", "baseline", "/r/copilot/baseline/django_s0/x")
    assert a != b, f"agent must change the container name; both were {a!r}"


def test_container_name_differs_by_run_dir():
    """Seed and timestamp live in the run dir, so distinct task-runs get distinct names."""
    R = _namer()
    mk = R("claude", "claude-opus-4.8")._container_name
    s0 = mk("sympy__sympy-11618", "trimech", "/r/t/trimech/sympy_s0/20260101T000000Z__000__x")
    s1 = mk("sympy__sympy-11618", "trimech", "/r/t/trimech/sympy_s1/20260101T000000Z__000__x")
    assert s0 != s1


def test_container_name_is_docker_legal_and_readable():
    R = _namer()
    n = R("claude", "claude-opus-4.8")._container_name(
        "scikit-learn__scikit-learn-25102", "trimech-repomap", "/r/t/a/b/c")
    assert len(n) <= 63, f"docker caps names at 63 chars; got {len(n)}"
    assert n[0].isalnum(), n
    assert all(c.isalnum() or c in "_.-" for c in n), n
    # The readable stem survives, so `docker ps` still identifies the cell.
    assert n.startswith("te-scikit-learn-scikit-learn-25102-trimech-repomap"[:50]), n


def test_container_name_is_stable_for_one_task_run():
    """Same inputs -> same name, so cleanup and the grading exec address one container."""
    R = _namer()
    mk = R("claude", "claude-opus-4.8")._container_name
    args = ("django__django-10097", "baseline", "/r/t/baseline/django_s0/run")
    assert mk(*args) == mk(*args)


# ---- not_graded accounting ------------------------------------------------------------

def test_eval_exec_failure_is_flagged_not_graded():
    ungraded, reason = AGG.not_graded(
        {"resolved": False,
         "eval_report": {"eval_exec_failed": True, "eval_exec_rc": 1,
                         "eval_exec_error": "No such container"}})
    assert ungraded is True
    assert "No such container" in reason


def test_grading_error_is_flagged_not_graded():
    ungraded, reason = AGG.not_graded(
        {"resolved": False, "eval_report": {"grading_error": "KeyError"}})
    assert ungraded is True
    assert "KeyError" in reason


def test_a_real_miss_is_not_flagged():
    """A graded-and-missed row must stay in the denominator -- that is a real result."""
    ungraded, _ = AGG.not_graded(
        {"resolved": False,
         "eval_report": {"patch_exists": True, "patch_successfully_applied": True,
                         "resolved": False}})
    assert ungraded is False


def test_a_resolve_is_not_flagged():
    ungraded, _ = AGG.not_graded({"resolved": True, "eval_report": {"resolved": True}})
    assert ungraded is False


def test_not_graded_rows_leave_the_resolve_denominator():
    """The whole point: an ungraded row must not depress a cell's resolve rate."""
    rows = [
        {"arm": "trimech", "repo": "django", "seed": 0, "resolved": True,
         "is_error": False, "num_turns": 5, "cost_effective_tokens": 100.0,
         "not_graded": False},
        {"arm": "trimech", "repo": "django", "seed": 0, "resolved": False,
         "is_error": False, "num_turns": 4, "cost_effective_tokens": 100.0,
         "not_graded": True},
    ]
    a = AGG.summarize(rows)[("trimech", "django", "0")]
    assert a["n"] == 2, "the row still exists and still cost money"
    assert a["graded"] == 1, "only one row was actually graded"
    assert a["resolved"] == 1
    assert a["ungraded"] == 1
    assert a["resolved"] / a["graded"] == 1.0, (
        "a lost container must not read as a failed task")


# ---- mechanism overhead must survive into row.json ------------------------------------

def test_every_overhead_key_is_summed_by_the_ledger():
    """OVERHEAD_KEYS is the contract; ledger.overhead must honour all of it."""
    from tokeneconomizer import ledger
    row = {k: 100 for k in ledger.OVERHEAD_KEYS}
    assert ledger.overhead(row) == 100 * len(ledger.OVERHEAD_KEYS)


def test_stream_only_overhead_is_copied_into_the_row():
    """The driver must lift overhead a mechanism reported on `srow` into `row`.

    `tooling.after_task` writes the per-task tool judge's spend to the STREAM row only,
    and aggregate.py reads nothing but row.json -- so untool-only and every trimech
    variant had that line item silently dropped from cost_effective_tokens. Measured on a
    6-task sympy cell: 11,244 tokens, 4.2% of the cell's reported cost, and one-directional
    because a baseline runs no judge.
    """
    from tokeneconomizer import ledger
    row = RX.lift_overhead({"tokens_total": 1000},
                           {"untool_judge_tokens": 5171, "resolved": True})
    assert row.get("untool_judge_tokens") == 5171
    assert ledger.overhead(row) == 5171, "the judge's spend must reach the cost basis"


def test_the_copy_never_clobbers_a_value_the_runner_already_set():
    """spiral_judge_tokens is set on `row` by the harness; srow may hold a stale zero."""
    row = RX.lift_overhead({"spiral_judge_tokens": 106916}, {"spiral_judge_tokens": 0})
    assert row["spiral_judge_tokens"] == 106916


def test_lift_overhead_covers_every_declared_overhead_key():
    """Keyed off OVERHEAD_KEYS, so a new overhead field cannot reintroduce the gap."""
    from tokeneconomizer import ledger
    srow = {k: 7 for k in ledger.OVERHEAD_KEYS}
    row = RX.lift_overhead({}, srow)
    assert ledger.overhead(row) == 7 * len(ledger.OVERHEAD_KEYS)


def test_lift_overhead_ignores_non_overhead_stream_fields():
    """It must not smuggle arbitrary stream telemetry into row.json."""
    row = RX.lift_overhead({}, {"untool_judge_dropped": ["WebFetch"], "resolved": True})
    assert "untool_judge_dropped" not in row
    assert "resolved" not in row


def test_a_grading_stall_flags_the_row_instead_of_ending_the_stream():
    """Patch extraction gets the guard container start already has.

    `_graded_diff` is a `docker exec ... git diff` under a 300 s cap. On 2026-09-13 one
    stalled at task 57 of a 60-task baseline and the TimeoutExpired propagated out of
    `run_task`, ending the cell with four tasks unrun after 65 minutes of paid work -- the
    exact failure the container-start guard exists to prevent, one step later in the same
    function. The agent's turn is already bought by then, so the row must survive, say what
    happened, and let the stream continue.
    """
    import inspect, re
    from te_harness import swebench
    src = inspect.getsource(swebench.ClaudeSwebenchRunner.run_task)
    body = src[src.index("patch, resolved, report"):]
    guarded = body[:body.index("row.update(self._row_from_segment")]
    assert "self._graded_diff(name)" in guarded and "self._grade(" in guarded
    assert re.search(r"except \(subprocess\.TimeoutExpired, RuntimeError\)", guarded)
    # the row is kept, flagged, and filterable -- not silently scored as a clean miss
    for field in ('row["is_error"] = True', '"grading_failed"', 'row["grading_error"]'):
        assert field in guarded, field
    # and the failure is logged, so a filtered-out row can be traced back to a lane log
    assert "GRADING FAILED" in guarded


# ---- the project env the agent shares with the grader ---------------------------------
# The official images install the instance's dependencies into the `testbed` conda env the
# grader runs in; the agent's shells get that env by a PATH prepend (swebench
# `_activate_project_env`), so the agent tests its fix against what the grader will use.

def test_choose_project_env_prefers_testbed_then_the_only_env():
    from te_harness.swebench import choose_project_env
    envs = "/opt/miniconda3/envs"
    assert choose_project_env([f"{envs}/other/bin", f"{envs}/testbed/bin"]) == \
        f"{envs}/testbed/bin"
    assert choose_project_env([f"{envs}/py39/bin"]) == f"{envs}/py39/bin"
    assert choose_project_env([f"{envs}/a/bin", f"{envs}/b/bin"]) is None
    assert choose_project_env([]) is None


def test_activation_env_prepends_and_names_the_env():
    from te_harness.swebench import activation_env
    env = activation_env("/opt/miniconda3/envs/testbed/bin", "/opt/miniconda3/bin:/usr/bin")
    assert env["PATH"] == "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/bin"
    assert env["CONDA_PREFIX"] == "/opt/miniconda3/envs/testbed"
    assert env["CONDA_DEFAULT_ENV"] == "testbed"
    assert activation_env("/x/envs/e/bin", "")["PATH"] == "/x/envs/e/bin"
