"""The vendor-dollar column: read the key the runners actually write, and flag its absence.

Two defects, both of which made a cost comparison quietly wrong rather than loudly broken:

  * `aggregate.py` emitted `cost_usd` from `d.get("cost_usd")`, but every runner writes the
    figure as `cost_usd_vendor`. The column was therefore EMPTY in every tree this repo
    generates, while looking like a column that simply had no data yet.

  * A row whose agent reported no `total_cost_usd` stored None and was marked by nothing:
    `usage_incomplete` stays False (the token breakdown is complete and the effective-token
    basis is exact) and the task grades normally, so the row reads as healthy. Summing the
    dollar column then under-counts, because the row's tokens are in the denominator while
    its dollars are not -- and because the misses cluster on the longest trajectories, the
    error lands on the most expensive tasks.

These tests are offline: they call the two pure functions with synthetic rows.
"""
import importlib.util
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGGREGATE = os.path.join(_REPO, "scripts", "aggregate.py")


def _agg():
    spec = importlib.util.spec_from_file_location("_aggregate", _AGGREGATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ran(**over):
    """A row that demonstrably ran: it resolved and it burned tokens."""
    row = {"agent": "claude", "resolved": True, "tokens_total": 400_000,
           "input_tokens": 1_600, "patch_bytes": 900}
    row.update(over)
    return row


# --- vendor_usd: the source key ----------------------------------------------------

def test_vendor_usd_reads_the_key_the_runners_write():
    assert _agg().vendor_usd(_ran(cost_usd_vendor=0.2239)) == 0.2239


def test_vendor_usd_falls_back_to_the_legacy_curated_name():
    # The older curated exports carry the figure as plain `cost_usd`; those trees must
    # keep working.
    assert _agg().vendor_usd({"cost_usd": 1.5}) == 1.5


def test_vendor_usd_prefers_the_runner_key_when_both_are_present():
    assert _agg().vendor_usd({"cost_usd_vendor": 2.0, "cost_usd": 9.9}) == 2.0


def test_vendor_usd_is_blank_not_zero_when_absent():
    # "" and 0.0 must not be confused: one is an unknown, the other is a measurement.
    assert _agg().vendor_usd({"cost_usd_vendor": None}) == ""
    assert _agg().vendor_usd({}) == ""


def test_vendor_usd_keeps_a_genuine_zero():
    assert _agg().vendor_usd({"cost_usd_vendor": 0.0}) == 0.0


def test_vendor_usd_rejects_a_bool():
    # bool is an int subclass; True must not read as $1.
    assert _agg().vendor_usd({"cost_usd_vendor": True}) == ""


# --- vendor_cost_missing: the flag -------------------------------------------------

def test_flags_a_row_that_ran_without_a_figure():
    missing, reason = _agg().vendor_cost_missing(_ran(cost_usd_vendor=None))
    assert missing is True and "total_cost_usd" in reason


def test_does_not_flag_a_row_that_has_a_figure():
    assert _agg().vendor_cost_missing(_ran(cost_usd_vendor=0.5))[0] is False


def test_does_not_flag_an_agent_that_never_reports_dollars():
    # Copilot meters premium requests and Codex bills a subscription: null is the
    # documented normal case there (docs/COPILOT.md), not a defect.
    for agent in ("copilot", "codex"):
        row = _ran(agent=agent, cost_usd_vendor=None)
        assert _agg().vendor_cost_missing(row)[0] is False, agent


def test_does_not_flag_a_task_that_never_ran():
    # An UNREACHED Web-Bench row: no verdict, no patch, no tokens. It has no cost because
    # nothing happened, which is not the same as a lost measurement.
    row = {"agent": "claude", "resolved": False, "tokens_total": 0,
           "input_tokens": 0, "patch_bytes": None, "cost_usd_vendor": None}
    assert _agg().vendor_cost_missing(row)[0] is False


def test_flags_an_unresolved_row_that_still_burned_tokens():
    # The expensive case: a long trajectory that failed. Dropping it understates the arm.
    row = _ran(resolved=False, patch_bytes=0, cost_usd_vendor=None)
    assert _agg().vendor_cost_missing(row)[0] is True


def test_flag_is_independent_of_usage_incomplete():
    # The row's token accounting is complete -- that is exactly why it needs its own flag.
    agg = _agg()
    row = _ran(cost_usd_vendor=None)
    assert agg.usage_incomplete(row)[0] is False
    assert agg.vendor_cost_missing(row)[0] is True


def test_both_columns_are_emitted():
    for col in ("cost_usd", "vendor_cost_missing", "vendor_cost_missing_reason"):
        assert col in _agg().FIELDS, col
