"""RTK — a Bash-output rewriter, hooked in ahead of the agent's shell tool.

Two integration modes, and the row says which one ran (`rtk_mode`):

  * **hook** (Claude Code, Copilot): a PreToolUse hook rewrites the shell command before it
    runs, transparently. The agent never knows. Compliance is not a question -- but WHICH
    channel the hook is registered through is, on Copilot: that CLI has two, and they hand
    the hook DIFFERENT payload shapes (see `_install_copilot_hook`). rtk parses one of them.
  * **instructions** (Codex): rtk ships no hook for Codex -- `rtk init -g --codex` writes
    `$CODEX_HOME/RTK.md` ("Always prefix shell commands with `rtk`") and an `AGENTS.md` that
    references it, and the AGENT is expected to comply. Two consequences the harness owns:
      - codex-cli 0.153.1 does NOT expand the `@/path/RTK.md` include rtk writes into
        AGENTS.md (verified with `codex debug prompt-input`: the model-visible prompt carried
        the literal `@` line and zero occurrences of the rule). The scaffold therefore
        INLINES RTK.md into AGENTS.md itself, and records that it did.
      - the saving depends on the agent actually typing `rtk ...`, so the runner reads the
        transcript afterwards and reports `rtk_compliance`: the fraction of shell commands
        that were rtk-prefixed. An arm at zero compliance is native, and the row says so.

`rtk hook <agent>` is a local filter (no network) that rewrites shell output into
token-compressed proxies. The scaffold's whole job is to make sure the hook is actually
registered, which is the step the tool itself will not do for you: `rtk init -g` in
non-interactive mode DECLINES to patch the agent's settings, prints a "MANUAL STEP" notice,
and exits 0 (rtk 0.45.0). Trusting it produces an arm where rtk is installed, `rtk --version`
works, every provenance field looks healthy -- and the hook never fires, so the arm is native
with extra steps. So the hook is written explicitly and the row records whether that took.

The binary is staged on the HOST and copied in, because the sealed task container has no
GitHub access: `install.sh` and the release tarball are both unreachable from inside. The
in-container install is kept only as a fallback for an un-blackholed environment, and the
row says which path was used (`rtk_staged_host`) so the two are never confused.

Version is pinned. An arm whose scaffold silently tracked `latest` would not be a fixed
baseline, and the published rows record `rtk 0.45.0`.
"""
import json
import os
import shlex

from .base import Scaffold, ScaffoldError

RTK_VERSION = "0.45.0"
RTK_URL = (f"https://github.com/rtk-ai/rtk/releases/download/v{RTK_VERSION}/"
           "rtk-x86_64-unknown-linux-musl.tar.gz")
# In-container fallback, used only when host staging failed AND the container can reach
# GitHub. Kept because an un-blackholed environment is a legitimate way to run this.
RTK_INSTALL_SH = ("curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/"
                  "refs/heads/master/install.sh | sh")


def cache_dir():
    """Where the staged binary lives. `TE_SCAFFOLD_CACHE`, else under `$DATA`.

    Not inside the repo: it is a multi-megabyte downloaded artifact, and the repo's own
    results/artifacts are git-ignored precisely so that binaries do not accumulate in it.
    """
    explicit = os.environ.get("TE_SCAFFOLD_CACHE")
    if explicit:
        return explicit
    data = os.environ.get("DATA")
    if data:
        return os.path.join(data, "scaffolds")
    raise ScaffoldError(
        "no scaffold cache: set TE_SCAFFOLD_CACHE (or DATA) so the rtk binary can be "
        "staged on the host -- the sealed task container cannot reach GitHub. "
        "See the repo's .env.example")


class RtkScaffold(Scaffold):
    name = "rtk"
    # `rtk init -g` writes RTK.md and appends a reference to the agent's instructions file;
    # both are scaffold artifacts, not the agent's fix.
    patch_excludes = ("RTK.md", ".rtk", ".rtk/**")

    # The PreToolUse hook, per agent. Claude Code reads hooks from settings.json; Copilot
    # reads them from `$COPILOT_HOME/hooks/*.json` (NOT a plugin, and NOT settings.json --
    # see `_install_copilot_hook` for the two channels and why only one works). There is no
    # common spelling, so this is the one place the scaffold branches on the agent.
    #
    # THE COMMAND MUST BE AN ABSOLUTE PATH, not a bare `rtk`. The hook is executed by the
    # AGENT's own subprocess, which does not inherit the PATH that `ctx.exec_` runs the
    # install under. In a container that distinction is invisible -- bin_dir is
    # /usr/local/bin, already on every PATH -- but on a host-run agent (Web-Bench) bin_dir
    # is a per-task directory, and a bare `rtk` resolves to nothing.
    #
    # A hook command that does not resolve is worse than a silent no-op: every shell call
    # gets `rtk: command not found` and the CLI reports the preToolUse hook erroring, while
    # `rtk_installed` and `rtk_hook_registered` both read true. The agent then works around
    # the failing hook, and the task costs many times its native run.
    @staticmethod
    def _hook_binary(profile):
        return f"{profile.bin_dir}/rtk"

    @classmethod
    def _claude_hook(cls, profile):
        return {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [
                {"type": "command",
                 "command": f"{cls._hook_binary(profile)} hook claude"}]}]}}

    # The `$COPILOT_HOME/hooks/*.json` document, in rtk's OWN published shape (what
    # `rtk init -g --copilot` writes): schema `version`, PascalCase `PreToolUse`, no
    # matcher, a per-call timeout. The one change is the command, which rtk writes as a
    # bare `rtk` -- see `_hook_binary` for why that must be absolute here.
    COPILOT_HOOK_FILE = "rtk-rewrite.json"

    @classmethod
    def _copilot_hook_config(cls, profile):
        return {"version": 1, "hooks": {"PreToolUse": [
            {"type": "command",
             "command": f"{cls._hook_binary(profile)} hook copilot",
             "cwd": ".",
             "timeout": 10}]}}

    def bin_path(self):
        return os.path.join(cache_dir(), "bin", "rtk")

    def preflight(self, profile):
        # Resolving the cache directory is what can be misconfigured; the download itself
        # is attempted per-task and falls back, so it is not a preflight failure.
        cache_dir()

    def stage_host_binary(self, ctx):
        """Fetch the pinned musl binary to the host cache once. Idempotent."""
        dest = self.bin_path()
        if os.path.exists(dest) and os.access(dest, os.X_OK):
            return True
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tgz = dest + ".tar.gz"
        res = ctx.host_run(
            ["bash", "-lc",
             f"curl -fsSL -o {tgz} {RTK_URL} && tar xzf {tgz} -C {os.path.dirname(dest)} "
             f"&& chmod +x {dest} && rm -f {tgz}"],
            timeout=300)
        ok = res.returncode == 0 and os.access(dest, os.X_OK)
        if not ok:
            ctx.log(f"rtk: host staging failed ({(res.stderr or '')[-200:]})")
        return ok

    def setup(self, ctx):
        notes = {"arm_setup": "rtk", "rtk_pinned_version": RTK_VERSION}

        # 1. the binary
        staged = self.stage_host_binary(ctx)
        notes["rtk_staged_host"] = bool(staged)
        if staged:
            dest = f"{ctx.profile.bin_dir}/rtk"
            ctx.copy_in(self.bin_path(), dest)
            ver = ctx.exec_(f"chmod +x {dest} && rtk --version", timeout=60)
        else:
            ver = ctx.exec_(
                f"({RTK_INSTALL_SH}) && "
                f"(cp ~/.local/bin/rtk {ctx.profile.bin_dir}/ 2>/dev/null "
                f"|| cp /root/.cargo/bin/rtk {ctx.profile.bin_dir}/ 2>/dev/null || true) && "
                "rtk --version", timeout=300)
        notes["rtk_installed"] = ver.returncode == 0
        notes["rtk_version"] = (ver.stdout or "").strip()[-40:]

        if ctx.profile.kind == "codex":
            notes.update(self._codex_instructions(ctx))
            return notes

        # 2. rtk's own init (generates RTK.md and instruction references). It may decline
        #    to touch settings -- that is expected, and step 3 is why it does not matter.
        init = ctx.exec_("rtk init -g", timeout=60,
                         env={"HOME": ctx.profile.home})
        notes["rtk_init_rc"] = init.returncode

        # 3. register the hook OURSELVES -- the step `rtk init -g` skips non-interactively.
        hook_bin = self._hook_binary(ctx.profile)
        notes["rtk_mode"] = "hook"          # the Codex branch above returns "instructions"
        notes["rtk_hook_cmd"] = hook_bin
        if ctx.profile.kind == "claude":
            notes["rtk_hook_registered"] = ctx.merge_json_file(
                f"{ctx.profile.home}/.claude/settings.json",
                self._claude_hook(ctx.profile))
        else:
            notes.update(self._install_copilot_hook(ctx))

        # 3b. Registration is not the same as INVOCATION. `rtk_hook_registered` only says
        # the settings/plugin write took; it says nothing about whether the agent can run
        # the command. Resolve it the way the agent's hook subprocess will -- by absolute
        # path, under a PATH that does NOT include bin_dir -- so a hook that cannot execute
        # is a flag on the row rather than a `command not found` on every shell call inside
        # a run that still reports healthy.
        probe = ctx.exec_(f"env PATH=/usr/bin:/bin {hook_bin} --version", timeout=60)
        notes["rtk_hook_exec_ok"] = probe.returncode == 0
        # 3c. Registered AND executable is still not REWRITING -- see _probe_hook_rewrites.
        notes["rtk_hook_rewrites"] = self._probe_hook_rewrites(ctx, ctx.profile.kind)
        if notes.get("rtk_hook_registered") and not notes["rtk_hook_rewrites"]:
            ctx.log("rtk: WARNING the hook is registered but does NOT rewrite this "
                    "payload shape -- the arm would run as native with extra steps. "
                    "Check the channel against baselines/rtk._install_copilot_hook.")
        if notes.get("rtk_hook_registered") and not notes["rtk_hook_exec_ok"]:
            ctx.log(f"rtk: WARNING hook registered but {hook_bin} is not executable by the "
                    f"agent's hook subprocess -- every bash call would raise a hook error "
                    f"and the arm would cost far MORE than native, not less.")

        if notes["rtk_installed"] and not notes.get("rtk_hook_registered"):
            # The exact silent-native failure this scaffold exists to prevent. Say it
            # loudly; the row carries the flag either way.
            ctx.log("rtk: WARNING binary present but hook NOT registered -- this arm "
                    "would run as native. Check the agent's settings/plugin path.")
        return notes

    # What the inlined AGENTS.md must contain for the instruction to have reached the agent.
    RTK_RULE = "Always prefix shell commands with"

    def _codex_instructions(self, ctx):
        """Codex: instruction files under $CODEX_HOME, inlined, no hook.

        `rtk init -g --codex` writes `$CODEX_HOME/RTK.md` and puts a one-line
        `@$CODEX_HOME/RTK.md` reference in `$CODEX_HOME/AGENTS.md`. That reference is a
        Claude-Code CLAUDE.md convention; codex-cli 0.153.1 loads AGENTS.md verbatim and does
        not expand it, so the agent would see a dangling path and no rule. The reference line
        is replaced with RTK.md's contents, and the row records both that the files were
        written and that the rule is now literally present in AGENTS.md.
        """
        env = dict(ctx.profile.env)
        home = env.get("CODEX_HOME")
        if not home:
            raise ScaffoldError(
                "the Codex profile carries no CODEX_HOME in its env: rtk would write its "
                "instructions into a directory the CLI never reads")
        env.setdefault("HOME", ctx.profile.home)
        init = ctx.exec_("rtk init -g --codex", timeout=60, env=env)
        notes = {"rtk_mode": "instructions", "rtk_init_rc": init.returncode,
                 "rtk_hook_registered": None}          # not applicable on this agent
        rule = self.RTK_RULE
        # Replace the exact reference line with the file it points at, then count the rule.
        inline = ctx.exec_(
            f"H={home}; test -f $H/RTK.md && test -f $H/AGENTS.md && "
            f"{{ grep -v -x -F \"@$H/RTK.md\" $H/AGENTS.md; printf '\\n'; cat $H/RTK.md; }} "
            f"> $H/AGENTS.md.tmp && mv $H/AGENTS.md.tmp $H/AGENTS.md; "
            f"grep -c '{rule}' $H/AGENTS.md", timeout=60, env=env)
        try:
            present = int((inline.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            present = 0
        notes["rtk_instructions_written"] = init.returncode == 0 and present > 0
        notes["rtk_agents_inlined"] = present > 0
        # The agent's own shell must find the binary under the PATH IT runs with -- the
        # same probe the hook path makes, for the same reason.
        probe = ctx.exec_(f"env PATH=/usr/bin:/bin {self._hook_binary(ctx.profile)} --version",
                          timeout=60)
        notes["rtk_hook_exec_ok"] = probe.returncode == 0
        if not notes["rtk_instructions_written"]:
            ctx.log("rtk: WARNING binary present but the rtk rule did NOT reach "
                    f"{home}/AGENTS.md -- this arm would run as native. "
                    f"(init rc={init.returncode}, inline: {(inline.stderr or '')[-160:]})")
        return notes

    def _install_copilot_hook(self, ctx):
        """Copilot: write rtk's hook into `$COPILOT_HOME/hooks/`, NOT a plugin.

        Copilot CLI exposes two PreToolUse channels, and they do not hand the hook the same
        payload -- measured on 1.0.83, 2026-09-13, by arming both at once with a dumper and
        reading what each received:

            $COPILOT_HOME/hooks/*.json   {"hook_event_name","tool_name":"Bash",
                                          "tool_input":{"command":...}}   <- CLAUDE's shape
            $COPILOT_HOME/settings.json  {"toolName":"bash",
                                          "toolArgs":{"command":...}}     <- Copilot's own

        **rtk parses only the first.** Handed the second it exits 0 with EMPTY stdout, which
        the CLI reads as "no opinion" and runs the command unchanged. A hook on that channel
        -- or in a plugin whose `hooks.json` uses the `preToolUse` spelling, which hands the
        hook the same Copilot shape -- installs cleanly and fires on every call, and rewrites
        nothing.

        On the hooks-dir channel the loop is closed end to end: the model asked for
        `ls -la src`, the CLI executed `rtk ls -la src`, and the agent got rtk's compressed
        output. rtk's reply there is Claude's own `hookSpecificOutput.updatedInput`, which
        this CLI honours -- so Copilot is a TRANSPARENT rewrite like Claude, not the
        instruction mode Codex needs, and no compliance field applies.

        `${COPILOT_HOME:-$HOME/.copilot}` is resolved in the shell rather than here because
        the two lanes disagree: SWE-bench leaves COPILOT_HOME unset and the CLI defaults it
        to `$HOME/.copilot` in the container, while Web-Bench redirects it to a per-task dir
        so a sweep cannot write into the operator's real config. One expansion is right in
        both, and the resolved path is recorded.

        The net guard keeps using `settings.json` (its own shape, its own channel), so the
        two never contend: both were observed firing on one call.
        """
        cfg = json.dumps(self._copilot_hook_config(ctx.profile), indent=2)
        name = self.COPILOT_HOOK_FILE
        script = (
            'H="${COPILOT_HOME:-$HOME/.copilot}"; mkdir -p "$H/hooks" && '
            f'cat > "$H/hooks/{name}" <<\'TE_EOF\'\n{cfg}\nTE_EOF\n'
            f'test -s "$H/hooks/{name}" && echo "RTK_HOOK_AT=$H/hooks/{name}"')
        res = ctx.exec_(script, timeout=60, env={"HOME": ctx.profile.home})
        at = [ln.split("=", 1)[1] for ln in (res.stdout or "").splitlines()
              if ln.startswith("RTK_HOOK_AT=")]
        return {"rtk_hook_registered": bool(at),
                "rtk_hook_channel": "copilot-hooks-dir",
                "rtk_hook_path": at[0] if at else None,
                "rtk_hook_log": ((res.stderr or "") + (res.stdout or ""))[-200:]}

    # A payload in the shape BOTH hook channels this scaffold uses actually deliver
    # (Claude's `tool_name`/`tool_input`; Copilot's hooks dir sends the same). Used to prove
    # the installed binary REWRITES, rather than merely being present and executable.
    _PROBE_PAYLOAD = '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}'

    def _probe_hook_rewrites(self, ctx, agent):
        """Does `rtk hook <agent>` actually rewrite, on THIS binary and THIS payload shape?

        `rtk_hook_registered` says the config was written and `rtk_hook_exec_ok` that the
        binary runs; neither catches a hook that parses nothing and returns an empty
        verdict, which leaves the arm native while both read true. This is the
        effect-level check: feed the hook the payload its channel delivers and look
        for the rewrite in its answer. Cheap, offline, and it fails loudly if a future rtk
        changes its reply shape.
        """
        res = ctx.exec_(
            f"printf '%s' {shlex.quote(self._PROBE_PAYLOAD)} | "
            f"{self._hook_binary(ctx.profile)} hook {agent}", timeout=60)
        return "rtk ls" in (res.stdout or "")
