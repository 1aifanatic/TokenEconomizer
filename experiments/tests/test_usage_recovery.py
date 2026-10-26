"""Usage recovery: a rollout killed mid-stream must not be recorded as free.

Claude Code reports its token breakdown only in the stream's terminal `result` event, and
both of `_segment`'s break paths -- a spiral guard stop and TE_TASK_TIMEOUT -- kill the CLI
before it arrives. A segment that returned `{}` would make `_row_from_result` write a row of
zeros for a rollout that really burned hundreds of thousands of tokens over tens of turns,
so the segment rebuilds its usage from the stream it did see. Only economized arms install
a guard, so the understatement would be one-directional and flatter exactly the arms under
test.

These tests are offline: they exercise the pure accounting functions with synthetic stream
events, no Docker and no agent. `te_harness` imports nothing heavy at module level (the
official SWE-bench grader is imported lazily), so the suite stays runnable from a fresh
clone with no benchmark data.
"""
from te_harness.swebench import _UsageRecovery, _merge_usage, _row_from_result


def _assistant(mid, inp, cw, cr, out):
    """One `assistant` stream event in Claude Code's stream-json schema."""
    return {"type": "assistant",
            "message": {"id": mid,
                        "usage": {"input_tokens": inp,
                                  "cache_creation_input_tokens": cw,
                                  "cache_read_input_tokens": cr,
                                  "output_tokens": out}}}


def _feed(events):
    rec = _UsageRecovery()
    for ev in events:
        rec.observe(ev)
    return rec


# ---- turns and tokens recovered from the stream --------------------------------------
def test_killed_stream_is_not_recorded_as_free():
    row = _row_from_result(_feed([
        _assistant("m1", 2, 4000, 3000, 5),
        _assistant("m2", 2, 500, 7000, 9),
    ]).synthesize("spiral_guard_stop"))
    assert row["input_tokens"] == 4
    assert row["cache_creation_input_tokens"] == 4500
    assert row["cache_read_input_tokens"] == 10000
    assert row["tokens_total"] > 0, "a killed rollout must never record zero cost"
    assert row["num_turns"] == 2, "turns are recoverable from the stream, so never 0"
    assert row["usage_incomplete"] is True
    assert row["usage_incomplete_reason"] == "spiral_guard_stop"


def test_usage_snapshots_are_deduplicated_on_message_id():
    """Snapshots are cumulative per message and repeat as content blocks stream in, so
    summing every event double-counts. Validated against 60 stored healthy rows: dedupe on
    message.id matches their real `result` event exactly on all three input-side fields."""
    rec = _feed([
        _assistant("m1", 2, 100, 1000, 1),
        _assistant("m1", 2, 400, 5000, 7),      # same message, later snapshot: wins
        _assistant("m2", 2, 50, 6000, 3),
    ])
    row = _row_from_result(rec.synthesize("task_timeout"))
    assert row["cache_creation_input_tokens"] == 450     # 400 + 50, not 550
    assert row["cache_read_input_tokens"] == 11000       # 5000 + 6000, not 12000
    assert row["num_turns"] == 2                         # two messages, not three events


def test_output_tokens_are_reported_missing_not_estimated():
    """The per-message `output_tokens` is the message_start placeholder, understating the
    truth by 7.5x-134.7x across stored rows. Too variable to model, so it is reported as
    absent and flagged -- a wrong number that looks like data is worse than a missing one."""
    row = _row_from_result(_feed([_assistant("m1", 2, 100, 1000, 5)])
                           .synthesize("spiral_guard_stop"))
    assert row["output_tokens"] == 0
    assert row["output_tokens_missing"] is True


def test_segment_that_never_ran_stays_unflagged():
    """A segment killed before its first assistant message really did cost nothing. It must
    come back empty so a genuinely free segment is not libelled as degraded."""
    assert _UsageRecovery().synthesize("spiral_guard_stop") == {}
    assert _feed([{"type": "system"}, {"type": "user"}]).synthesize("x") == {}


def test_healthy_row_carries_the_flags_as_false():
    """Present-and-False, so "no flag" means "written by a harness that did not check"
    rather than "verified complete"."""
    row = _row_from_result({
        "type": "result", "num_turns": 12,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                  "cache_read_input_tokens": 30, "output_tokens": 40}})
    assert row["usage_incomplete"] is False
    assert row["output_tokens_missing"] is False
    assert row["num_turns_recovered"] is False
    assert row["output_tokens"] == 40, "a healthy row must be untouched by the recovery"


# ---- the spiral case: one bad segment degrades the whole task -------------------------
HEALTHY = {"type": "result", "num_turns": 4,
           "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                     "cache_read_input_tokens": 30, "output_tokens": 40}}


def test_flags_survive_a_spiral_merge():
    """The common spiral shape: segment 1 completes, segment 2 is killed by the guard.
    `_merge_usage` builds on the SECOND segment, so the flags have to be OR-ed in or the
    marker is cleared on precisely the case that produces it."""
    killed = _feed([_assistant("m1", 2, 100, 1000, 3)]).synthesize("spiral_guard_stop")
    merged = _row_from_result(_merge_usage(HEALTHY, killed))
    assert merged["usage_incomplete"] is True
    assert merged["num_turns"] == 5                       # 4 + 1, summed not replaced
    assert merged["cache_read_input_tokens"] == 1030      # both segments billed
    # ...and in the other order, since a nudge can be stopped on the first segment.
    assert _row_from_result(_merge_usage(killed, HEALTHY))["usage_incomplete"] is True


def test_merge_of_two_healthy_segments_stays_clean():
    assert _row_from_result(_merge_usage(HEALTHY, HEALTHY))["usage_incomplete"] is False


def test_merge_reason_names_every_degraded_segment():
    a = _feed([_assistant("m1", 2, 1, 1, 1)]).synthesize("spiral_guard_stop")
    b = _feed([_assistant("m2", 2, 1, 1, 1)]).synthesize("task_timeout")
    assert _merge_usage(a, b)["usage_incomplete_reason"] == "spiral_guard_stop+task_timeout"


# ---- the guard-parameter probe --------------------------------------------------------
def test_guard_accepts_probes_the_vendored_signature():
    """`never_abort` exists in some vendored versions of the guard and not in others, and
    passing a keyword the guard does not take raises at task 0, before a row is written. So
    neither runner passes it unconditionally: both go through this one probe."""
    from te_bench import nudge_guard as spiral_vendor
    from te_harness.swebench import guard_accepts
    assert guard_accepts("envelope") is True
    assert guard_accepts("no_such_parameter_exists") is False
    # whatever this checkout's guard takes, the probe agrees with it
    import inspect
    params = inspect.signature(spiral_vendor.NudgeGuard.__init__).parameters
    assert guard_accepts("never_abort") == ("never_abort" in params)


def test_webbench_guard_constructs_on_this_checkout():
    """`_make_guard` must not raise whatever the vendored guard's signature is. It takes no
    `self` state, so it is callable unbound."""
    from te_harness.webbench import WebBenchRunner
    guard = WebBenchRunner._make_guard(None, {
        "spiral_guard": True, "spiral_nudge": True, "spiral_never_abort": True,
        "abort_envelope": 30.0})
    assert guard is not None and guard.nudge_budget == 1
    assert WebBenchRunner._make_guard(None, {}) is None, "guard off -> no guard"


# ---- session-log recovery: output_tokens survives the kill after all -----------------
# The stream's per-message `output_tokens` is a `message_start` placeholder, and the
# terminal `result` event never arrives on a kill (measured 2026-09-10: SIGTERM to a
# mid-stream `claude -p` produces NO result event). But the CLI persists each assistant
# message WITH its real usage as that message completes, so every turn but an in-flight
# one is exactly recoverable from that log.
import json as _json

from te_harness.swebench import session_output_tokens


def _session_lines(pairs, duplicate=True):
    """Claude Code's session log. Each message is written TWICE by the real CLI -- same
    `message.id`, different `uuid` -- which is why the parser must deduplicate."""
    out = []
    for mid, out_tok in pairs:
        for uu in (("a", "b") if duplicate else ("a",)):
            out.append(_json.dumps({"type": "assistant", "uuid": f"{mid}-{uu}",
                                    "message": {"id": mid,
                                                "usage": {"output_tokens": out_tok}}}))
    return "\n".join(out)


def test_session_log_parser_deduplicates_the_double_write():
    """The trap: summing entries returns exactly 2x the truth, which looks plausible."""
    text = _session_lines([("m1", 421)])
    assert len(text.splitlines()) == 2                # the CLI really does write it twice
    total, ids = session_output_tokens(text)
    assert (total, ids) == (421, {"m1"})              # NOT 842


def test_session_log_parser_never_raises_on_junk():
    for junk in ("", "not json", '{"no":"message"}', '{"message":{"id":"m"}}',
                 '{"message":{"id":"m","usage":{"output_tokens":true}}}'):
        assert session_output_tokens(junk) == (0, set())


def test_output_tokens_recovered_exactly_when_every_turn_completed():
    """A spiral stop lands between turns, so the log accounts for all of them and the row
    is a MEASUREMENT again -- not a lower bound to be filtered out."""
    rec = _feed([_assistant("m1", 2, 4000, 3000, 5), _assistant("m2", 2, 500, 7000, 9)])
    row = _row_from_result(rec.synthesize(
        "spiral_guard_stop", _session_lines([("m1", 1200), ("m2", 800)])))
    assert row["output_tokens"] == 2000               # exact, not the 5+9 placeholders
    assert row["output_tokens_exact_turns"] == 2
    assert row["output_tokens_missing_turn"] is False
    assert row["usage_incomplete"] is False           # proven complete -> not flagged
    assert row["output_tokens_missing"] is False
    assert row["usage_incomplete_reason"] is None


def test_an_in_flight_turn_keeps_the_row_flagged():
    """A TE_TASK_TIMEOUT can land mid-generation: the stream saw m3 start, the log has no
    figure for it, so the row is a lower bound and must still say so."""
    rec = _feed([_assistant("m1", 2, 10, 20, 5), _assistant("m2", 2, 10, 20, 7),
                 _assistant("m3", 2, 10, 20, 3)])
    row = _row_from_result(rec.synthesize(
        "task_timeout", _session_lines([("m1", 1200), ("m2", 800)])))
    assert row["output_tokens"] == 2000               # the two turns we can prove
    assert row["output_tokens_exact_turns"] == 2
    assert row["output_tokens_missing_turn"] is True
    assert row["usage_incomplete"] is True            # still a lower bound
    assert row["usage_incomplete_reason"] == "task_timeout"


def test_no_session_log_falls_back_to_a_flagged_zero():
    """An unreadable log must degrade to `output_tokens=0` + flagged, never to a crash."""
    rec = _feed([_assistant("m1", 2, 4000, 3000, 5)])
    for text in (None, "", "garbage"):
        row = _row_from_result(rec.synthesize("spiral_guard_stop", text))
        assert row["output_tokens"] == 0
        assert row["usage_incomplete"] is True
        assert row["output_tokens_missing"] is True
        assert row["output_tokens_exact_turns"] == 0


def test_exact_turns_add_across_spiral_segments_while_flags_or():
    """A nudged task has two segments. Counts add; degradation ORs."""
    clean = _feed([_assistant("m1", 2, 10, 20, 5)]).synthesize(
        "spiral_guard_stop", _session_lines([("m1", 500)]))
    partial = _feed([_assistant("m2", 2, 10, 20, 5), _assistant("m3", 2, 10, 20, 5)]
                    ).synthesize("spiral_guard_stop", _session_lines([("m2", 700)]))
    merged = _merge_usage(clean, partial)
    assert merged["output_tokens_exact_turns"] == 2            # 1 + 1
    assert merged["output_tokens_missing_turn"] is True        # from the second segment
    assert merged["usage_incomplete"] is True
