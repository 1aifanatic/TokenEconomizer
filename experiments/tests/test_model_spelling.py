"""One model, two spellings, and the launcher spells it for the backend.

The Copilot CLI's own catalog lists the older Opus as `claude-opus-4.8` (63 healthy stored
subscription rows); the Anthropic API serves `claude-opus-4-8` and 404s the dotted form.
The docs name the model once, dashed; `te_model_for_agent` picks the served spelling per
agent, and `te_check_copilot_model` verifies it against the CLI and switches to the other
separator when the CLI has THAT one, instead of refusing the lane. Everything lands in the
same `_opus48` tree.

Shell functions, exercised through bash against a stub `copilot` binary: no network, no
credentials, no real CLI.
"""
import os
import stat
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_SH = os.path.join(_REPO, "scripts", "launch", "_env.sh")


def _sh(script, env=None):
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
            "REPO": _REPO, "TOKENECONOMIZER_ENV_FILE": "/dev/null"}
    base.update(env or {})
    return subprocess.run(["bash", "-uc", f'. "$REPO/scripts/launch/_env.sh"; {script}'],
                          capture_output=True, text=True, env=base)


# ---- the pure helpers ------------------------------------------------------------------

@pytest.mark.parametrize("model,alt", [
    ("claude-opus-4-8", "claude-opus-4.8"),
    ("claude-opus-4.8", "claude-opus-4-8"),
    ("claude-haiku-4-5", "claude-haiku-4.5"),
    ("claude-haiku-4.5", "claude-haiku-4-5"),
    ("claude-opus-5", ""),
    ("claude-sonnet-5", ""),
    ("auto", ""),
])
def test_alt_spelling_swaps_the_version_separator(model, alt):
    p = _sh(f'te_model_alt_spelling "{model}"')
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == alt


@pytest.mark.parametrize("agent,byok,model,expected", [
    # the Copilot CLI on a subscription spells the older Opus with a dot
    ("copilot", "", "claude-opus-4-8", "claude-opus-4.8"),
    ("copilot", "", "claude-opus-4.8", "claude-opus-4.8"),
    # under BYOK it speaks the Anthropic wire API, which takes the dash
    ("copilot", "https://api.anthropic.com", "claude-opus-4.8", "claude-opus-4-8"),
    ("copilot", "https://api.anthropic.com", "claude-opus-4-8", "claude-opus-4-8"),
    # Claude Code bills the Anthropic API, which takes the dash
    ("claude", "", "claude-opus-4.8", "claude-opus-4-8"),
    ("claude", "", "claude-opus-4-8", "claude-opus-4-8"),
    ("claude", "", "claude-haiku-4.5", "claude-haiku-4-5"),
    # one-spelling ids and non-Claude ids pass through
    ("copilot", "", "claude-opus-5", "claude-opus-5"),
    ("claude", "", "claude-sonnet-5", "claude-sonnet-5"),
    ("codex", "", "gpt-5.6-sol", "gpt-5.6-sol"),
    ("copilot", "", "auto", "auto"),
])
def test_model_for_agent_picks_the_served_spelling(agent, byok, model, expected):
    env = {"COPILOT_PROVIDER_BASE_URL": byok} if byok else {}
    p = _sh(f'te_model_for_agent "{agent}" "{model}"', env)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == expected
    # Only an actual change is announced.
    assert ("model id:" in p.stderr) is (expected != model)


def test_model_for_agent_is_verbatim_under_strict():
    p = _sh('te_model_for_agent copilot claude-opus-4-8; te_model_for_agent claude claude-opus-4.8',
            {"TE_STRICT_MODEL_ID": "1"})
    assert p.stdout.split() == ["claude-opus-4-8", "claude-opus-4.8"] and "model id:" not in p.stderr


def test_model_for_agent_never_changes_the_tree_tag():
    p = _sh('te_model_tag "$(te_model_for_agent copilot claude-opus-4-8 2>/dev/null)"; '
            'te_model_tag "$(te_model_for_agent claude claude-opus-4.8 2>/dev/null)"')
    assert p.stdout.split() == ["opus48", "opus48"]


# ---- the Copilot probe against a stub CLI ----------------------------------------------

_STUB = r'''#!/usr/bin/env bash
# A stand-in for `copilot -p ... --model <id>`: answers the way the real CLI does for an
# id its subscription catalog lists (ok), one it does not ("is not available"), and one a
# BYOK provider 404s. Records every model it was asked for.
m=""
for ((i = 1; i <= $#; i++)); do
  if [[ "${!i}" == "--model" ]]; then j=$((i + 1)); m="${!j}"; fi
done
echo "$m" >> "$STUB_LOG"
case "$m" in
  claude-opus-4.8|claude-sonnet-5|claude-opus-5) echo "ok" ;;
  claude-opus-4-8) echo "Model 'claude-opus-4-8' is not available. Available models: ..." ;;
  *) echo "Model '$m' not found on provider at https://api.anthropic.com (HTTP 404)." ;;
esac
'''


@pytest.fixture
def stub_copilot(tmp_path):
    bin_ = tmp_path / "copilot"
    bin_.write_text(_STUB)
    bin_.chmod(bin_.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "asked.log"
    log.write_text("")
    return {"COPILOT_HOST_BIN": str(bin_), "STUB_LOG": str(log)}, log


def test_copilot_model_check_switches_to_the_cli_spelling(stub_copilot):
    env, log = stub_copilot
    p = _sh('MODEL=claude-opus-4-8; te_check_copilot_model "$MODEL" high; echo "MODEL=$MODEL"', env)
    assert p.returncode == 0, p.stderr
    assert "MODEL=claude-opus-4.8" in p.stdout
    assert "copilot model 'claude-opus-4.8' serves; effort=high accepted" in p.stdout
    assert "does not serve 'claude-opus-4-8' but DOES serve 'claude-opus-4.8'" in p.stderr
    assert log.read_text().split() == ["claude-opus-4-8", "claude-opus-4.8"]   # one retry


def test_copilot_model_check_is_silent_on_a_served_id(stub_copilot):
    env, log = stub_copilot
    p = _sh('te_check_copilot_model claude-sonnet-5 high', env)
    assert p.returncode == 0 and "!!" not in p.stderr
    assert log.read_text().split() == ["claude-sonnet-5"]


def test_copilot_model_check_refuses_a_byok_404(stub_copilot):
    """A BYOK provider says 'not found on provider ... (HTTP 404)', not the subscription's
    'is not available', so the check must read both: on the first alone the launch banner
    would print `serves` for an id every task then 404s on."""
    env, _ = stub_copilot
    env = dict(env, COPILOT_PROVIDER_BASE_URL="https://api.anthropic.com")
    p = _sh('te_check_copilot_model claude-sonnet-9 high', env)
    assert p.returncode == 2
    assert "not served by the BYOK provider" in p.stderr
    assert "serves" not in p.stdout


def test_copilot_model_check_refuses_when_neither_spelling_serves(stub_copilot):
    env, log = stub_copilot
    p = _sh('te_check_copilot_model claude-haiku-4.5 high', env)
    assert p.returncode == 2
    assert "is not available on this subscription" in p.stderr
    assert log.read_text().split() == ["claude-haiku-4.5", "claude-haiku-4-5"]


def test_copilot_model_check_can_be_made_strict(stub_copilot):
    env, log = stub_copilot
    p = _sh('te_check_copilot_model claude-opus-4-8 high', dict(env, TE_STRICT_MODEL_ID="1"))
    assert p.returncode == 2
    assert log.read_text().split() == ["claude-opus-4-8"]                     # no retry
