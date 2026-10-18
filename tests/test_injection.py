"""Injection: the distiller lifecycle + the PrefixStage render/after_task cycle."""
import os

from tokeneconomizer.microskills import Distiller, PrefixStage
from tokeneconomizer.config import InjectionConfig
from fixtures import write_claude_transcript


def _facts_runner(payload):
    """An optimizer runner returning a fixed proposer payload + 5 eff tokens."""
    return lambda prompt, model="agent": (payload, 5.0)


def test_distiller_empty_render_before_formation(tmp_path):
    d = Distiller("acme/widget", runner=_facts_runner('{"extractors":[],"facts":[]}'))
    tr = str(tmp_path / "t.jsonl")
    write_claude_transcript(tr, [("bash", {"command": "pytest"}, "ok")])
    d.update(tr, iid="widget-1", resolved=True)
    assert d.render() == ""          # nothing has fired/formed yet
    assert d.now == 1


def test_distiller_persist_roundtrip(tmp_path):
    root = str(tmp_path)
    d = Distiller("acme/widget", runner=_facts_runner('{"extractors":[],"facts":[]}'))
    tr = str(tmp_path / "t.jsonl")
    write_claude_transcript(tr, [("bash", {"command": "pytest"}, "ok")])
    d.update(tr, iid="w-1", resolved=True)
    d.save(root)
    d2 = Distiller.load("acme/widget", root)
    assert d2.now == d.now


def test_distiller_grounds_and_fires_a_fact(tmp_path):
    # A proposer fact whose confirm regex matches a repeated pytest form; after enough
    # drifted confirmations it should promote and (once past the firing gate) inject.
    payload = ('{"extractors":[],"facts":[{"id":"use-pytest","claim":"pytest is canonical",'
               '"injection":"run tests with pytest","high_stakes":true,'
               '"detector":{"scope":"ran_tests","confirm":"passed","fired":"ran_tests"},'
               '"cite":["w-0","w-1"]}]}')
    d = Distiller("acme/widget", runner=_facts_runner(payload))
    d.last_propose = -1
    # feed several tasks that each run pytest and pass, on disjoint edited files
    import tokeneconomizer.microskills.distiller as inj
    inj.PROPOSE_EVERY = 1
    for i in range(8):
        tr = str(tmp_path / f"t{i}.jsonl")
        write_claude_transcript(tr, [
            ("edit", {"file_path": f"pkg/mod{i}.py"}, "ok"),
            ("bash", {"command": "python -m pytest tests/test_x.py"}, "1 passed")])
        d.update(tr, iid=f"w-{i}", resolved=True)
    assert "use-pytest" in d.facts
    # at least reached ACTIVE or accrued firing evidence
    f = d.facts["use-pytest"]
    assert f.life.fire_den >= 1


def test_prefix_stage_after_task_reports_tokens(tmp_path):
    root = str(tmp_path)
    cfg = InjectionConfig(enabled=True, propose_every=5)
    stage = PrefixStage(cfg, "acme/widget", root, fresh=True,
                        runner=_facts_runner('{"extractors":[],"facts":[]}'),
                        agent="claude")
    rd = str(tmp_path / "run"); os.makedirs(rd)
    write_claude_transcript(os.path.join(rd, "transcript.jsonl"),
                            [("bash", {"command": "pytest"}, "ok")])
    srow = {"resolved": True}
    opt, reason = stage.after_task({"instance_id": "w-1"}, {}, rd, srow)
    assert reason == "manual-distill"
    assert "manual_facts" in srow
