"""External tool baselines: the contracts that silently corrupt a result if broken.

Offline — no Docker, no network, no agent. Every container interaction goes through the
callables on `ScaffoldContext`, so a fake context is enough to assert what a scaffold
*does*, which is the only way to test this without a task container.

`tokeneconomizer.baselines` lives in the CORE package, so the first group of tests guards
the two invariants that buys: nothing in it imports Docker or `te_harness` (a baseline can
only reach a container through the context it is handed), and nothing in the core package
imports it on its own (so `import tokeneconomizer` stays stdlib-only and the economizer
cannot see a baseline by accident). Those are structural claims; without a test they decay
into a convention.

Three properties matter more than the rest, because each fails QUIETLY:

  * **patch_excludes.** A scaffold writes project instructions into `/testbed`, so `git
    diff` picks them up and the official grader would apply the scaffold's own prompt text
    as part of the agent's fix. Every scaffold that writes into the repo must declare it.
  * **the agent profile is used, not assumed.** Writing `CLAUDE.md` on a Copilot run
    produces an arm that is native with a stray file in it; the instructions path must come
    from the profile.
  * **failure is recorded, not raised.** A scaffold whose install half-failed has to leave
    a row saying so — a lane of flagged rows is diagnosable, a lane of missing rows is not.
"""
import json
import os
import subprocess

import builtins
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from tokeneconomizer import baselines as scaffolds
from tokeneconomizer.baselines import repomap as repomap_mod
from tokeneconomizer.baselines import rtk as rtk_mod


CLAUDE = scaffolds.AgentProfile(
    kind="claude", home="/tmp/agent-home", instructions_file="CLAUDE.md",
    nav_tools=("Read", "Grep", "Glob"), mcp_args=lambda c: ["--mcp-config", c],
    patch_excludes=("CLAUDE.md",))
COPILOT = scaffolds.AgentProfile(
    kind="copilot", home="/home/agent",
    instructions_file=".github/copilot-instructions.md",
    nav_tools=("view", "grep", "glob"), mcp_args=lambda c: [],
    patch_excludes=(".github/copilot-instructions.md",))


def _ok(stdout="ok", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


class FakeCtx(scaffolds.ScaffoldContext):
    """Records every container interaction instead of performing one."""

    def __init__(self, profile, tmp_path, exec_result=None, task=None):
        self.execs, self.copies, self.host_runs, self.logs = [], [], [], []
        self._result = exec_result or (lambda script: _ok())
        super().__init__(
            profile=profile, container="c1",
            task=task or {"instance_id": "django__django-1", "problem_statement": "x"},
            run_dir=str(tmp_path),
            exec_=self._exec, copy_in=self._copy, host_run=self._host,
            log=self.logs.append)

    def _exec(self, script, timeout=300, env=None):
        self.execs.append((script, env or {}))
        return self._result(script)

    def _copy(self, host_path, container_path):
        with open(host_path, encoding="utf-8") as fh:
            self.copies.append((container_path, fh.read()))
        return _ok()

    def _host(self, argv, timeout=None):
        self.host_runs.append(argv)
        return _ok()

    def written(self, path):
        for dest, body in self.copies:
            if dest == path:
                return body
        return None

    def written_prefix(self, prefix):
        """The one file copied to a path starting with `prefix`.

        `merge_json_file` names its helper uniquely per run (a fixed /tmp name would race
        between two host-side lanes sharing /tmp), so a test cannot pin the exact path.
        """
        hits = [(d, b) for d, b in self.copies if d.startswith(prefix)]
        assert len(hits) == 1, f"expected one copy under {prefix!r}, got {[d for d, _ in hits]}"
        return hits[0][1]


# ---- the package boundary ---------------------------------------------------------------
_PKG_DIR = os.path.dirname(scaffolds.__file__)
# Non-stdlib at import time is a bug in every module here EXCEPT the repo-map generator,
# which is a subprocess entry point (never imported) and deliberately does need aider.
_SUBPROCESS_ENTRY_POINTS = {"_repomap_gen.py"}


def _module_sources():
    for name in sorted(os.listdir(_PKG_DIR)):
        if name.endswith(".py") and name not in _SUBPROCESS_ENTRY_POINTS:
            with open(os.path.join(_PKG_DIR, name), encoding="utf-8") as fh:
                yield name, fh.read()


def test_no_baseline_module_imports_docker_or_the_harness():
    """A baseline reaches a container only through the context it is handed. If one
    imported a Docker client or te_harness, it could not live in the core package and no
    other harness could run it."""
    import ast
    for name, src in _module_sources():
        for node in ast.walk(ast.parse(src)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module or ""]
            for mod in mods:
                root = mod.split(".")[0]
                assert root not in ("docker", "te_harness"), f"{name} imports {mod}"


def test_baseline_modules_are_stdlib_only_at_import_time():
    """TOP-LEVEL imports only -- those are the ones that run on `import`.

    A deferred import inside a function is fine and is how the one host dependency is
    handled: `repomap.preflight()` imports aider to check for it, so a machine that will
    never run that arm never needs it installed.
    """
    import ast
    import sys
    for name, src in _module_sources():
        for node in ast.parse(src).body:              # module level, not ast.walk
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module or ""]
            for mod in mods:
                root = mod.split(".")[0]
                assert root in sys.stdlib_module_names or root == "tokeneconomizer", \
                    f"{name} imports non-stdlib {mod} at module level"


def test_the_one_host_dependency_is_deferred_not_top_level():
    """aider must be imported inside preflight, never at module scope -- otherwise
    `import tokeneconomizer.baselines` would require it on every machine."""
    import ast
    src = open(os.path.join(_PKG_DIR, "repomap.py"), encoding="utf-8").read()
    top = {a.name for n in ast.parse(src).body if isinstance(n, ast.Import)
           for a in n.names}
    assert not any(m.startswith("aider") for m in top)
    assert "import aider" in src, "the preflight check itself must still be there"


def test_the_core_package_does_not_import_baselines_by_itself():
    """`import tokeneconomizer` must not pull this package in -- that is what keeps the
    core import free of the baselines and the economizer unable to see one."""
    import subprocess
    import sys
    probe = ("import sys, tokeneconomizer; "
             "print(any(m.startswith('tokeneconomizer.baselines') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         cwd=os.path.dirname(_REPO))     # the repository root holds the core
    assert out.stdout.strip() == "False", out.stdout + out.stderr


def test_no_mechanism_imports_a_baseline():
    """A mechanism that reached for a baseline would make the comparison circular."""
    import ast
    mech_dir = os.path.join(os.path.dirname(_REPO), "tokeneconomizer", "mechanisms")
    for name in sorted(os.listdir(mech_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(mech_dir, name), encoding="utf-8") as fh:
            src = fh.read()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                assert "baselines" not in (node.module or ""), f"{name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert "baselines" not in a.name, f"{name}"


# ---- registry ---------------------------------------------------------------------------
def test_registry_resolves_every_named_scaffold():
    for name in scaffolds.SCAFFOLDS:
        assert scaffolds.get(name).name == name


def test_no_scaffold_is_not_an_error():
    assert scaffolds.get(None) is None and scaffolds.get("") is None
    assert scaffolds.for_arm("trimech") is None and scaffolds.for_arm("") is None


def test_unknown_scaffold_names_the_known_ones():
    with pytest.raises(scaffolds.ScaffoldError) as e:
        scaffolds.get("nope")
    assert "rtk" in str(e.value)


def test_every_arm_mapping_points_at_a_real_scaffold():
    for arm, name in scaffolds.ARM_SCAFFOLDS.items():
        assert name in scaffolds.SCAFFOLDS, arm


# ---- the graded-diff contract -----------------------------------------------------------
def test_scaffolds_writing_into_the_repo_declare_patch_excludes():
    """rtk writes RTK.md and serena indexes into .serena/; both are inside /testbed and
    would otherwise be graded as part of the agent's patch."""
    assert "RTK.md" in scaffolds.get("rtk").patch_excludes
    assert any(e.startswith(".serena") for e in scaffolds.get("serena").patch_excludes)


def test_repomap_relies_on_the_profiles_exclude_not_its_own():
    """The repo map writes ONLY the agent's own instructions file, which every profile
    already excludes -- so an empty tuple here is correct, and this test is what says the
    emptiness was a decision."""
    assert scaffolds.get("repomap").patch_excludes == ()
    assert "CLAUDE.md" in CLAUDE.patch_excludes


# ---- the agent profile is honoured ------------------------------------------------------
def test_instructions_go_to_the_profiles_path(tmp_path):
    for profile, expected in ((CLAUDE, "/testbed/CLAUDE.md"),
                              (COPILOT, "/testbed/.github/copilot-instructions.md")):
        ctx = FakeCtx(profile, tmp_path)
        assert ctx.write_instructions("hello") is True
        assert ctx.written(expected) == "hello"


def test_a_nested_instructions_path_is_created_first(tmp_path):
    ctx = FakeCtx(COPILOT, tmp_path)
    ctx.write_instructions("x")
    assert any("mkdir -p" in script and ".github" in script for script, _ in ctx.execs)


def test_instructions_are_copied_not_shell_quoted(tmp_path):
    """A repo map contains backticks, $(...) and stray EOF markers. Copying bytes means no
    quoting scheme has to survive them."""
    nasty = "`cmd` $(sub) 'q' \"d\"\nEOF\nTE_EOF\n"
    ctx = FakeCtx(CLAUDE, tmp_path)
    ctx.write_instructions(nasty)
    assert ctx.written("/testbed/CLAUDE.md") == nasty


def test_mcp_config_lands_in_home_never_in_the_repo(tmp_path):
    ctx = FakeCtx(CLAUDE, tmp_path)
    cfg = ctx.write_mcp_config({"s": {"command": "/bin/true"}})
    assert cfg.startswith("/tmp/agent-home/") and "/testbed" not in cfg
    assert json.loads(ctx.written(cfg))["mcpServers"]["s"]["command"] == "/bin/true"


def test_write_instructions_leaves_no_temp_file_behind(tmp_path):
    ctx = FakeCtx(CLAUDE, tmp_path)
    ctx.write_instructions("x")
    ctx.write_mcp_config({"s": {}})
    assert [p for p in os.listdir(tmp_path) if p.startswith("_scaffold")] == []


# ---- rtk --------------------------------------------------------------------------------
def test_rtk_registers_the_hook_itself(tmp_path, monkeypatch):
    """`rtk init -g` declines to patch settings non-interactively and exits 0. If the
    scaffold trusted it, the arm would run as native with healthy-looking provenance."""
    monkeypatch.setenv("TE_SCAFFOLD_CACHE", str(tmp_path / "cache"))
    binary = tmp_path / "cache" / "bin" / "rtk"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    ctx = FakeCtx(CLAUDE, tmp_path, exec_result=lambda s: _ok("merged" if "merge" in s
                                                              else "rtk 0.45.0"))
    notes = scaffolds.get("rtk").setup(ctx)
    assert notes["arm_setup"] == "rtk"
    assert notes["rtk_installed"] and notes["rtk_hook_registered"]
    assert notes["rtk_staged_host"] is True
    merged = ctx.written_prefix("/tmp/_scaffold_merge")
    assert "PreToolUse" in merged and "rtk hook claude" in merged
    assert "/tmp/agent-home/.claude/settings.json" in merged
    # The helper's name must be unique per run: on a harness that runs the agent on the
    # HOST, /tmp is shared between lanes and a fixed name would let one run's settings
    # overwrite another's mid-install.
    dest = [d for d, _ in ctx.copies if d.startswith("/tmp/_scaffold_merge")][0]
    assert dest != "/tmp/_scaffold_merge.py", "merge helper must not use a fixed /tmp name"


def test_rtk_warns_loudly_when_the_hook_did_not_register(tmp_path, monkeypatch):
    monkeypatch.setenv("TE_SCAFFOLD_CACHE", str(tmp_path / "cache"))
    binary = tmp_path / "cache" / "bin" / "rtk"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    # exec always succeeds but never prints the merge marker
    ctx = FakeCtx(CLAUDE, tmp_path, exec_result=lambda s: _ok("rtk 0.45.0"))
    notes = scaffolds.get("rtk").setup(ctx)
    assert notes["rtk_installed"] and not notes["rtk_hook_registered"]
    assert any("would run as native" in m for m in ctx.logs)


def _copilot_exec(script):
    """Stand in for the container: echo the install marker, and rewrite on the hook probe."""
    if "RTK_HOOK_AT=" in script:
        return _ok("RTK_HOOK_AT=/home/agent/.copilot/hooks/rtk-rewrite.json")
    if "hook copilot" in script:                      # the effect probe
        return _ok('{"hookSpecificOutput":{"updatedInput":{"command":"rtk ls -la"}}}')
    return _ok("rtk 0.45.0")


def test_rtk_on_copilot_registers_through_the_hooks_dir_not_a_plugin(tmp_path, monkeypatch):
    """Copilot CLI has TWO PreToolUse channels and they deliver DIFFERENT payload shapes
    (measured on 1.0.83, 2026-09-13, by arming both at once):

        $COPILOT_HOME/hooks/*.json   -> {"tool_name": "Bash", "tool_input": {...}}
        $COPILOT_HOME/settings.json  -> {"toolName": "bash",  "toolArgs":   {...}}

    rtk parses only the first. The plugin this scaffold used to install delivered the
    second, so rtk exited 0 with empty stdout on every call and rewrote nothing -- the arm
    was native while every provenance field read healthy. Pin the channel, the document
    shape rtk itself publishes, and the absolute command path."""
    monkeypatch.setenv("TE_SCAFFOLD_CACHE", str(tmp_path / "cache"))
    ctx = FakeCtx(COPILOT, tmp_path, exec_result=_copilot_exec)
    notes = scaffolds.get("rtk").setup(ctx)
    assert notes["rtk_hook_registered"] is True
    assert notes["rtk_hook_channel"] == "copilot-hooks-dir"
    assert notes["rtk_mode"] == "hook"
    assert notes["rtk_hook_path"].endswith("/hooks/rtk-rewrite.json")
    # never through a plugin, and never through the guard's settings.json channel
    assert not any("plugin install" in s for s, _ in ctx.execs)
    assert not any("settings.json" in s for s, _ in ctx.execs)
    write = [s for s, _ in ctx.execs if "RTK_HOOK_AT=" in s][0]
    # COPILOT_HOME is resolved in the SHELL: SWE-bench leaves it unset (the CLI defaults it
    # to $HOME/.copilot in the container), Web-Bench redirects it to a per-task dir.
    assert '${COPILOT_HOME:-$HOME/.copilot}' in write
    body = json.loads(write.split("TE_EOF")[1].lstrip("'").strip())
    assert body["version"] == 1 and list(body["hooks"]) == ["PreToolUse"]
    entry = body["hooks"]["PreToolUse"][0]
    assert entry["command"] == "/usr/local/bin/rtk hook copilot", "absolute path, not bare rtk"
    assert entry["type"] == "command" and "matcher" not in entry


def test_rtk_hook_effect_is_probed_not_assumed(tmp_path, monkeypatch):
    """Registered and executable is still not REWRITING. The Copilot arm was silently
    native for a week with both of those true, so the row carries the effect itself."""
    monkeypatch.setenv("TE_SCAFFOLD_CACHE", str(tmp_path / "cache"))
    ctx = FakeCtx(COPILOT, tmp_path, exec_result=_copilot_exec)
    notes = scaffolds.get("rtk").setup(ctx)
    assert notes["rtk_hook_rewrites"] is True
    probe = [s for s, _ in ctx.execs if "hook copilot" in s and "tool_input" in s]
    assert probe, "the hook must be fed a payload in the shape its channel delivers"
    assert '"tool_name"' in probe[0] and '"tool_input"' in probe[0]

    # ... and a hook that parses nothing (empty stdout, rc 0 -- exactly the old failure)
    # is caught and said out loud rather than reported as a healthy install.
    def mute(script):
        return _ok("") if "hook copilot" in script and "tool_input" in script \
            else _copilot_exec(script)
    ctx2 = FakeCtx(COPILOT, tmp_path, exec_result=mute)
    notes2 = scaffolds.get("rtk").setup(ctx2)
    assert notes2["rtk_hook_registered"] is True and notes2["rtk_hook_rewrites"] is False
    assert any("does NOT rewrite" in m for m in ctx2.logs)


def test_rtk_version_is_pinned():
    """An arm tracking `latest` is not a fixed baseline; the published rows say 0.45.0."""
    assert rtk_mod.RTK_VERSION == "0.45.0"
    assert f"v{rtk_mod.RTK_VERSION}" in rtk_mod.RTK_URL


def test_rtk_cache_dir_requires_configuration(monkeypatch):
    monkeypatch.delenv("TE_SCAFFOLD_CACHE", raising=False)
    monkeypatch.delenv("DATA", raising=False)
    with pytest.raises(scaffolds.ScaffoldError):
        rtk_mod.cache_dir()
    monkeypatch.setenv("DATA", "/somewhere")
    assert rtk_mod.cache_dir() == "/somewhere/scaffolds"


# ---- serena -----------------------------------------------------------------------------
def test_serena_nudges_rather_than_excluding_tools(tmp_path):
    """`Read` is in PROTECTED_TOOLS, so an exclusion would be filtered and the arm would
    silently be native. The baseline is an instruction the agent may disobey."""
    ctx = FakeCtx(CLAUDE, tmp_path, exec_result=lambda s: _ok("serena 1.0"))
    notes = scaffolds.get("serena").setup(ctx)
    assert notes["serena_installed"] and notes["serena_nudged"]
    nudge = ctx.written("/testbed/CLAUDE.md")
    for tool in ("Read", "Grep", "Glob"):
        assert tool in nudge
    assert "find_symbol" in nudge
    assert not hasattr(scaffolds.get("serena"), "exclude_tools")


def test_serena_nudge_names_the_agents_own_tools(tmp_path):
    ctx = FakeCtx(COPILOT, tmp_path, exec_result=lambda s: _ok("serena 1.0"))
    scaffolds.get("serena").setup(ctx)
    nudge = ctx.written("/testbed/.github/copilot-instructions.md")
    assert "view/grep/glob" in nudge and "Read" not in nudge


def test_serena_records_a_failed_install_instead_of_raising(tmp_path):
    ctx = FakeCtx(CLAUDE, tmp_path, exec_result=lambda s: _ok("", rc=1))
    notes = scaffolds.get("serena").setup(ctx)
    assert notes["serena_installed"] is False and notes["serena_nudged"] is False
    assert "mcp_config" not in notes
    assert any("install FAILED" in m for m in ctx.logs)


# ---- repomap ----------------------------------------------------------------------------
def test_repomap_extracts_idents_from_the_issue_only():
    idents = repomap_mod.extract_idents(
        "`DataArray.sel` breaks; see get_axis_num and CFTimeIndex")
    assert "sel" in idents and "get_axis_num" in idents and "CFTimeIndex" in idents
    assert len(idents) <= 60


def test_repomap_ident_extraction_survives_empty_text():
    assert repomap_mod.extract_idents("") == []
    assert repomap_mod.extract_idents(None) == []


def test_repomap_budget_is_configurable(monkeypatch):
    assert repomap_mod.map_tokens() == repomap_mod.DEFAULT_MAP_TOKENS
    monkeypatch.setenv("TE_REPOMAP_TOKENS", "512")
    assert repomap_mod.map_tokens() == 512


def test_repomap_flags_a_task_that_got_no_map(tmp_path, monkeypatch):
    """No map means the task ran native. Averaged in as a repomap row it would dilute the
    very effect being measured, so it must be flagged."""
    scaffold = scaffolds.get("repomap")
    monkeypatch.setattr(scaffold, "fetch_sources", lambda ctx: str(tmp_path / "src"))
    monkeypatch.setattr(scaffold, "generate", lambda src, idents: ("", "boom"))
    ctx = FakeCtx(CLAUDE, tmp_path)
    notes = scaffold.setup(ctx)
    assert notes["map_ok"] is False and notes["map_chars"] == 0
    assert notes["map_err"] == "boom"
    assert ctx.written("/testbed/CLAUDE.md") is None


def test_repomap_writes_the_map_and_records_its_size(tmp_path, monkeypatch):
    scaffold = scaffolds.get("repomap")
    monkeypatch.setattr(scaffold, "fetch_sources", lambda ctx: str(tmp_path / "src"))
    monkeypatch.setattr(scaffold, "generate", lambda src, idents: ("MAP BODY", ""))
    ctx = FakeCtx(CLAUDE, tmp_path)
    notes = scaffold.setup(ctx)
    assert notes["map_ok"] is True and notes["map_chars"] == len("MAP BODY")
    body = ctx.written("/testbed/CLAUDE.md")
    assert body.startswith(repomap_mod.HEADER) and "MAP BODY" in body


def test_repomap_preflight_demands_aider_on_the_host():
    """The generator is a subprocess of this interpreter, so a missing aider must stop the
    lane at construction rather than yield 277 rows of `map_ok: false`."""
    scaffold = scaffolds.get("repomap")
    try:
        import aider.repomap                                        # noqa: F401
    except ImportError:
        with pytest.raises(scaffolds.ScaffoldError) as e:
            scaffold.preflight(CLAUDE)
        assert "aider" in str(e.value)
    else:
        scaffold.preflight(CLAUDE)


def test_repomap_preflight_names_the_interpreter_before_telling_you_to_install(monkeypatch):
    """"Install aider" is the WRONG advice for the likelier failure. The launchers run
    `${PYTHON:-python3}`, so a lane started from an unactivated shell picks up the system
    Python even though the guide's `pip install -e '.[baselines]'` put aider in the venv.
    Observed 2026-09-08: a detached lane died here on a host whose system Python carried
    every other harness dependency, so only this one arm failed. The message must let that
    reader diagnose it instead of reinstalling a package they already have.
    """
    real_import = builtins.__import__

    def no_aider(name, *a, **k):
        if name.startswith("aider"):
            raise ImportError("No module named 'aider'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_aider)
    with pytest.raises(scaffolds.ScaffoldError) as e:
        scaffolds.get("repomap").preflight(CLAUDE)
    msg = str(e.value)
    assert sys.executable in msg, "the message must name the interpreter that actually ran"
    assert "PYTHON=" in msg, "and the launcher knob that fixes it"
    assert msg.index("WRONG INTERPRETER") < msg.index("NOT INSTALLED"), \
        "likeliest cause first"


def test_repomap_generator_keeps_aiders_console_off_stdout(tmp_path):
    """The scaffold takes the generator's stdout VERBATIM as the map body, and aider writes
    its progress line -- "Initial repo scan can be slow in larger repos, but only happens
    once." -- to stdout whenever more than 100 files are uncached, which is every SWE-bench
    repository on every task. Left as it was, that sentence opened every repo map an agent
    read (seen live, 2026-09-13). The generator routes aider's console to stderr while the map
    is built, so stdout is the map and nothing else."""
    pytest.importorskip("aider.repomap")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for i in range(120):                      # above aider's 100-uncached-files threshold
        (pkg / f"mod_{i}.py").write_text(
            f"class Widget{i}:\n    def render_{i}(self):\n        return {i}\n")
    res = subprocess.run([sys.executable, repomap_mod.GEN, str(tmp_path), "512", ""],
                         capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, res.stderr[-500:]
    assert "pkg/mod_" in res.stdout, "a map was expected"
    assert "Initial repo scan" not in res.stdout, res.stdout[:200]
    assert not res.stdout.lstrip().startswith(("Warning", "Initial")), res.stdout[:120]


# ---- rtk on Codex: instruction files, inlined, no hook ---------------------------------
CODEX = scaffolds.AgentProfile(
    kind="codex", home="/tmp/agent-home", instructions_file="AGENTS.md", nav_tools=(),
    mcp_args=lambda cfg: [], patch_excludes=("AGENTS.md", "RTK.md"),
    env={"CODEX_HOME": "/opt/codex-home"})


def _codex_rtk_ctx(tmp_path, monkeypatch, rule_count="1"):
    monkeypatch.setenv("TE_SCAFFOLD_CACHE", str(tmp_path / "cache"))
    binary = tmp_path / "cache" / "bin" / "rtk"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    def result(script):
        if "grep -c" in script:
            return _ok(rule_count)          # the inline step reports how many rule lines landed
        return _ok("rtk 0.45.0")
    return FakeCtx(CODEX, tmp_path, exec_result=result)


def test_rtk_on_codex_inlines_the_rule_into_agents_md_instead_of_registering_a_hook(tmp_path, monkeypatch):
    """rtk's Codex mode is `rtk init -g --codex`: RTK.md plus an AGENTS.md holding a one-line
    `@$CODEX_HOME/RTK.md` include. codex-cli 0.153.1 loads AGENTS.md verbatim and does not
    expand `@` (verified with `codex debug prompt-input`: the literal path reached the model
    and the rule did not). So the scaffold must inline the file itself, under CODEX_HOME --
    not $HOME -- and say on the row that the rule is now literally present."""
    ctx = _codex_rtk_ctx(tmp_path, monkeypatch)
    notes = scaffolds.get("rtk").setup(ctx)
    assert notes["rtk_mode"] == "instructions"
    assert notes["rtk_hook_registered"] is None            # not applicable, not False
    assert notes["rtk_installed"] and notes["rtk_instructions_written"]
    assert notes["rtk_agents_inlined"] is True
    init = [(s, e) for s, e in ctx.execs if s.startswith("rtk init")]
    assert init == [("rtk init -g --codex", {"CODEX_HOME": "/opt/codex-home",
                                            "HOME": "/tmp/agent-home"})]
    inline, env = [(s, e) for s, e in ctx.execs if "grep -c" in s][0]
    assert inline.startswith("H=/opt/codex-home;")
    assert "$H/RTK.md" in inline and "$H/AGENTS.md" in inline
    assert "Always prefix shell commands with" in inline
    assert env["CODEX_HOME"] == "/opt/codex-home"
    # nothing hook-shaped was written anywhere
    assert not any("PreToolUse" in s or "plugin install" in s for s, _ in ctx.execs)
    assert not any("settings.json" in d for d, _ in ctx.copies)


def test_rtk_on_codex_warns_loudly_when_the_rule_never_reached_agents_md(tmp_path, monkeypatch):
    """The failure that matters is silent: files written, agent never told. The row and the
    log must both say the arm would run native."""
    ctx = _codex_rtk_ctx(tmp_path, monkeypatch, rule_count="0")
    notes = scaffolds.get("rtk").setup(ctx)
    assert notes["rtk_installed"] and not notes["rtk_instructions_written"]
    assert notes["rtk_agents_inlined"] is False
    assert any("would run as native" in m for m in ctx.logs)


def test_rtk_on_codex_refuses_a_profile_that_does_not_say_where_codex_home_is(tmp_path, monkeypatch):
    """Without CODEX_HOME the install would land in a directory the CLI never reads --
    healthy provenance over a native arm. Better to refuse."""
    monkeypatch.setenv("TE_SCAFFOLD_CACHE", str(tmp_path / "cache"))
    (tmp_path / "cache" / "bin").mkdir(parents=True)
    (tmp_path / "cache" / "bin" / "rtk").write_text("#!/bin/sh\n")
    (tmp_path / "cache" / "bin" / "rtk").chmod(0o755)
    bare = scaffolds.AgentProfile(kind="codex", home="/tmp/agent-home",
                                  instructions_file="AGENTS.md", nav_tools=(),
                                  mcp_args=lambda cfg: [])
    ctx = FakeCtx(bare, tmp_path, exec_result=lambda s: _ok("rtk 0.45.0"))
    with pytest.raises(scaffolds.ScaffoldError, match="CODEX_HOME"):
        scaffolds.get("rtk").setup(ctx)


# ---- serena on Codex: the same server, spelled for the client that launches it -----------
def test_serena_on_codex_uses_serenas_codex_context_and_hands_the_runner_the_spec(tmp_path):
    """Codex takes MCP servers from config.toml, not argv, so the scaffold cannot wire itself
    the way it does on Claude; it returns the spec (Codex only -- Claude and Copilot rows stay
    byte-identical) and the runner writes it. Serena ships a `codex` context for this client
    (oraios.github.io/serena, clients); everything else about the server is unchanged, so the
    three lanes run the same baseline."""
    ctx = FakeCtx(CODEX, tmp_path, exec_result=lambda s: _ok("serena 1.0"))
    notes = scaffolds.get("serena").setup(ctx)
    assert notes["serena_installed"] and notes["serena_nudged"]
    spec = notes["mcp_servers"]["serena"]
    assert spec["command"] == "/tmp/agent-home/.local/bin/serena"
    assert spec["args"][:3] == ["start-mcp-server", "--project", "/testbed"]
    assert "--context" in spec["args"] and spec["args"][spec["args"].index("--context") + 1] == "codex"
    assert "--transport" in spec["args"] and "stdio" in spec["args"]
    # the nudge lands where Codex reads project instructions, and steers away from the SHELL
    nudge = ctx.written("/testbed/AGENTS.md")
    assert "find_symbol" in nudge and "rg/grep/sed/cat" in nudge
    # Claude keeps its context and gets no spec in its notes
    cctx = FakeCtx(CLAUDE, tmp_path, exec_result=lambda s: _ok("serena 1.0"))
    cnotes = scaffolds.get("serena").setup(cctx)
    assert "mcp_servers" not in cnotes
    assert "ide-assistant" in scaffolds.get("serena").mcp_server(CLAUDE)["args"]
