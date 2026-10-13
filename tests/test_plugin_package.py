"""The installable plugin: manifests, hook files, launchers, and the self-contained bundle.

These pin the packaging facts a host relies on and a refactor could silently break: every hook
file runs the one launcher that exists and is executable, the Codex file names its host and keeps
SessionEnd under Codex's three-second cap, the marketplace points at the plugin directory, the
launcher fails open, and the bundle `scripts/build_plugin.py` writes runs with no checkout beside
it -- which is what a marketplace install, copying only the plugin directory, actually gets.
"""
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugin")
EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"}


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def _commands(hooks_doc):
    for event, groups in hooks_doc["hooks"].items():
        for group in groups:
            for h in group["hooks"]:
                yield event, group, h


def test_manifests_agree_on_identity():
    claude = _json(PLUGIN, ".claude-plugin", "plugin.json")
    codex = _json(PLUGIN, ".codex-plugin", "plugin.json")
    # Copilot reads `.github/plugin/plugin.json` in preference to `.claude-plugin/` (verified on
    # 1.0.83: only the hooks it names ran), which is how it gets a hook file of its own.
    copilot = _json(PLUGIN, ".github", "plugin", "plugin.json")
    assert claude["name"] == codex["name"] == copilot["name"] == "tokeneconomizer"
    assert claude["version"] == codex["version"] == copilot["version"]
    assert codex["hooks"] == "./hooks/codex-hooks.json"
    assert copilot["hooks"] == "hooks/copilot-hooks.json"
    for m in (codex, copilot):
        assert os.path.exists(os.path.join(PLUGIN, m["hooks"]))
    assert "hooks" not in claude              # Claude Code reads hooks/hooks.json by convention


@pytest.mark.parametrize("name,host", [("hooks.json", None), ("codex-hooks.json", "codex"),
                                       ("copilot-hooks.json", "copilot")])
def test_hook_files_run_the_launcher_on_every_event(name, host):
    doc = _json(PLUGIN, "hooks", name)
    # no PreToolUse: the plugin never blocks a call. Copilot reports a failed tool in an event
    # of its own, which the guard must see too.
    want = EVENTS | ({"PostToolUseFailure"} if host == "copilot" else set())
    assert set(doc["hooks"]) == want
    launcher = os.path.join(PLUGIN, "scripts", "te-hook")
    assert os.access(launcher, os.X_OK)
    for event, group, h in _commands(doc):
        assert h["type"] == "command" and "/scripts/te-hook" in h["command"]
        assert "matcher" not in group                     # PostToolUse watches every tool
        if host:
            assert f"--host {host}" in h["command"]
        if host == "copilot":
            assert h["command"].startswith('"${PLUGIN_ROOT}/scripts/te-hook"')
            if event == "SessionEnd" and host == "codex":
                assert h["timeout"] <= 3                  # Codex's cap for SessionEnd


def test_marketplace_lists_the_plugin_directory():
    m = _json(REPO, ".claude-plugin", "marketplace.json")
    entry = {p["name"]: p for p in m["plugins"]}["tokeneconomizer"]
    assert entry["source"] == "./plugin"
    assert os.path.isdir(os.path.join(REPO, entry["source"], ".claude-plugin"))


def test_no_bin_directory():
    """Files in a plugin's bin/ go on the Bash tool's PATH, and claude.ai will not install a
    plugin that has one; the launchers live in scripts/."""
    assert not os.path.exists(os.path.join(PLUGIN, "bin"))


def _run_hook(launcher, payload, env):
    p = subprocess.run([launcher], input=json.dumps(payload), capture_output=True, text=True,
                       env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


def _hook_env(tmp_path, **extra):
    env = {"PATH": os.environ.get("PATH", ""), "TOKENECONOMIZER_HOME": str(tmp_path / "home"),
           "GIT_CEILING_DIRECTORIES": str(tmp_path), "TE_FOLD_SYNC": "1"}
    env.update(extra)
    return env


def test_the_launcher_runs_the_dispatcher_from_the_checkout(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code, out, _ = _run_hook(os.path.join(PLUGIN, "scripts", "te-hook"),
                             {"hook_event_name": "SessionStart", "session_id": "s",
                              "cwd": str(work), "source": "startup"}, _hook_env(tmp_path))
    assert code == 0 and json.loads(out) == {}
    assert os.path.isdir(tmp_path / "home" / "repos")      # it ran, and recorded the session


def test_the_launcher_fails_open_without_python(tmp_path):
    code, out, _ = _run_hook(os.path.join(PLUGIN, "scripts", "te-hook"),
                             {"hook_event_name": "SessionStart"},
                             {"PATH": "", "TE_PYTHON": "/nonexistent/python"})
    assert code == 0 and json.loads(out) == {}


def test_the_bootstrap_answers_no_change_on_an_old_python(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
    try:
        import te_bootstrap
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(te_bootstrap, "MIN_VERSION", (99, 0))
    monkeypatch.setattr(sys, "argv", ["te_bootstrap.py", "hook"])
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert te_bootstrap.main() == 0 and out.getvalue().strip() == "{}"


def test_the_bundle_runs_without_a_checkout(tmp_path):
    out = tmp_path / "market"
    subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_plugin.py"),
                    "--out", str(out)], check=True, capture_output=True)
    bundle = out / "tokeneconomizer"
    assert (bundle / "lib" / "te_live" / "hook.py").exists()
    assert (bundle / ".github" / "plugin" / "plugin.json").exists()
    assert (bundle / "hooks" / "copilot-hooks.json").exists()
    assert (bundle / "lib" / "tokeneconomizer" / "__init__.py").exists()
    assert not (bundle / "lib" / "te_harness").exists()
    assert not list(bundle.rglob("__pycache__"))
    m = json.loads((out / ".claude-plugin" / "marketplace.json").read_text())
    assert m["plugins"][0]["source"] == "./tokeneconomizer"
    work = tmp_path / "work"
    work.mkdir()
    # Run the bundled launcher with the core reachable ONLY through the bundle's lib/.
    code, stdout, stderr = _run_hook(str(bundle / "scripts" / "te-hook"),
                                     {"hook_event_name": "SessionStart", "session_id": "s",
                                      "cwd": str(work), "source": "startup"},
                                     _hook_env(tmp_path, PYTHONNOUSERSITE="1"))
    assert code == 0 and json.loads(stdout) == {}, stderr
    sys.path.insert(0, str(bundle / "scripts"))
    try:
        import importlib
        import te_bootstrap
        importlib.reload(te_bootstrap)
        assert te_bootstrap.core_path() == str(bundle / "lib")
    finally:
        sys.path.pop(0)


# ---- the layering the README promises ------------------------------------------------------
def test_import_tokeneconomizer_cannot_reach_the_live_harness():
    """`te_live` shells out to agent CLIs (opt-in side calls, the catalog probe); the core must
    not be able to, which is why it is a sibling package and not `tokeneconomizer.live`."""
    probe = ("import sys, tokeneconomizer, tokeneconomizer.arms, tokeneconomizer.mechanisms; "
             "print(any(m.split('.')[0] == 'te_live' for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO)
    assert out.stdout.strip() == "False", out.stdout + out.stderr
    assert not os.path.exists(os.path.join(REPO, "tokeneconomizer", "live"))


def test_the_live_harness_is_stdlib_only_and_never_touches_the_benchmark_harness():
    import ast
    live = os.path.join(REPO, "te_live")
    for name in sorted(os.listdir(live)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(live, name), encoding="utf-8").read())
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                    [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0
                    else [])
            for mod in mods:
                root = mod.split(".")[0]
                assert root not in ("te_harness", "docker"), f"{name} imports {mod}"
                # tomllib: standard library from 3.11, imported behind an ImportError guard
                assert root in sys.stdlib_module_names or root in (
                    "tokeneconomizer", "te_live", "tomllib"), \
                    f"{name} imports non-stdlib {mod}"


def test_the_plugin_directory_is_also_a_pi_package():
    """One directory, three agents: Pi loads the extension its `pi` manifest names, and that
    extension drives the same launchers the Claude Code and Codex hooks run."""
    pkg = _json(PLUGIN, "package.json")
    assert "pi-package" in pkg["keywords"]
    (ext,) = pkg["pi"]["extensions"]
    src = open(os.path.join(PLUGIN, ext), encoding="utf-8").read()
    assert 'join(HERE, "..", "scripts")' in src and '"te-hook", ["--host", "pi"]' in src
    # Exactly the dispatcher's five events. Not `tool_call`, the one Pi event that can block a
    # call before it runs: the plugin guards nothing, on Pi as on the hook hosts.
    assert set(re.findall(r'pi\.on\("(\w+)"', src)) == {
        "session_start", "before_agent_start", "tool_result", "agent_end", "session_shutdown"}
    # no lockfile: Claude Code installs a plugin's npm dependencies only when one exists,
    # and this package has none to install (the Pi API is a peer dependency)
    assert not any(os.path.exists(os.path.join(PLUGIN, f)) for f in
                   ("package-lock.json", "npm-shrinkwrap.json", "bun.lock", "bun.lockb"))


def test_the_plugin_carries_the_core_as_symlinks_an_install_copy_follows(tmp_path):
    """Claude Code installs a COPY of plugin/ into its cache, even from a local checkout, so the
    core must be reachable from inside plugin/. Seen live: without plugin/lib the installed copy's
    hooks answered `{}` and every command crashed."""
    import shutil
    for pkg in ("tokeneconomizer", "te_live"):
        link = os.path.join(PLUGIN, "lib", pkg)
        assert os.path.islink(link) and os.readlink(link) == f"../../{pkg}"
        assert os.path.isfile(os.path.join(link, "__init__.py"))
    # What an install does: copy the directory, following links. The copy runs on its own.
    copy = str(tmp_path / "cache" / "tokeneconomizer" / "0.1.0")
    shutil.copytree(PLUGIN, copy, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = subprocess.run([os.path.join(copy, "scripts", "te"), "doctor"], capture_output=True,
                         text=True, env=dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "h")),
                         timeout=60)
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]
