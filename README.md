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
- **Tool pruning.** Tools that stay loaded but go unused, session after session (five in a row
  by default), are left out of the next sessions through the agent's own settings. By default
  this happens without asking you: the next session start says which tools were left out, and
  one command gives them back. Set `toolprune=propose` to approve each deny list yourself instead.
- **Failure-loop intervention.** A turn that keeps repeating the same failing edit-and-test
  cycle, well past how long this repository's turns usually take, gets one redirect. If it is
  still stuck after that, the plugin tells you how to stop it; it never stops a turn itself.

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
  can be restored, and the guard only redirects and tells you: stopping a turn is your call.

The paper evaluates these mechanisms with Claude Code, Codex and the Copilot CLI on SWE-Bench,
Web-Bench and two security benchmarks; [`experiments/`](experiments/README.md) reruns the
SWE-Bench and Web-Bench experiments.

## Quick start

You need one of the four agents, signed in any way it supports, and Python 3.10 or newer on
`PATH`. The plugin runs inside your agent's own sessions and never handles credentials, so any
plan or login works (a Claude subscription or Anthropic API key, a ChatGPT plan or OpenAI API
key, a Copilot subscription or your own model key, any provider Pi is set up with), and it needs
no API key of its own.

### Install

Steps 1 and 2 run in your shell; steps 4 and 5 are typed into the agent, in a new session
started inside one of your repositories.

| | Claude Code | Codex | Copilot CLI | Pi |
|---|---|---|---|---|
| **1. Get it** | `git clone https://github.com/microsoft/TokenEconomizer.git` | same as Claude Code | same as Claude Code | nothing to clone |
| **2. Install** | `claude plugin marketplace add "$PWD/TokenEconomizer"`<br>`claude plugin install tokeneconomizer@tokeneconomizer` | `codex plugin marketplace add "$PWD/TokenEconomizer"`<br>`codex plugin add tokeneconomizer@tokeneconomizer` | `copilot plugin marketplace add "$PWD/TokenEconomizer"`<br>`copilot plugin install tokeneconomizer@tokeneconomizer` | `pi install git:github.com/microsoft/TokenEconomizer` |
| **3. Start a session** | trust the folder when asked (**Yes, I trust this folder**) | **Trust and continue** for the folder, then at **Hooks need review** choose **Trust all and continue**; the hooks run from the next session on | **Yes, and remember this folder for future sessions** | nothing to approve |
| **4. Check it** | `/tokeneconomizer:status`, allowing its script if asked | `$tokeneconomizer:status` | `/tokeneconomizer:status`, choosing **Yes** to run its script | `/te status` |
| **5. Once per machine** | `/tokeneconomizer:toolprune refresh-catalog` | not needed | `/tokeneconomizer:toolprune refresh-catalog` | not needed |
| **Update** | `git pull` in the clone | `git pull` in the clone, then `codex plugin add tokeneconomizer@tokeneconomizer` again | `git pull` in the clone | `pi update --extensions` |

Run step 2 where you ran step 1. The marketplace path must be absolute (the Copilot CLI reads
`./TokenEconomizer` as the name of a GitHub repository), and the clone must stay where it is:
Claude Code, Codex and the Copilot CLI run the plugin from it. Pi can install from the clone too,
as `pi install ./TokenEconomizer`. Step 4 prints a short status block. Step 5 records which tools
a session loads, with no model call, so that tool pruning sees all of them; repeat it after the
agent updates. Uninstalling, Copilot with your own model key, and troubleshooting are in
**[`plugin/README.md`](plugin/README.md)**, which walks through each agent.

### Supported versions

The commands in this section were checked on these versions:

| Agent | Checked on | Older versions |
|---|---|---|
| Claude Code | 2.1.306, 2.1.300 | not supported |
| Codex | 0.163.0, 0.153.1 | not supported (before 0.141.0, Codex does not run the plugin's hooks at all) |
| Copilot CLI | 1.0.101, 1.0.83 | not supported |
| Pi | 1.3.0, 1.2.0 | 0.99.x: instead of step 2, start each session with `pi --extension /path/to/TokenEconomizer/plugin/pi/tokeneconomizer.ts`; older: not supported |

### Everyday commands

Work as usual; the plugin needs nothing from you. To see what it does, or to change it:

| To | Claude Code | Codex | Copilot CLI | Pi |
|---|---|---|---|---|
| see what it learned and did, and what sessions cost | `/tokeneconomizer:status` | `$tokeneconomizer:status` | `/tokeneconomizer:status` | `/te status` |
| see the facts it injects, and those forming | `/tokeneconomizer:microskills` | `$tokeneconomizer:microskills` | `/tokeneconomizer:microskills` | `/te microskills` |
| see the tools it left out, and the evidence | `/tokeneconomizer:toolprune` | `$tokeneconomizer:toolprune` | `/tokeneconomizer:toolprune` | not used: Pi loads four tools |
| give them back (it will not remove them again) | `/tokeneconomizer:toolprune restore` | `$tokeneconomizer:toolprune restore` | `/tokeneconomizer:toolprune restore` | — |
| approve each deny list yourself instead | `/tokeneconomizer:config set toolprune=propose`, then `/tokeneconomizer:toolprune apply` | `$tokeneconomizer:config set toolprune=propose`, then `$tokeneconomizer:toolprune apply` | `/tokeneconomizer:config set toolprune=propose`, then `/tokeneconomizer:toolprune apply` | — |
| show every setting in effect | `/tokeneconomizer:config` | `$tokeneconomizer:config` | `/tokeneconomizer:config` | `/te config` |
| turn a mechanism off in this repository | `/tokeneconomizer:config set loopguard=off` | `$tokeneconomizer:config set loopguard=off` | `/tokeneconomizer:config set loopguard=off` | `/te config set loopguard=off` |
| ... or in every repository | `/tokeneconomizer:config set loopguard=off --global` | `$tokeneconomizer:config set loopguard=off --global` | `/tokeneconomizer:config set loopguard=off --global` | `/te config set loopguard=off --global` |
| put a setting back to its default | `/tokeneconomizer:config unset loopguard` | `$tokeneconomizer:config unset loopguard` | `/tokeneconomizer:config unset loopguard` | `/te config unset loopguard` |
| list every repository it keeps state for | `/tokeneconomizer:repos` | `$tokeneconomizer:repos` | `/tokeneconomizer:repos` | `/te repos` |
| **from a terminal**, inside the repository | `~/.tokeneconomizer/bin/te --host claude status` | `~/.tokeneconomizer/bin/te --host codex status` | `~/.tokeneconomizer/bin/te --host copilot status` | `~/.tokeneconomizer/bin/te --host pi status` |
| check the installation (terminal only) | `~/.tokeneconomizer/bin/te --host claude doctor` | `~/.tokeneconomizer/bin/te --host codex doctor` | `~/.tokeneconomizer/bin/te --host copilot doctor` | `~/.tokeneconomizer/bin/te --host pi doctor` |

Inside an agent, each command is one model turn. Codex has no plugin slash commands, so there they
are skills, named like the slash commands: type `$tokeneconomizer` and pick one. (A question in
plain words may be answered from the plugin's files without running the command; naming the skill
runs it.) A Codex command that changes something (`config set`, `toolprune restore`, ...) writes
outside Codex's sandbox, so Codex asks first: choose **Yes, proceed**. The Copilot CLI runs slash
commands in its interactive mode only, and asks before each: choose **Yes**. From a terminal every
command is free: put any of the ones above after `te --host <agent>` (`te --host codex toolprune
restore`, `te --host pi repos`; settings are shared by every agent, so `te config set loopguard=off
--global` needs none). From a plain terminal, `te` without `--host` means the agent that last worked
in the repository. The launcher in `~/.tokeneconomizer/bin` is written by the first session.

## Settings

The three mechanisms are `microskills`, `toolprune` and `loopguard`; each takes `on` or `off`,
and each has numbers you can tune with the same `config set` command (in Claude Code's form here;
each agent's is in the table above). Tool pruning is `auto` by default: once a tool has gone
unused for `toolprune_evidence` sessions in a row (five), the plugin adds it to the deny list
without asking, and it is left out from the next session on. With `toolprune=propose` the plugin
only proposes the list, and nothing changes until you run `toolprune apply`.

```text
/tokeneconomizer:config set microskills_max_facts=2     # inject at most two facts
/tokeneconomizer:config set toolprune_evidence=10       # wait for ten unused sessions
/tokeneconomizer:config set loopguard_nudges=2          # redirect twice before telling you
```

For one session only, set an environment variable when you start the agent:
`TE_DISABLE=1 claude` turns the whole plugin off, and `TE_LOOPGUARD=off codex` (likewise
`TE_MICROSKILLS`, `TE_TOOLPRUNE`) one mechanism. The paid helpers -- an LLM fact proposer and a
judge for the loop guard -- are off unless you set `paid=true`; they run through your own agent's
CLI, on the same plan or login as your sessions. Every setting, its default and its environment
variable: [Settings](plugin/README.md#settings).

## What it does on each agent

| | Micro-skills | Tool pruning | Loop guard |
|---|---|---|---|
| Claude Code | at session start | tools, in `.claude/settings.local.json` | a redirect, then a notice to you |
| Codex | at session start | capability packages, in the project's `.codex/config.toml` | a redirect, then a notice to you |
| Copilot CLI | at session start | whole MCP servers, in `.github/copilot/settings.local.json` | a redirect, then a notice to you |
| Pi | at session start | — (Pi loads four tools) | a redirect, then a notice to you |

Tool pruning writes only the entries it adds and never touches deny rules you wrote; a settings
file it creates is kept out of git (`.git/info/exclude`). The next session start says what it
left out. On Claude Code that line shows only in the detailed transcript (**Ctrl+O**), so
`/tokeneconomizer:toolprune` is the place to check. `toolprune restore` removes the plugin's
entries and no others, and what you give back stays back. With `toolprune=propose`, nothing is
written until you approve it. If the plugin cannot edit a file safely (a `.codex/config.toml`
your repository commits, a settings file that is not valid JSON, a path through a symlink), the
list stays a proposal and the session start says why. How each agent's hooks and settings
were verified to behave is in **[`docs/PLUGIN.md`](docs/PLUGIN.md)**, and how the package fits
together in **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**.

### When a turn gets stuck

The loop guard does the same on every agent, in its interactive UI and in a headless run
(`claude -p`, `codex exec`, `copilot -p`, `pi -p`). The first time a turn looks stuck, it adds a
redirect to the model's context, and the model sees it on every agent. If the turn is still
stuck after that, it shows you one line, which ends with how to stop the turn on that agent:

> TokenEconomizer: this turn still looks stuck after a redirect (envelope, 12 tool calls). Press
> Esc to interrupt it.

It never stops a turn itself; whether to stop is up to you. Where that line appears, and how you
stop, differ by agent and mode:

| | Claude Code | Codex | Copilot CLI | Pi |
|---|---|---|---|---|
| **Interactive: the line appears** | in Claude's reply: the plugin asks Claude to repeat it word for word; the detailed transcript (**Ctrl+O**) always has it | in the conversation ("↳ Hook · TokenEconomizer: …") | in the conversation ("● TokenEconomizer: …") | as a notification |
| **Interactive: to stop** | **Esc** | **Esc**, then `/stop` to end a command it left running | **Esc** twice | **Esc** |
| **Headless: the line appears** | in Claude's final reply, so in the text output; also as a `hook_response` event with `--output-format stream-json --verbose` | nowhere: `codex exec` prints no hook messages | on stdout | on stderr |
| **Headless: to stop** | **Ctrl+C** (ends the run and its command) | **Ctrl+C** (ends the run and its command) | **Ctrl+C** (ends the run and its command) | **Ctrl+C** ends the run; a command it started runs on until it finishes |

Claude Code can keep a plugin's own lines to its detailed transcript, which is why the line
reaches you there through Claude: it is a request to the model, so a model may word it
differently or leave it out. In a headless run nobody may be watching, so a turn the guard
flagged keeps running until it ends by itself or reaches the agent's own limits. `te status` counts the redirects and the
"still stuck" notices afterwards. `loopguard=stop`, with which earlier versions stopped a
headless turn on Claude Code and Pi, is still accepted and works as `nudge`, with a warning.

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
