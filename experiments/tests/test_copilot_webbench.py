"""The Copilot Web-Bench lane: what the AGENT's own shell can find.

One thing has to be pinned here and nowhere else. Web-Bench runs the agent on the HOST, so a
scaffold installs into a per-task bin dir rather than `/usr/local/bin`, and on this lane the
binary has to be on the PATH of the agent's shell -- not merely of the install step. rtk's
hook does not run commands itself: it rewrites `ls -la` into `rtk ls -la` and hands that back
to the CLI to execute. A bin dir missing from the agent's PATH therefore does not degrade the
arm quietly, it breaks every shell call with `rtk: command not found` and the agent flails
against the failure at many times the baseline cost.

That gap was real and invisible until 2026-09-13: while the Copilot rtk hook was registered
through a channel rtk could not parse, nothing was ever rewritten, so no command ever needed
`rtk` on PATH and the missing entry cost nothing. Fixing the channel made it load-bearing.
"""
import os
import subprocess

from te_harness import webbench as wb


def _stub(tmp_path):
    """A runner instance without construction: `__init__` probes a CLI and credentials, and
    nothing under test here depends on either."""
    r = object.__new__(wb.CopilotWebBenchRunner)
    r.model, r.effort = "claude-haiku-4-5-20251001", "high"
    r.copilot = "/usr/bin/true"
    r.task_timeout = 5
    r.scaffold = None
    return r


class _P:
    """A Popen stand-in that records the environment it was handed."""

    seen = {}

    def __init__(self, cmd, **kw):
        _P.seen = dict(kw.get("env") or {})
        self.pid = 1
        self.stdin = None
        self.stdout = iter([])
        self.stderr = type("R", (), {"read": lambda s: ""})()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_the_scaffold_bin_dir_is_on_the_agents_path(tmp_path, monkeypatch):
    """The agent's own shell must find a scaffold's binary, because the rewritten command
    the hook produces is run by that shell."""
    monkeypatch.setattr(subprocess, "Popen", _P)
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True)
    r = _stub(tmp_path)
    with open(tmp_path / "t.jsonl", "w") as fh:
        r._segment(str(ws), "go", fh, [], 80, None)

    run_dir = str(tmp_path / "run")
    bin_dir = wb.WebBenchRunner.scaffold_bin_dir(r.agent_home(run_dir))
    assert _P.seen["PATH"].split(os.pathsep)[0] == bin_dir, (
        "the per-task scaffold bin dir must come FIRST on the agent's PATH; without it "
        "rtk's rewritten `rtk <cmd>` dies `command not found` on every shell call")


def test_copilot_home_is_isolated_and_matches_where_the_hooks_were_written(tmp_path,
                                                                          monkeypatch):
    """Both hook writers land in `COPILOT_HOME` -- rtk's `hooks/rtk-rewrite.json` and the net
    guard's `settings.json`. The segment must export the SAME dir the installs wrote to, or
    the agent looks for its hooks where none were registered, and neither may be the
    operator's real `~/.copilot`."""
    monkeypatch.setattr(subprocess, "Popen", _P)
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True)
    r = _stub(tmp_path)
    with open(tmp_path / "t.jsonl", "w") as fh:
        r._segment(str(ws), "go", fh, [], 80, None)

    run_dir = str(tmp_path / "run")
    assert _P.seen["COPILOT_HOME"] == r.scaffold_copilot_home(run_dir)
    assert _P.seen["COPILOT_HOME"] == os.path.dirname(
        r.net_guard_settings_path(r.agent_home(run_dir), run_dir))
    assert os.path.expanduser("~/.copilot") not in _P.seen["COPILOT_HOME"]


def test_byok_reaches_the_agent_as_configured(tmp_path, monkeypatch):
    """Copilot's own BYOK provider is a supported lane: its settings reach the CLI's
    environment unchanged, so the CLI bills the provider the run records."""
    from te_harness import copilot
    for var in copilot.PROVIDER_VARS + copilot.PROVIDER_SECRET_VARS:
        monkeypatch.delenv(var, raising=False)
    byok = {"COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com",
            "COPILOT_PROVIDER_TYPE": "anthropic", "COPILOT_PROVIDER_API_KEY": "sk-ant-x"}
    for k, v in byok.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "Popen", _P)
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True)
    r = _stub(tmp_path)
    with open(tmp_path / "t.jsonl", "w") as fh:
        r._segment(str(ws), "go", fh, [], 80, None)
    for k, v in byok.items():
        assert _P.seen[k] == v, k
