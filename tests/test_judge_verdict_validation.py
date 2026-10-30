import json
from types import SimpleNamespace
import pytest
from tokeneconomizer.loopguard.judge import llm_stuck_judge
from tokeneconomizer.plugin import judge
from tokeneconomizer.plugin.store import Store

BAD = [dict(stuck='false', confidence=0.95), dict(stuck=1, confidence=0.9),
       dict(stuck=True, confidence=float('nan')), dict(stuck=True, confidence=float('inf')),
       dict(stuck=True, confidence=True), dict(stuck=True, confidence=1.5),
       dict(stuck=True, confidence='bad'), dict(stuck=True)]

@pytest.mark.parametrize('payload', BAD)
def test_invalid_verdict_is_unknown_and_retains_cost(payload):
    result = llm_stuck_judge('digest', lambda *args: (json.dumps(payload), 42))
    assert result['stuck'] is None
    assert result['confidence'] == 0
    assert result['_tokens'] == 42

@pytest.mark.parametrize('stuck', [False, True])
def test_valid_verdict(stuck):
    result = llm_stuck_judge('digest', lambda *args: (json.dumps(dict(stuck=stuck, confidence=0.8)), 42))
    assert result['stuck'] is stuck
    assert result['confidence'] == 0.8


def test_invalid_judge_does_not_trigger_paid_redirect(tmp_path, monkeypatch):
    ctx = SimpleNamespace(store=Store(tmp_path), root=str(tmp_path), host=SimpleNamespace(name='claude'), config=object())
    ctx.store.write('judge/s.request.json', dict(id='job', digest='digest', threshold=0.7))
    calls = []
    def runner(*args):
        calls.append(args)
        return '{"stuck":"false","confidence":0.95}', 42
    monkeypatch.setattr(judge.sidecall, 'runner_for', lambda *args: runner)
    result = judge.run_job(ctx, 's', 'job')
    assert result['stuck'] is None
    assert len(calls) == 1
    assert ctx.store.read_lines('ledger.jsonl')[0]['overhead']['spiral_judge_tokens'] == 42
