# TokenEconomizer

Runtime cost control for coding agents, from outside the agent. TokenEconomizer is a plugin
for **Claude Code**, **Codex**, the **GitHub Copilot CLI** and **Pi**. It watches your sessions
in each repository and, from one session to the next, targets three kinds of token waste:

| Mechanism | What it does | Setting |
|---|---|---|
| **Learned facts** | Environment facts your sessions keep rediscovering the hard way (for example, that `tox` is missing here and `python -m pytest` works) are added to the next session's context. | `inject` |
| **Unused-tool deny list** | Tools that stay loaded but go unused for several sessions are proposed as a deny list. Nothing changes until you apply it. | `untool` |
| **Stuck-turn guard** | A turn that keeps repeating the same failing edit-and-test cycle, well past how long this repository's turns usually take, gets one mid-turn redirect. | `spiral` |

Each mechanism can be turned off or tuned on its own ([Settings](#settings)). Every action is
announced in one line, every hook fails open, and by default the plugin makes no model calls of
its own. What it learns stays on your machine, under `~/.tokeneconomizer`.

## Install

You need Python 3.10 or newer on `PATH`. Clone the repository:

```bash
git clone https://github.com/microsoft/TokenEconomizer.git
```

Then install it into your agent (use the path of your clone):

| Agent | Install |
|---|---|
| Claude Code | `claude plugin marketplace add /path/to/TokenEconomizer`<br>`claude plugin install tokeneconomizer@tokeneconomizer` |
| Copilot CLI | `copilot plugin marketplace add /path/to/TokenEconomizer`<br>`copilot plugin install tokeneconomizer@tokeneconomizer` |
| Codex | `python /path/to/TokenEconomizer/scripts/build_plugin.py --out ~/te-marketplace`<br>`codex plugin marketplace add ~/te-marketplace`<br>`codex plugin add tokeneconomizer@tokeneconomizer`<br>then approve its hooks once in `/hooks` inside Codex |
| Pi | `pi install /path/to/TokenEconomizer/plugin` |

Plugins load when a session starts, so start a new one. On Claude Code and the Copilot CLI, run
`/tokeneconomizer:untool refresh-catalog` once in each repository (Copilot from a terminal:
`~/.tokeneconomizer/bin/te --host copilot untool refresh-catalog`); without it the deny list
cannot see the tools every session loads.

**[`plugin/README.md`](plugin/README.md)** walks through the first run on each agent, updating
and uninstalling, using Copilot with your own model key, and troubleshooting.

## Use

Work as usual; the plugin needs nothing from you. To see what it is doing, in Claude Code or
the Copilot CLI's interactive mode:

| Command | What it shows or does |
|---|---|
| `/tokeneconomizer:status` | what it learned in this repository, what it did, and what your sessions cost |
| `/tokeneconomizer:facts` | the facts it injects at session start, and those still forming |
| `/tokeneconomizer:untool` | the proposed deny list and its evidence; `… apply` accepts it, `… restore` undoes it |
| `/tokeneconomizer:config` | the settings: what is on, and every value in effect |

On Codex, run the same commands from a terminal inside the repository, as
`~/.tokeneconomizer/bin/te --host codex <command>` (for example `… untool apply`). On Pi they
are `/te <command>`.

## Settings

Turn a mechanism on or off, in this repository or, with `--global`, in all of them:

```text
/tokeneconomizer:config set spiral=off
/tokeneconomizer:config set untool=off inject=on
```

Tune one by hand:

```text
/tokeneconomizer:config set inject_max_facts=2           # inject at most two facts
/tokeneconomizer:config set untool_evidence=10           # wait ten unused sessions before proposing
/tokeneconomizer:config set spiral_nudges=2              # allow two redirects per turn
/tokeneconomizer:config set spiral.min_turns=30          # let a turn run 30 tool calls before it can count as stuck
/tokeneconomizer:config unset spiral.min_turns           # back to the default
```

`/tokeneconomizer:config` shows every value in effect. Every setting, its default, and how to
set one for a single session: [Settings in `plugin/README.md`](plugin/README.md#settings).

## What it does on each agent

| | Learned facts | Unused-tool deny list | Stuck-turn guard |
|---|---|---|---|
| Claude Code | at session start | tools, in `.claude/settings.local.json` | redirect; can stop a headless turn |
| Codex | at session start | capability packages, in the project's `.codex/config.toml` | redirect, then a notice |
| Copilot CLI | at session start | whole MCP servers, in `.github/copilot/settings.local.json` | redirect, then a notice |
| Pi | at session start | — (Pi loads four tools) | redirect; can stop a headless turn |

Nothing is written to your settings until you approve it, and `untool restore` removes only
what the plugin added. The design, and how each agent's hooks and settings were verified to
behave, are in **[`docs/PLUGIN.md`](docs/PLUGIN.md)**.

## Repository layout

```
plugin/              the installable plugin: manifests and hook files per agent, the Pi
                     extension, launchers, slash commands
te_live/             the live harness the plugin runs: hook dispatch, per-repository state,
                     host adapters, the `te` command line
tokeneconomizer/     the shared core: the three mechanisms, the trace reader, the cost ledger
                     (stdlib-only, no dependencies)
tests/               offline tests for the plugin and the core
docs/                PLUGIN.md (design and verified agent behaviour), METHOD.md (the method
                     and the core's seams)
scripts/             build_plugin.py: a self-contained bundle for marketplace installs
experiments/         the paper's reproduction code (see below)
```

## Development

Python ≥ 3.10, no runtime dependencies. Run the plugin and core tests with
`python -m pytest tests`, or `python -m pytest` to include the experiments' suite. Build an
installable bundle with `python scripts/build_plugin.py --out dist/marketplace`.

## Reproducing the paper

The benchmark experiments behind the research — the Docker harness that runs sealed agents on
SWE-bench and Web-Bench, the sweep and aggregation pipeline, the evaluated task sets, and the
run book — live in **[`experiments/`](experiments/README.md)**. They are separate
from the plugin and not packaged with it; they share only the core in `tokeneconomizer/`.

The same research code in its original layout, without the plugin, is on the
[`paper`](https://github.com/microsoft/TokenEconomizer/tree/paper) branch.

## Limitations
TokenEconomizer was developed for research and experimental purposes. Further testing and validation are needed before considering its application in commercial or real-world scenarios.

There has not been a systematic effort to ensure that systems using TokenEconomizer are protected from security vulnerabilities such as indirect prompt injection attacks. Content in a repository, issue text, or tool output could influence a synthesized micro-skill that persists across later tasks. Any systems using it should take proactive measures to harden their systems as appropriate.

Downstream developers remain responsible for ensuring that the underlying coding agent, its tool permissions, execution sandbox, generated code, and deployment environment are appropriate to their use case.
