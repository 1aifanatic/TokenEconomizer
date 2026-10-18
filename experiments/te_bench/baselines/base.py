"""The scaffold seam: what a scaffold is handed, and what it may assume about the agent.

Two objects, because a scaffold has to straddle two things that differ independently:

  * `AgentProfile` -- the per-AGENT facts a scaffold needs (where project instructions
    live, what the native navigation tools are called, how an MCP config is wired in,
    what $HOME is). Claude Code and Copilot CLI disagree on every one of them, and a
    scaffold that hardcoded either would be silently wrong on the other -- writing
    `CLAUDE.md` for Copilot, which never reads it, produces a "scaffold" arm that is
    really a native arm with a stray file in it.
  * `ScaffoldContext` -- the per-TASK handles (container name, exec/copy callables, the
    task dict, the run directory). Passed rather than reached for, so a scaffold cannot
    touch Docker except through the runner that already owns it.

No module here imports Docker, or `te_harness`, or anything outside the standard
library. Every container interaction arrives as a callable on the context -- `exec_`,
`copy_in`, `read_bytes`, `host_run` -- which is what lets these baselines live in the core
package next to the mechanisms they are compared against while still being unable to reach
a container on their own. Any harness that can implement those four can run them.

`exec_` returns a `subprocess.CompletedProcess`; a scaffold reads `.returncode`/`.stdout`
and RECORDS what happened instead of raising. That is deliberate: a scaffold whose install
half-failed must produce a row that says so (`rtk_installed: false`) rather than losing the
task, because a lane of unresolved rows with an explicit reason is diagnosable and a lane
of missing rows is not. Raise `ScaffoldError` only for a misconfiguration that will affect
every task -- those should stop the lane at task 0, not 200 tasks in.
"""
import hashlib
import json
import os
import shlex


class ScaffoldError(RuntimeError):
    """A scaffold cannot work AT ALL as configured (missing host dependency, unknown
    name). Raised at construction so the lane dies before the first paid task."""


class AgentProfile:
    """The per-agent facts a scaffold needs. Runners expose one as `scaffold_profile`.

    `kind` is the coarse switch for genuinely agent-specific wiring (RTK registers a
    PreToolUse hook through Claude's settings.json and through a Copilot plugin -- there is
    no common spelling). Everything else is data.
    """

    def __init__(self, kind, home, instructions_file, nav_tools, mcp_args,
                 patch_excludes=(), bin_dir="/usr/local/bin", env=None):
        self.kind = kind                          # "claude" | "copilot" | "codex"
        self.home = home                          # $HOME the agent runs under
        self.instructions_file = instructions_file  # repo-relative project-instructions path
        self.nav_tools = tuple(nav_tools)         # native code-navigation tool names
        self._mcp_args = mcp_args                 # (cfg_path) -> argv fragment
        self.patch_excludes = tuple(patch_excludes)  # agent artifacts, always excluded
        # Where a scaffold may install an executable, and where the agent's own shell will
        # find it. `/usr/local/bin` is right in a task container, which runs as root; a
        # harness that runs the agent on the HOST (Web-Bench) has no writable /usr/local
        # and passes a per-run directory it also puts on PATH. A scaffold must never
        # hardcode the path -- doing so silently no-ops the install off-container: the
        # `chmod` fails, `&&` short-circuits, and the row reads `<tool>_installed: false`.
        self.bin_dir = bin_dir
        # Environment an install step must run under to land where the AGENT will look.
        # Codex reads its global instructions from `$CODEX_HOME/AGENTS.md`, and CODEX_HOME is
        # not $HOME (on SWE-bench it is /opt/codex-home, on Web-Bench a per-task dir), so a
        # scaffold that ran `rtk init -g --codex` under the bare profile would write into a
        # directory the CLI never reads -- healthy provenance over an arm that is native.
        self.env = dict(env or {})

    def mcp_args(self, cfg_path):
        return list(self._mcp_args(cfg_path)) if cfg_path else []


class ScaffoldContext:
    """Per-task handles a scaffold is allowed to use."""

    def __init__(self, *, profile, container, task, run_dir, exec_, copy_in, host_run,
                 read_bytes=None, log=None):
        self.profile = profile
        self.container = container
        self.task = task
        self.run_dir = run_dir
        self.exec_ = exec_          # (script, *, timeout=, env=) -> CompletedProcess
        self.copy_in = copy_in      # (host_path, container_path) -> CompletedProcess
        self.host_run = host_run    # (argv, *, timeout=) -> CompletedProcess  (HOST side)
        # (script, *, timeout=) -> bytes: run a script in the container and return its RAW
        # stdout. Separate from exec_ because a tar stream is not text and decoding it
        # would corrupt it; a harness that cannot provide it simply cannot run the arms
        # that need it, which is checked rather than discovered mid-task.
        self._read_bytes = read_bytes
        self._log = log or (lambda msg: None)

    def read_bytes(self, script, timeout=600):
        if self._read_bytes is None:
            raise ScaffoldError(
                "this harness provides no `read_bytes` on the baseline context, which "
                "this baseline needs to stream the repository out of the container")
        return self._read_bytes(script, timeout=timeout)

    def log(self, msg):
        self._log(msg)

    # ---- helpers every scaffold needs -----------------------------------------------
    def write_instructions(self, text):
        """Write `text` to the agent's project-instructions file under /testbed.

        Copied in rather than heredoc'd through `bash -lc`: a repo map is a couple of
        thousand characters of arbitrary source-derived text, and any quoting scheme that
        survives backticks, `$(`, and embedded EOF markers is a bug waiting to happen.
        `docker cp` moves bytes.
        """
        rel = self.profile.instructions_file
        path = "/testbed/" + rel
        parent = os.path.dirname(path)
        if parent and parent != "/testbed":
            self.exec_(f"mkdir -p {shlex.quote(parent)}", timeout=30)
        tmp = os.path.join(self.run_dir, "_scaffold_instructions")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        res = self.copy_in(tmp, path)
        os.unlink(tmp)
        return res.returncode == 0

    def write_mcp_config(self, servers, filename="scaffold.mcp.json"):
        """Write an `{"mcpServers": {...}}` config into $HOME and return its path.

        Under $HOME, never under /testbed: a config file in the repo would land in the
        graded diff, and excluding it would be one more thing to remember.
        """
        cfg = f"{self.profile.home}/{filename}"
        tmp = os.path.join(self.run_dir, "_scaffold_mcp.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": servers}, fh, indent=2)
        self.exec_(f"mkdir -p {shlex.quote(self.profile.home)}", timeout=30)
        res = self.copy_in(tmp, cfg)
        os.unlink(tmp)
        return cfg if res.returncode == 0 else None

    def merge_json_file(self, path, patch):
        """Merge `patch` into a container-side JSON file, creating it if absent.

        Used for the agent's own settings file, which the CLI may have seeded already --
        clobbering it would drop keys the agent needs. Runs a here-doc'd Python program
        rather than `jq`, which the era-pinned SWE-bench images do not carry.
        """
        prog = (
            "import json, os, sys\n"
            f"p = {path!r}\n"
            f"patch = json.loads({json.dumps(json.dumps(patch))})\n"
            "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "try:\n"
            "    cur = json.load(open(p))\n"
            "except Exception:\n"
            "    cur = {}\n"
            "for k, v in patch.items():\n"
            "    if isinstance(v, dict) and isinstance(cur.get(k), dict):\n"
            "        cur[k].update(v)\n"
            "    else:\n"
            "        cur[k] = v\n"
            "json.dump(cur, open(p, 'w'), indent=2)\n"
            "print('merged')\n")
        # A UNIQUE name, not a fixed one. In a task container /tmp is private, but a
        # harness that runs the agent on the HOST (Web-Bench) shares /tmp across lanes, and
        # two concurrent runs on one fixed path would race: whichever wrote last decides
        # what both merge, and the loser silently registers the wrong settings.
        stamp = hashlib.sha1(
            f"{self.run_dir}|{path}|{os.getpid()}".encode()).hexdigest()[:12]
        remote = f"/tmp/_scaffold_merge_{stamp}.py"
        tmp = os.path.join(self.run_dir, f"_scaffold_merge_{stamp}.py")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(prog)
        self.copy_in(tmp, remote)
        os.unlink(tmp)
        res = self.exec_(f"python3 {remote}; rm -f {remote}", timeout=60)
        return "merged" in (res.stdout or "")


class Scaffold:
    """Base class. Subclasses set `name` / `patch_excludes` and implement `setup`."""

    name = "base"
    # Paths (repo-relative, git-pathspec syntax) this scaffold writes under /testbed. The
    # runner subtracts them from the graded diff -- see the package docstring.
    patch_excludes = ()

    def mounts(self, profile):
        """Extra `docker run -v` spec strings. Default: none."""
        return []

    def preflight(self, profile):
        """Raise ScaffoldError for a misconfiguration that dooms EVERY task. Called once,
        at runner construction, so a lane fails before it spends anything."""
        return

    def setup(self, ctx):
        """Install and configure in the started container. Return a notes dict for
        `row.json`; it must include `arm_setup` and should record whether each step
        actually took effect, not merely that it was attempted."""
        raise NotImplementedError
