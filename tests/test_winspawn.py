"""Windows' detached spawn: `te` runs with a hidden console, and the WMI runner with no window.

`DETACHED_PROCESS` looks harmless here and is what opened a visible console window for every git
or agent CLI call `te` made; WMI starting `python.exe` opened one more for the runner. Both are
pinned by the arguments the spawn passes, so these run on any OS.
"""
import os
import subprocess

from tokeneconomizer.plugin import winspawn

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def test_te_gets_a_hidden_console_and_leaves_the_job(monkeypatch, tmp_path):
    seen = {}

    def popen(cmd, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(winspawn.subprocess, "Popen", popen)
    assert winspawn.spawn(["te"], str(tmp_path), {}, str(tmp_path / "te.log"))
    flags = seen["creationflags"]
    assert not flags & DETACHED_PROCESS
    assert flags & CREATE_NO_WINDOW
    assert flags & CREATE_BREAKAWAY_FROM_JOB


def test_a_job_without_breakaway_goes_through_wmi(monkeypatch, tmp_path):
    def popen(cmd, **kwargs):
        raise OSError("access denied")

    calls = []
    monkeypatch.setattr(winspawn.subprocess, "Popen", popen)
    monkeypatch.setattr(winspawn, "_spawn_wmi", lambda *args: calls.append(args) or True)
    assert winspawn.spawn(["te"], str(tmp_path), {}, str(tmp_path / "te.log"))
    assert len(calls) == 1


def _interpreter(monkeypatch, tmp_path, name, beside):
    for other in beside:
        (tmp_path / other).write_text("")
    monkeypatch.setattr(winspawn.sys, "executable", str(tmp_path / name))


def test_the_runner_is_pythonw(monkeypatch, tmp_path):
    _interpreter(monkeypatch, tmp_path, "python.exe", ["python.exe", "pythonw.exe"])
    assert winspawn._windowless_python() == str(tmp_path / "pythonw.exe")


def test_the_runner_matches_a_suffixed_interpreter(monkeypatch, tmp_path):
    _interpreter(monkeypatch, tmp_path, "python3.13t.exe",
                 ["python3.13t.exe", "pythonw3.13t.exe", "pythonw.exe"])
    assert winspawn._windowless_python() == str(tmp_path / "pythonw3.13t.exe")


def test_the_runner_falls_back_to_this_interpreter(monkeypatch, tmp_path):
    _interpreter(monkeypatch, tmp_path, "python.exe", ["python.exe"])
    assert winspawn._windowless_python() == str(tmp_path / "python.exe")


def test_wmi_starts_the_windowless_runner(monkeypatch, tmp_path):
    _interpreter(monkeypatch, tmp_path, "python.exe", ["python.exe", "pythonw.exe"])
    scripts = []

    def run(cmd, **kwargs):
        scripts.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(winspawn.subprocess, "run", run)
    assert winspawn._spawn_wmi(["te"], str(tmp_path), {}, str(tmp_path / "te.log"))
    pythonw = subprocess.list2cmdline([str(tmp_path / "pythonw.exe")])
    assert "CommandLine = '%s " % pythonw.replace("'", "''") in scripts[0]
    for spec in tmp_path.glob("te-spawn-*.json"):
        os.remove(spec)
