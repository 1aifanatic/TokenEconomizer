"""Serena — an off-the-shelf LSP-backed semantic-code MCP toolkit.

Installed per-container through `uv` (it wants its own Python 3.13, which the era-pinned
SWE-bench images do not have), exposed to the agent as an MCP server, and then *asked* to
be used in place of the native navigation tools.

Asked, not enforced. The original study nudged with an instruction and did not pass
`--disallowed-tools`, and that is kept exactly: `Read` is in `PROTECTED_TOOLS`, so an
exclusion would be filtered out by the runner and the arm would quietly run as native with
an unused MCP server attached. An instruction the agent is free to disobey is a real
property of this baseline -- if the agent keeps grepping, that IS the measurement.

The install needs network (PyPI + astral.sh) from inside the container. Unlike RTK's
GitHub-blackholed path there is no host-staging fallback here, because what gets installed
is an entire Python toolchain rather than one static binary -- so on a fully sealed host
this arm cannot run, and the row says `serena_installed: false` rather than pretending.
"""
from .base import Scaffold

# The semantic tools the nudge names. Listed explicitly rather than discovered, because
# the nudge has to name them for the agent and a drifting list would silently weaken it.
SERENA_TOOLS = ("find_symbol", "get_symbols_overview", "find_referencing_symbols",
                "find_implementations", "search_for_pattern")

INSTALL = ("curl -LsSf https://astral.sh/uv/install.sh | sh >/tmp/uv.log 2>&1; "
           "export PATH=$HOME/.local/bin:$PATH; "
           "uv tool install -p 3.13 serena-agent >/tmp/serena_install.log 2>&1 && "
           "serena --version")


class SerenaScaffold(Scaffold):
    name = "serena"
    # Serena indexes the project into `.serena/` inside the repo.
    patch_excludes = (".serena", ".serena/**")

    def setup(self, ctx):
        home = ctx.profile.home
        ins = ctx.exec_(INSTALL, timeout=900, env={"HOME": home})
        installed = ins.returncode == 0
        notes = {"arm_setup": "serena", "serena_installed": installed,
                 "serena_version": (ins.stdout or "").strip()[-30:]}
        if not installed:
            # Every later step depends on the binary; recording the failure and running
            # native is the honest outcome, and the flag makes the row unusable-as-serena
            # rather than silently counted as one.
            ctx.log("serena: install FAILED -- this task runs without the MCP server "
                    f"({(ins.stderr or '')[-200:]})")
            notes["serena_nudged"] = False
            return notes

        spec = self.mcp_server(ctx.profile)
        cfg = ctx.write_mcp_config({"serena": spec})
        notes["mcp_config"] = cfg
        if ctx.profile.kind == "codex":
            # Codex takes MCP servers from `config.toml`, not from an argv flag, so the JSON
            # above is provenance only there; the runner rewrites the config with this spec
            # (te_harness.swebench.CodexSwebenchRunner._apply_scaffold). Returned for Codex
            # alone so Claude and Copilot rows stay byte-identical to before.
            notes["mcp_servers"] = {"serena": spec}
        notes["serena_nudged"] = ctx.write_instructions(self.nudge(ctx.profile))
        return notes

    @staticmethod
    def mcp_server(profile):
        """The one server spec, spelled for the agent that will launch it.

        Serena ships a context per client (oraios.github.io/serena, "clients"): `codex` for
        Codex CLI, and the IDE-assistant context this scaffold has always used for the two
        agents that take an argv MCP config. The context trims the tool set to what the
        client can use, which is the only per-agent difference; project, transport and the
        dashboard flag are the same everywhere so the three lanes run the SAME baseline.
        """
        home = profile.home
        context = "codex" if profile.kind == "codex" else "ide-assistant"
        return {"command": f"{home}/.local/bin/serena",
                "args": ["start-mcp-server", "--project", "/testbed",
                         "--context", context, "--transport", "stdio",
                         "--enable-web-dashboard", "false"],
                "env": {"PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"}}

    @staticmethod
    def nudge(profile):
        tools = ", ".join("`" + t + "`" for t in SERENA_TOOLS)
        # Codex has no separate navigation tools: it reads and searches through the shell,
        # so the tools to steer it away from are the shell ones.
        forbid = "/".join(profile.nav_tools) or "rg/grep/sed/cat in the shell"
        return ("# MANDATORY code-navigation policy\n"
                "For ANY code search, symbol lookup, or 'where is X defined/used' task, "
                "you MUST use the Serena MCP tools (use tool search to load them if "
                f"needed): {tools}. Do NOT use {forbid} to locate or browse code -- use "
                "Serena's semantic tools instead. They return precise, symbol-level "
                "results and are the required way to navigate this repository.\n")
