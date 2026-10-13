"""Each agent bills its own vendor's API directly, decided from the environment alone.

  claude   the Anthropic API, on ANTHROPIC_API_KEY. ANTHROPIC_BASE_URL and
           ANTHROPIC_AUTH_TOKEN are not inputs: a set base URL is reported and ignored, and
           neither one stands in for the key.
  copilot  the GitHub subscription through the CLI's own auth, or the CLI's own BYOK
           provider (COPILOT_PROVIDER_*), which is forwarded to the CLI as configured.
  codex    the OpenAI API, on OPENAI_API_KEY (test_codex_routing.py).

These call the resolvers with an explicit env dict, so they need no network, no CLI binary
and no credentials.
"""
import pytest

from te_harness import copilot
from te_harness import swebench as sw


# ---- Claude: the Anthropic API, and nothing else ----------------------------------------
def test_the_key_is_the_one_credential():
    assert sw.resolve_anthropic_key({"ANTHROPIC_API_KEY": "sk-ant-x"}) == "sk-ant-x"


@pytest.mark.parametrize("env", [
    {},
    {"ANTHROPIC_BASE_URL": "https://example.invalid", "ANTHROPIC_AUTH_TOKEN": "tok"},
    {"ANTHROPIC_AUTH_TOKEN": "tok"},
    {"OPENAI_API_KEY": "sk-test"},
])
def test_nothing_but_the_api_key_satisfies_the_claude_lane(env):
    """No endpoint and no other credential substitutes for ANTHROPIC_API_KEY, and the lane
    refuses at construction rather than once per task."""
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        sw.resolve_anthropic_key(dict(env))


def test_a_set_base_url_is_reported_and_ignored(capsys, monkeypatch):
    monkeypatch.setenv("TE_VERBOSE", "1")
    env = {"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_BASE_URL": "https://example.invalid",
           "ANTHROPIC_AUTH_TOKEN": "tok"}
    assert sw.resolve_anthropic_key(env) == "sk-ant-x"
    out = capsys.readouterr().out
    assert out.count("ANTHROPIC_BASE_URL is set and ignored") == 1
    assert "api.anthropic.com" in out
    # resolving never rewrites the caller's environment
    assert env["ANTHROPIC_BASE_URL"] == "https://example.invalid"


def test_no_notice_when_no_base_url_is_set(capsys, monkeypatch):
    monkeypatch.setenv("TE_VERBOSE", "1")
    sw.resolve_anthropic_key({"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert "ignored" not in capsys.readouterr().out


def test_the_ignored_set_covers_every_steering_variable():
    """The variables that would move Claude Code to another endpoint, credential or model
    catalog. The key itself is never among them."""
    assert {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"} <= set(sw.IGNORED_ANTHROPIC_VARS)
    assert "ANTHROPIC_API_KEY" not in sw.IGNORED_ANTHROPIC_VARS


def test_the_claude_runners_record_the_anthropic_api_as_their_endpoint():
    """run_config.json -> routing.base_url, on both domains."""
    import inspect
    from te_harness import webbench as wb
    for cls in (sw.ClaudeSwebenchRunner, wb.WebBenchRunner):
        src = inspect.getsource(cls.__init__)
        assert "self.routing_base = _ANTHROPIC_DIRECT" in src, cls.__name__
        assert "resolve_anthropic_key()" in src, cls.__name__
    assert sw._ANTHROPIC_DIRECT == "https://api.anthropic.com"


# ---- Copilot: the CLI's own auth, or its own BYOK provider ------------------------------
_BYOK = {"COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
         "COPILOT_PROVIDER_TYPE": "anthropic", "COPILOT_PROVIDER_API_KEY": "sk-ant-x",
         "COPILOT_PROVIDER_MODEL_ID": "claude-sonnet-5"}


def _clear_byok(monkeypatch):
    for var in copilot.PROVIDER_VARS + copilot.PROVIDER_SECRET_VARS:
        monkeypatch.delenv(var, raising=False)


def test_the_subscription_is_the_default(monkeypatch):
    _clear_byok(monkeypatch)
    assert copilot.byok_active() is False
    assert copilot.provider_base() == "github-copilot"
    assert copilot.provider_env() == {} and copilot.provider_secret_names() == []


def test_byok_is_forwarded_to_the_cli_as_configured(monkeypatch):
    """Copilot's own BYOK stays supported: its settings reach the CLI unchanged, the secret
    by NAME only, and the run records the provider it billed."""
    _clear_byok(monkeypatch)
    for k, v in _BYOK.items():
        monkeypatch.setenv(k, v)
    assert copilot.byok_active() is True
    assert copilot.provider_base() == "https://api.anthropic.com"
    env = copilot.provider_env()
    assert env == {k: v for k, v in _BYOK.items() if k != "COPILOT_PROVIDER_API_KEY"}
    assert copilot.provider_secret_names() == ["COPILOT_PROVIDER_API_KEY"]
    assert "sk-ant-x" not in copilot.run_env().values()


def test_an_unrelated_key_never_fills_a_byok_slot(monkeypatch):
    """BYOK is configured through COPILOT_PROVIDER_* alone: no other variable activates it
    or supplies its type or credential."""
    _clear_byok(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert copilot.byok_active() is False
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://example.invalid")
    assert "COPILOT_PROVIDER_TYPE" not in copilot.provider_env()
    assert copilot.provider_secret_names() == []
