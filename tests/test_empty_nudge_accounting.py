import pytest
from tokeneconomizer.loopguard.judge import llm_nudge_writer

@pytest.mark.parametrize('reply', ['', '   \n', None])
def test_empty_paid_reply_retains_reported_cost(reply):
    result = llm_nudge_writer('digest', lambda *args: (reply, 123))
    assert result == {'text': '', '_tokens': 123}


def test_empty_reply_without_usage_estimates_prompt_cost():
    result = llm_nudge_writer('digest', lambda *args: ('', None))
    assert result['text'] == ''
    assert result['_tokens'] > 0


def test_no_runner_does_not_charge():
    assert llm_nudge_writer('digest', None) == {'text': '', '_tokens': 0}


def test_background_job_charges_empty_nudge(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tokeneconomizer.plugin import judge
    from tokeneconomizer.plugin.store import Store
    ctx = SimpleNamespace(store=Store(tmp_path), root=str(tmp_path), host=SimpleNamespace(name='claude'), config=object())
    ctx.store.write('judge/s.request.json', dict(id='job', digest='digest', threshold=0.7))
    replies = iter([('{"stuck":true,"confidence":0.9}', 42), ('', 123)])
    monkeypatch.setattr(judge.sidecall, 'runner_for', lambda *args: lambda *args: next(replies))
    result = judge.run_job(ctx, 's', 'job')
    assert '_nudge' not in result
    overhead = ctx.store.read_lines('ledger.jsonl')[0]['overhead']
    assert overhead == {'spiral_judge_tokens': 42, 'spiral_nudge_writer_tokens': 123}
