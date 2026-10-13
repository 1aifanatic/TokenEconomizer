"""Ledger: overhead(), effective_exec(), and the CostLedger accumulator."""
from tokeneconomizer import ledger
from tokeneconomizer.ledger import CostLedger, OVERHEAD_KEYS


def test_overhead_sums_only_known_keys():
    row = {"optimizer_tokens": 10, "spiral_judge_tokens": 5,
           "untool_judge_tokens": 3, "not_an_overhead_key": 999}
    assert ledger.overhead(row) == 18


def test_overhead_ignores_non_numeric():
    assert ledger.overhead({"optimizer_tokens": None, "spiral_judge_tokens": "x"}) == 0


def test_all_overhead_keys_counted():
    row = {k: 1 for k in OVERHEAD_KEYS}
    assert ledger.overhead(row) == len(OVERHEAD_KEYS)


def test_effective_exec_uses_breakdown():
    # cache_read weighted at 0.10, output at 5.0 on the Claude curve.
    row = {"input_tokens": 100, "cache_read_input_tokens": 1000,
           "cache_creation_input_tokens": 0, "output_tokens": 10}
    assert ledger.effective_exec(row) == 100 * 1.0 + 1000 * 0.10 + 10 * 5.0


def test_effective_exec_falls_back_to_total():
    assert ledger.effective_exec({"tokens_total": 777}) == 777


def test_cost_ledger_amortizes():
    cl = CostLedger()
    cl.add({"tokens_total": 100, "optimizer_tokens": 20})
    cl.add({"tokens_total": 200})
    assert cl.cum_exec == 300
    assert cl.cum_overhead == 20
    assert cl.cum_total == 320
    assert cl.amortized_per_task == 160
    assert 0 < cl.overhead_frac < 1
