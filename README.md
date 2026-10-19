# TokenEconomizer: Experience-Driven Black-Box Cost Control for Coding Agents

<p align="center">
  Aaron J. Li<sup>1</sup>,
  Weijia Xu<sup>2</sup>,
  Chandan Singh<sup>2</sup>,
  Zixiao Chen<sup>3</sup>,
  Jeevana Priya Inala<sup>2</sup>,
  Charlotte Siska<sup>3</sup>,
  <br>
  Acar Tamersoy<sup>3</sup>,
  Bin Yu<sup>1</sup>,
  Ion Stoica<sup>1</sup>,
  Michel Galley<sup>2</sup>,
  Jianfeng Gao<sup>2</sup>
</p>
<p align="center">
  <sup>1</sup>UC Berkeley &nbsp; <sup>2</sup>Microsoft Research &nbsp; <sup>3</sup>Microsoft Security
</p>

<p align="center">
  <a href="https://arxiv.org/abs/2610.22732"><img src="https://img.shields.io/badge/arXiv-2610.22732-b31b1b.svg" alt="arXiv"></a>
  <a href="https://tokeneconomizer.github.io"><img src="https://img.shields.io/badge/project-page-2f6fb0.svg" alt="Project page"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="Apache 2.0 license">
</p>

<p align="center"><img src="docs/assets/framework_overview.png" width="100%" alt="TokenEconomizer: the plugin keeps what it knows about a repository and applies micro-skills, tool pruning and failure-loop intervention to your unchanged coding agent; each finished session's log updates all three"></p>

TokenEconomizer is a plugin for **Claude Code**, **Codex**, the **GitHub Copilot CLI** and
**Pi**. It leaves the agent as it is, reads each finished session in a repository, and changes
what the next sessions in that repository pay for:

- **Micro-skills.** Environment facts your sessions keep rediscovering by failing at them --
  that `pytest` is missing here and `python -m pytest` works, that the test runner wants labels
  without a `tests.` prefix -- are learned from recurring evidence and stated at the start of
  the next session. Learning them makes no model call.
- **Tool pruning.** Tools that stay loaded but go unused, session after session, are proposed
  as a deny list. Nothing changes until you approve it, and it applies from the next session.
- **Failure-loop intervention.** A turn that keeps repeating the same failing edit-and-test
  cycle, well past how long this repository's turns usually take, gets one redirect, then a
  stop (headless) or a notice.

Each mechanism can be switched off or tuned on its own. The plugin's actions are announced in
one line, every hook fails open, and by default it makes no model calls of its own. What it learns
stays on your machine, under `~/.tokeneconomizer`.

## How it works

Coding agents are built for task success, not for what a task costs, and every turn re-bills
the agent's instructions, every loaded tool, and the conversation so far. Developers use the same
agent on the same codebase over many tasks, so much of that cost recurs: the same environment
facts rediscovered, the same unused tools re-sent, the same kind of failing loop. TokenEconomizer
holds the agent fixed -- no change to its model, weights or code -- and treats a repository's
stream of sessions as the unit to optimize: what finished sessions show about the repository
shapes what later sessions are given, and anything the plugin spends is amortized over the stream.
Four principles shape the mechanisms:

- **Act through what the agent already reads.** Facts go into the context at session start and
  tools leave through the agent's own settings, so nothing depends on the agent adopting a new
  tool.
- **Count cost as it is billed.** Most tokens in a session are cached re-reads, so cost is
  measured on a cache-aware basis, including any model calls the plugin makes.
- **Keep task success.** Facts form only from recurring evidence, tools are pruned only after
  going unused, and the loop guard spares a turn that is still making progress.
- **Recover from mistakes.** A fact a later session contradicts is retired, an applied deny list
  can be restored, and the guard redirects before it ever stops a turn.

The paper evaluates these mechanisms with Claude Code, Codex and the Copilot CLI on SWE-Bench,
Web-Bench and two security benchmarks; [`experiments/`](experiments/README.md) reruns the
SWE-Bench and Web-Bench experiments.

## Supported agents

| Agent | Install |
|---|---|
| Claude Code | `claude plugin marketplace add /path/to/TokenEconomizer`<br>`claude plugin install tokeneconomizer@tokeneconomizer` |
| Codex | `codex plugin marketplace add /path/to/TokenEconomizer`<br>`codex plugin add tokeneconomizer@tokeneconomizer`, then trust its hooks when Codex asks |
| Copilot CLI | `copilot plugin marketplace add /path/to/TokenEconomizer`<br>`copilot plugin install tokeneconomizer@tokeneconomizer` |
| Pi | `pi install git:github.com/microsoft/TokenEconomizer` |

**Any plan or login works.** The plugin runs inside your agent's own sessions and never handles
credentials, so it works with whatever your agent is signed in with: a Claude subscription or an
Anthropic API key for Claude Code, a ChatGPT plan or an OpenAI API key for Codex, a Copilot
subscription or a bring-your-own-key provider for the Copilot CLI, and any provider Pi is set up
with. It needs no API key of its own.

Claude Code, Codex and the Copilot CLI install from a clone of this repository, through their
own plugin commands; Pi installs the package straight from GitHub, as any Pi package does. You
need Python 3.10 or newer on `PATH`, and nothing else. Plugins load when a session starts, so
start a new one. **[`plugin/README.md`](plugin/README.md)** walks through each agent: the first
run, its commands, updating, uninstalling, Copilot with your own model key, and troubleshooting.

## Quick start

```bash
git clone https://github.com/microsoft/TokenEconomizer.git
claude plugin marketplace add ./TokenEconomizer
claude plugin install tokeneconomizer@tokeneconomizer
```

Then, in a new Claude Code session inside one of your repositories:

```text
/tokeneconomizer:toolprune refresh-catalog    # once on this machine: record the tools a session loads
```

Work as usual; the plugin needs nothing from you. To see what it is doing:

| Command | What it shows or does |
|---|---|
| `/tokeneconomizer:status` | what it learned in this repository, what it did, and what your sessions cost |
| `/tokeneconomizer:microskills` | the facts it injects at session start, and those still forming |
| `/tokeneconomizer:toolprune` | the proposed deny list and its evidence; `… apply` accepts it, `… restore` undoes it |
| `/tokeneconomizer:config` | the settings: what is on, and every value in effect |

On Codex, ask for them or name the skill (`$tokeneconomizer-status`, `-microskills`,
`-toolprune`, `-config`); on Pi they are `/te <command>`. On any agent they are also free from a
terminal inside the repository, as `~/.tokeneconomizer/bin/te <command>`.

## Settings

```text
/tokeneconomizer:config set loopguard=off                 # one mechanism off, in this repository
/tokeneconomizer:config set toolprune=off --global        # ... or in every repository
/tokeneconomizer:config set microskills_max_facts=2       # tune one: inject at most two facts
```

The paid helpers -- an LLM fact proposer and a judge for the loop guard -- are off unless you set
`paid=true`; they run through your own agent's CLI, on the same plan or login as your sessions.
Every setting, its default and its environment variable: [Settings](plugin/README.md#settings).

## What it does on each agent

| | Micro-skills | Tool pruning | Loop guard |
|---|---|---|---|
| Claude Code | at session start | tools, in `.claude/settings.local.json` | redirect; can stop a headless turn |
| Codex | at session start | capability packages, in the project's `.codex/config.toml` | redirect, then a notice |
| Copilot CLI | at session start | whole MCP servers, in `.github/copilot/settings.local.json` | redirect, then a notice |
| Pi | at session start | — (Pi loads four tools) | redirect; can stop a headless turn |

Nothing is written to your settings until you approve it, and `toolprune restore` removes only
what the plugin added. How each agent's hooks and settings were verified to behave is in
**[`docs/PLUGIN.md`](docs/PLUGIN.md)**, and how the package fits together in
**[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**.

## Repository

| | |
|---|---|
| [`tokeneconomizer/microskills`](tokeneconomizer/microskills/) | Micro-skill synthesis and injection: facts learned from finished sessions |
| [`tokeneconomizer/toolprune`](tokeneconomizer/toolprune/) | Tool pruning: unused tools and Codex capability packages |
| [`tokeneconomizer/loopguard`](tokeneconomizer/loopguard/) | Failure-loop intervention: the per-call guard and its per-repository envelope |
| [`tokeneconomizer/trace`](tokeneconomizer/trace/) | Each agent's session log, read into one shape |
| [`tokeneconomizer/plugin`](tokeneconomizer/plugin/) | The runtime the hooks and the `te` command run |
| [`plugin`](plugin/) | The installable plugin: manifests, hook files, launchers, slash commands, the Pi extension |
| [`experiments`](experiments/) | The harness, launchers and task sets behind the paper's results |
| [`docs`](docs/) | `PLUGIN.md` (design, verified agent behavior) and `ARCHITECTURE.md` (how the package fits together) |
| [`tests`](tests/) | Offline tests for the package and the plugin |

The package is standard-library only. Run the tests with `python -m pytest tests` (add
`experiments/tests` for the benchmark side), and build a self-contained plugin bundle with
`python3 scripts/build_plugin.py --out dist/marketplace`.

## Reproducing the experiments

The code behind the paper's results -- a harness that runs Claude Code, Codex and the Copilot CLI
on SWE-Bench and Web-Bench, the launchers, and the evaluated task sets -- is in
**[`experiments/`](experiments/README.md)**, whose README gives the setup and one command per
result. It is not packaged with the plugin and shares only `tokeneconomizer/`.

## Citation

```bibtex
@misc{li2026tokeneconomizer,
  title         = {{TokenEconomizer}: Experience-Driven Black-Box Cost Control for Coding Agents},
  author        = {Li, Aaron J. and Xu, Weijia and Singh, Chandan and Chen, Zixiao and
                   Inala, Jeevana Priya and Siska, Charlotte and Tamersoy, Acar and Yu, Bin and
                   Stoica, Ion and Galley, Michel and Gao, Jianfeng},
  year          = {2026},
  eprint        = {2610.22732},
  archivePrefix = {arXiv},
  primaryClass  = {cs.CL},
  url           = {https://arxiv.org/abs/2610.22732}
}
```

## License

Apache 2.0; see [`LICENSE`](LICENSE).
