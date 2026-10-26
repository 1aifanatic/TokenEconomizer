"""The between-task optimizer runs on the agent's own model, in every harness.

The mechanisms make paid LLM calls between tasks -- the injection proposer, the tool
judge, the spiral judge, the nudge writer -- and `ledger.overhead` charges them to the
same cost basis as the task rows, against a baseline that made no such calls. So the
model those calls run on is not a tuning knob: a cheaper proposer would understate the
overhead an economized arm actually incurs, and the arm would look better for having
billed its own thinking at a discount. It is also what the cell name claims, since a cell
names one model.

These are SOURCE-level assertions rather than constructed runners: building a real one
needs a CLI binary, a grading table and Docker, none of which an offline test has. The
thing worth guarding is the resolution rule itself, and that is visible in the source.
"""
import ast
import inspect
import os
import json

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HARNESSES = ("swebench.py", "webbench.py")


def _tree(fname):
    with open(os.path.join(_REPO, "te_harness", fname), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _runner_classes(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name.endswith("Runner")]


def _self_optimizer_assignments(node):
    """Every `self.optimizer_model = <value>` in this subtree, as value nodes."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if (isinstance(t, ast.Attribute) and t.attr == "optimizer_model"
                    and isinstance(t.value, ast.Name) and t.value.id == "self"):
                out.append(n.value)
    return out


def _mentions_self_model(value):
    return any(isinstance(n, ast.Attribute) and n.attr == "model"
               and isinstance(n.value, ast.Name) and n.value.id == "self"
               for n in ast.walk(value))


def test_every_runner_takes_its_optimizer_model_from_its_own_model():
    seen = 0
    for fname in _HARNESSES:
        for cls in _runner_classes(_tree(fname)):
            for value in _self_optimizer_assignments(cls):
                seen += 1
                assert _mentions_self_model(value), (
                    f"{fname}:{cls.name} assigns optimizer_model from something other "
                    f"than self.model: {ast.dump(value)[:120]}")
    assert seen >= 4, f"expected an assignment per runner, found {seen}"


def test_no_runner_accepts_an_optimizer_model_argument():
    """A caller that could pass one could split the ledger's cost basis in two."""
    for fname in _HARNESSES:
        for cls in _runner_classes(_tree(fname)):
            for fn in cls.body:
                if not (isinstance(fn, ast.FunctionDef) and fn.name == "__init__"):
                    continue
                names = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
                assert "optimizer_model" not in names, f"{fname}:{cls.name}.__init__"


def test_the_old_env_knob_is_refused_not_read():
    """TE_OPTIMIZER_MODEL is not honoured. Silently ignoring a variable someone set is
    worse than refusing it, so every mention must be inside a raise."""
    for fname in _HARNESSES:
        tree = _tree(fname)
        raised = {n for r in ast.walk(tree) if isinstance(r, ast.Raise)
                  for n in ast.walk(r)}
        guards = {n for i in ast.walk(tree) if isinstance(i, ast.If)
                  for n in ast.walk(i.test)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "TE_OPTIMIZER_MODEL":
                assert node in guards, f"{fname}: TE_OPTIMIZER_MODEL read, not guarded"
        assert any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and "TE_OPTIMIZER_MODEL is no longer honoured" in n.value
                   for n in raised), f"{fname}: no refusal message"


def test_the_driver_refuses_a_model_id_for_the_agent_label_flag():
    """`--optimizer-model` is an agent label; a model id there is the one remaining way to
    charge overhead to a model the arm was not measured on."""
    path = os.path.join(_REPO, "scripts", "run_experiment.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "looks like a model id" in src
    assert "AGENT LABEL" in src


# ---------------------------------------------------------------------------------------
# The mechanism call's TRANSPORT.
#
# The MODEL is fixed by the assertions above -- the agent's own, so overhead is priced on
# the task rows' basis. The WIRE is not: `copilot -p` is an agent session that carries the
# CLI's system prompt on every one-shot mechanism call, which a bare request over the
# Messages API (what the Claude harness uses for these calls) does not. So a Copilot lane
# that can reach the Messages API sends its mechanism calls there.
#
# These guard the two things that must stay true of that transport:
#   * it only ever points at an endpoint that bills WHAT THE TASKS BILL -- the lane's own
#     Anthropic-typed BYOK provider -- and
#   * on the GitHub subscription it never activates at all: the calls stay on the CLI.
# ---------------------------------------------------------------------------------------
import importlib


def _endpoint(env):
    copilot = importlib.import_module("te_harness.copilot")
    return copilot.anthropic_optimizer_endpoint(dict(env))


def test_byok_anthropic_resolves_to_the_provider_the_tasks_already_bill():
    got = _endpoint({"COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
                     "COPILOT_PROVIDER_TYPE": "anthropic",
                     "COPILOT_PROVIDER_API_KEY": "sk-test"})
    assert got == ("https://api.anthropic.com", "sk-test", "")


def test_a_byok_bearer_is_carried_when_there_is_no_key():
    got = _endpoint({"COPILOT_PROVIDER_BASE_URL": "https://llm.example.invalid",
                     "COPILOT_PROVIDER_TYPE": "anthropic",
                     "COPILOT_PROVIDER_BEARER_TOKEN": "bt-test"})
    assert got == ("https://llm.example.invalid", "", "bt-test")


def test_byok_needs_its_own_type_and_credential():
    """Only COPILOT_PROVIDER_* configures BYOK: no other key fills its type or credential,
    and resolving never writes into the environment it reads."""
    env = {"COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
           "ANTHROPIC_API_KEY": "sk-ant-x"}
    assert _endpoint(env) is None
    copilot = importlib.import_module("te_harness.copilot")
    copilot.anthropic_optimizer_endpoint(env)
    assert sorted(env) == ["ANTHROPIC_API_KEY", "COPILOT_PROVIDER_BASE_URL"]


def test_a_non_anthropic_byok_provider_keeps_the_cli():
    # It does not speak the Messages wire; there is nothing to send it.
    assert _endpoint({"COPILOT_PROVIDER_BASE_URL": "https://api.openai.com",
                      "COPILOT_PROVIDER_TYPE": "openai",
                      "COPILOT_PROVIDER_API_KEY": "sk-test"}) is None


def test_byok_without_a_credential_keeps_the_cli():
    assert _endpoint({"COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
                      "COPILOT_PROVIDER_TYPE": "anthropic"}) is None


def test_a_subscription_lane_always_keeps_the_cli():
    """The subscription is reachable only through `copilot -p`, so a subscription lane's
    mechanism calls stay on the CLI whatever else the environment carries."""
    assert _endpoint({}) is None
    assert _endpoint({"ANTHROPIC_API_KEY": "sk-ant-x",
                      "ANTHROPIC_BASE_URL": "https://example.invalid"}) is None


def test_the_optimizer_call_excludes_tools_and_not_only_denies_them():
    """--deny-tool withholds permission but leaves the schema in the prompt; only
    --excluded-tools keeps it out. Passing just the first leaves ~16,900 tokens of tool
    schema on EVERY mechanism call, which the ledger charges to the arm."""
    copilot = importlib.import_module("te_harness.copilot")
    src = inspect.getsource(copilot.optimizer_call)
    assert "--excluded-tools" in src
    assert "--deny-tool" in src, "permission should still be withheld too"
    assert set(copilot.OPTIMIZER_DENY_TOOLS) <= set(copilot.OPTIMIZER_EXCLUDE_TOOLS)


def test_both_harnesses_branch_on_the_endpoint_and_record_the_transport():
    """A transport that is not recorded is a silent basis change: rows made over the CLI
    and rows made over the API are not comparable on overhead."""
    for fname in _HARNESSES:
        src = open(os.path.join(_REPO, "te_harness", fname), encoding="utf-8").read()
        assert "anthropic_optimizer_endpoint()" in src, fname
        assert '"optimizer_transport"' in src, fname


# ---------------------------------------------------------------------------------------
# WHO pays for the transport preflight.
#
# The preflight exists because anthropic_optimizer_call never raises: a dead mechanism
# endpoint would leave an economized arm running as a baseline wearing its name. It runs
# only for an arm that makes mechanism calls: `baseline` makes none, so probing for it
# would bill a call it never needs and refuse a lane over an endpoint it never touches.
# `config.needs_optimizer` is the gate, and these pin it to the facade so the two cannot
# drift.
# ---------------------------------------------------------------------------------------
def test_needs_optimizer_agrees_with_the_facade_for_every_arm():
    """The predicate must be true exactly when Economizer.__init__ would resolve a runner.

    The facade's three conditions are the definition; this asserts the aggregate matches
    them arm by arm, so adding an arm cannot silently skip a preflight it needs.
    """
    from te_bench.arms import ARMS
    from tokeneconomizer.config import needs_optimizer
    for name, arm in sorted(ARMS.items()):
        cfg = arm.cfg()
        facade_would_resolve = bool(
            (cfg.injection.enabled and "distiller" in cfg.injection.profile)
            or cfg.tooling.judge
            or (cfg.spiral.guard and (cfg.spiral.judge or cfg.spiral.nudge_llm)))
        assert needs_optimizer(cfg) is facade_would_resolve, name


def test_arm_mode_is_not_the_test():
    """Guards the trap that made this non-obvious: `spiral-only` and `untool-only` carry
    mode `standalone` and DO make paid calls, so gating on mode would skip their
    preflight and hand back exactly the silent-fallback rows it exists to prevent."""
    from te_bench.arms import ARMS
    from tokeneconomizer.config import needs_optimizer
    for name in ("spiral-only", "untool-only"):
        assert ARMS[name].mode == "standalone", name
        assert needs_optimizer(ARMS[name].cfg()) is True, name
    for name in ("baseline", "baseline-low", "baseline-medium"):
        assert needs_optimizer(ARMS[name].cfg()) is False, name


def test_the_driver_tells_the_harness_before_building_the_runner():
    """Order matters: the signal has to be set BEFORE load_runner, because the harness
    reads it in its constructor."""
    src = open(os.path.join(_REPO, "scripts", "run_experiment.py"), encoding="utf-8").read()
    signal = src.index('TE_OPTIMIZER_PREFLIGHT')
    build = src.index('runner = load_runner(')
    assert signal < build, "the preflight signal must be exported before the runner is built"
    assert "needs_optimizer(config)" in src


def test_both_harnesses_gate_the_preflight_and_default_to_checking():
    """Unset must mean 'assume it might' -- a direct caller of make_runner keeps the
    check, so the gate can only ever be relaxed deliberately by the driver. The gate lives
    in the two shared probes (the Messages one in swebench, the Responses one in codex),
    and every runner of both domains goes through one of them."""
    gate = 'os.environ.get("TE_OPTIMIZER_PREFLIGHT", "1") == "0"'
    for fname in ("swebench.py", "codex.py"):
        src = open(os.path.join(_REPO, "te_harness", fname), encoding="utf-8").read()
        assert gate in src, fname
    for fname in _HARNESSES:
        tree = _tree(fname)
        for cls in _runner_classes(tree):
            fn = next((m for m in cls.body if isinstance(m, ast.FunctionDef)
                       and m.name == "_preflight_optimizer_endpoint"), None)
            if fn is None:
                continue
            calls = {getattr(n.func, "attr", getattr(n.func, "id", None))
                     for n in ast.walk(fn) if isinstance(n, ast.Call)}
            assert calls & {"preflight_optimizer_endpoint", "preflight_optimizer"}, (
                f"{fname}:{cls.name} probes without the shared, gated helper")


def test_every_runner_caps_the_optimizer_call_at_the_same_number():
    """One cap across both agents and both domains.

    A runner with a lower cap than the others is a hazard rather than an inconsistency: a
    cap too low truncates a judge's per-tool JSON
    into an unparseable answer, and the mechanisms read that as "no verdict" while the call
    is still billed. So the cell pays for a judge whose verdict was thrown away, and only
    the truncation log says so. Pin them equal.
    """
    seen = {}
    for fname in _HARNESSES:
        tree = _tree(fname)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"):
                continue
            args = node.args
            if (len(args) == 2 and isinstance(args[0], ast.Constant)
                    and args[0].value == "TE_OPTIMIZER_MAX_TOKENS"
                    and isinstance(args[1], ast.Constant)):
                seen.setdefault(args[1].value, []).append(fname)
    assert seen, "no TE_OPTIMIZER_MAX_TOKENS default found -- did the call shape change?"
    assert list(seen) == ["16384"], f"runners disagree on the optimizer cap: {seen}"


# --------------------------------------------------------------------------------------
# WHERE the mechanism call goes, and WHICH CREDENTIAL it sends.
#
# Exactly one header, because anthropic_optimizer_call never raises: a request the endpoint
# refuses arrives as ("", 0.0) == "no verdict", and the arm quietly runs its LLM-free path
# while the cell grades and bills like any other.
# --------------------------------------------------------------------------------------
def _request_for(monkeypatch, **kw):
    """Capture the request anthropic_optimizer_call would send, without a network call."""
    from te_harness import swebench as S
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"content": [{"type": "text", "text": "ok"}],
                               "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr(S.urllib.request, "urlopen", _fake_urlopen)
    S.anthropic_optimizer_call("hi", model="agent", configured_model="claude-opus-5",
                               max_tokens=16, **kw)
    return seen


def test_the_claude_lane_calls_the_anthropic_api_on_its_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TE_OPTIMIZER_LOG", str(tmp_path / "log.jsonl"))
    got = _request_for(monkeypatch, api_key="sk-ant-x")
    assert got["url"] == "https://api.anthropic.com/v1/messages"
    assert got["headers"]["x-api-key"] == "sk-ant-x"
    assert "authorization" not in got["headers"]


def test_a_byok_provider_gets_its_own_url_and_one_credential(monkeypatch, tmp_path):
    """The one other caller: a Copilot lane on an Anthropic-typed BYOK provider."""
    monkeypatch.setenv("TE_OPTIMIZER_LOG", str(tmp_path / "log.jsonl"))
    got = _request_for(monkeypatch, base_url="https://llm.example.invalid/", bearer="bt")
    assert got["url"] == "https://llm.example.invalid/v1/messages"
    assert got["headers"]["authorization"] == "Bearer bt"
    assert "x-api-key" not in got["headers"]
    both = _request_for(monkeypatch, base_url="https://llm.example.invalid",
                        api_key="sk-ant-x", bearer="bt")
    assert both["headers"]["x-api-key"] == "sk-ant-x"
    assert "authorization" not in both["headers"], "never both headers at once"


def test_every_runner_preflights_the_mechanism_endpoint():
    """All FOUR runners, not just the Copilot pair. A runner without the probe lets a
    refused credential (a 401) bill a lane's rows before the ledger shows overhead of zero."""
    for fname, classes in (("swebench.py", ("ClaudeSwebenchRunner", "CopilotSwebenchRunner")),
                           ("webbench.py", ("WebBenchRunner", "CopilotWebBenchRunner"))):
        tree = _tree(fname)
        for cls in classes:
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.ClassDef) and n.name == cls)
            methods = {m.name for m in node.body if isinstance(m, ast.FunctionDef)}
            assert "_preflight_optimizer_endpoint" in methods, f"{fname}:{cls}"
            init = next(m for m in node.body
                        if isinstance(m, ast.FunctionDef) and m.name == "__init__")
            called = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "_preflight_optimizer_endpoint"
                         for n in ast.walk(init))
            assert called, f"{fname}:{cls}.__init__ never runs its preflight"
