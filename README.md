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

<p align="center">
  In the paper's SWE-Bench runs: <b>18.4–30.9% lower mean cost</b> across five agent–model
  combinations, cheaper in <b>all 30</b> repository × combination cells, and Pass@1 up in
  <b>4 of 5</b>. <a href="#results">Results</a>
</p>

<p align="center"><img src="docs/assets/framework_overview.png" width="100%" alt="TokenEconomizer: the plugin keeps what it knows about a repository and applies micro-skills, tool pruning and failure-loop intervention to your unchanged coding agent; each finished session's log updates all three"></p>

TokenEconomizer is a plugin for **Claude Code**, **Codex**, the **GitHub Copilot CLI** and
**Pi**. It leaves the agent as it is, reads each finished session in a repository, and changes
what the next sessions in that repository pay for:

- **Micro-skills** (off until you turn it on). Repository knowledge your sessions keep needing,
  in any language -- that a build step must run before anything else works, which spelling of a
  tool works here (`npx tsc` where `tsc` is missing, `python -m pytest` where `pytest` is), an
  environment variable a command needs, where generated files live -- is learned from finished
  sessions and stated at the start of the next one. A fact is kept only while later sessions
  confirm it. Turned on, it calls a model through your own agent every three sessions to propose
  facts, and that cost is counted with your sessions'. How much it helps depends on the
  repository's state and the tasks you run in it.
- **Tool pruning.** Tools that stay loaded but are never used (over ten prompts across at least
  two sessions, or five sessions however short) are left out of the next sessions through the
  agent's own settings. By default this happens without asking you: the next session start says
  which tools were left out, and one command gives them back. Set `toolprune=propose` to approve
  each deny list yourself instead.
- **Failure-loop intervention.** A turn that keeps hitting the same failures is put to a judge,
  a model called through your own agent, and gets one redirect only if the judge finds it stuck.
  If it is still stuck after that, the plugin tells you how to stop it; it never stops a turn
  itself.

Tool pruning and the loop guard are on by default, and each mechanism can be switched on or off
and tuned on its own. The plugin's actions are announced in one line, and every hook fails open.
By default its only model calls are the loop guard's judge, asked only about a turn that shows
signs of looping. What it learns stays on your machine, under `~/.tokeneconomizer`.

## Results

<p align="center"><img src="docs/assets/results_overview.png" width="100%" alt="SWE-Bench results from the paper. Mean cost per task, lower than default: Claude Code with Sonnet 5, 23.6%; Claude Code with Opus 4.8, 30.9%; Codex with GPT-5.6 Terra, 24.7%; Codex with GPT-5.6 Sol, 26.1%; Copilot CLI with Sonnet 5, 18.4%. Pass@1 changes by +1.3, −1.0, +0.9, +1.6 and +2.6 points."></p>

The paper runs five agent–model combinations on the same 277 SWE-Bench tasks from six
repositories, each repository's tasks in order and starting from no traces; costs include every
call TokenEconomizer made. It was cheaper than default in each of the 30 repository ×
combination cells, by 5–40%, and the savings carried over to Web-Bench (16–21% lower) and to two
security-auditing benchmarks where every task is a new repository (6–20% lower). The
[project page](https://tokeneconomizer.github.io) has interactive charts, the per-repository
numbers, and nine annotated runs; [`experiments/`](experiments/README.md) reruns the SWE-Bench and
Web-Bench experiments.

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

## Quick start

You need one of the four agents, signed in any way it supports, and Python 3.10 or newer on
`PATH`. The plugin runs inside your agent's own sessions and never handles credentials, so any
plan or login works, and it needs no API key of its own.

### Install

Run the shell commands where the clone should stay: Claude Code, Codex and the Copilot CLI run
the plugin from it. Then start the agent inside one of your repositories.

**Claude Code**

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
claude plugin marketplace add "$PWD/TokenEconomizer"
claude plugin install tokeneconomizer@tokeneconomizer
```

Start `claude`, trust the folder (**Yes, I trust this folder**), and run
`/tokeneconomizer:status`, allowing its script if asked. Once per machine, and again after Claude
Code updates, run `/tokeneconomizer:toolprune refresh-catalog`.

**Codex**

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
codex plugin marketplace add "$PWD/TokenEconomizer"
codex plugin add tokeneconomizer@tokeneconomizer
```

Start `codex`, choose **Trust and continue** for the folder, then at **Hooks need review**
choose **Trust all and continue**; the hooks run from the next session on. Check with
`$tokeneconomizer:status`.

**Copilot CLI**

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
copilot plugin marketplace add "$PWD/TokenEconomizer"
copilot plugin install tokeneconomizer@tokeneconomizer
```

Start `copilot`, choose **Yes, and remember this folder for future sessions**, and run
`/tokeneconomizer:status`, choosing **Yes** to run its script. Once per machine, and again after
Copilot updates, run `/tokeneconomizer:toolprune refresh-catalog`.

**Pi**

```sh
pi install git:github.com/microsoft/TokenEconomizer
```

Start `pi` and run `/te status`.

The status command prints a short status block. `refresh-catalog` records which tools a session
loads, with no model call, so that tool pruning sees all of them. The marketplace path must be
absolute: the Copilot CLI reads `./TokenEconomizer` as the name of a GitHub repository.

To update:

| Agent | Update |
|---|---|
| Claude Code, Copilot CLI | `git pull` in the clone |
| Codex | `git pull` in the clone, then the `codex plugin add` line again |
| Pi | `pi update --extensions` |

Uninstalling, Copilot with your own model key, and troubleshooting are in
**[`plugin/README.md`](plugin/README.md)**, which walks through each agent.

### Supported versions

| Agent | Supported | Older versions |
|---|---|---|
| Claude Code | 2.1.300 to 2.1.310 | not supported |
| Codex | 0.153.1 to 0.164.0 | not supported |
| Copilot CLI | 1.0.83 to 1.0.102 | not supported |
| Pi | 1.2.0 to 1.3.0 | 0.99.x: load the extension by path (below) |

The commands above were checked on both ends of each range; later versions have not been
checked.

- **Codex before 0.141.0** does not run plugin hooks at all.
- **Pi 0.99.x**: instead of `pi install`, clone the repository and start each session with
  `pi --extension /path/to/TokenEconomizer/plugin/pi/tokeneconomizer.ts`, using your clone's
  full path; `git pull` in the clone updates it.

### Everyday commands

Work as usual; the plugin needs nothing from you. Its commands have the same names on every
agent; only how you type them differs:

| Agent | Type a command as |
|---|---|
| Claude Code, Copilot CLI | `/tokeneconomizer:<command>` |
| Codex | `$tokeneconomizer:<command>` |
| Pi | `/te <command>` |
| a terminal, in the repository | `~/.tokeneconomizer/bin/te <command>` |

| Command | What it does |
|---|---|
| `status` | what it learned and did here, and what your sessions cost |
| `microskills` | the facts it injects, and those still forming |
| `toolprune` | the tools it left out, and the evidence (not on Pi) |
| `toolprune restore` | give them back; they will not be removed again |
| `config` | every setting in effect |
| `config set microskills=on` | turn a mechanism on (or `=off`) here; add `--global` for every repository |

Inside an agent each command is one model turn; from a terminal it costs nothing. Every other
command (`repos`, `doctor`, `config unset`, ...) and how each agent asks before running one:
[Commands](plugin/README.md#commands).

## Settings

The three mechanisms are `microskills` (off by default), `toolprune` and `loopguard` (both on);
each takes `on` or `off`, and each has numbers you can tune with the same `config set` command
(in Claude Code's form here). Tool pruning is `auto` by default: once a tool never used here has
gone unused over `toolprune_evidence` prompts (ten) across at least `toolprune_min_sessions`
sessions (two), or over `toolprune_sessions` sessions however short (five), the plugin adds it
to the deny list without asking, and it is left out from the next session on. Prompts let long
sessions count for more; the two-session floor keeps one session's work from deciding alone. It
writes the list once your sessions in the repository have been idle for an hour
(`toolprune_quiet_minutes`): a conversation resumed after its tools change writes its whole
history to the prompt cache again instead of reading it, and after an idle hour that cache has
expired anyway. With `toolprune=propose` the plugin only proposes the list, and nothing changes
until you run `toolprune apply`.

```text
/tokeneconomizer:config set microskills=on              # learn and state repository facts
/tokeneconomizer:config set microskills_max_facts=2     # inject at most two facts
/tokeneconomizer:config set toolprune_evidence=20       # wait for twenty unused prompts
/tokeneconomizer:config set loopguard_nudges=2          # redirect twice before telling you
```

For one session only, set an environment variable when you start the agent:
`TE_DISABLE=1 claude` turns the whole plugin off, and `TE_LOOPGUARD=off codex` (likewise
`TE_MICROSKILLS`, `TE_TOOLPRUNE`) one mechanism. The plugin's model calls -- the loop guard's
judge, and micro-skills' proposer when it is on -- run through your own agent's CLI, on the same
plan or login as your sessions; `microskills=envfacts` keeps only the facts found without a model
call, and `loopguard=off` turns the judge off with the guard. Every setting, its default and its
environment variable: [Settings](plugin/README.md#settings).

## What it does on each agent

Micro-skills and the loop guard work the same way on every agent. Tool pruning acts through each
agent's own settings, so what it can leave out differs:

| Agent | Tool pruning leaves out | Written to |
|---|---|---|
| Claude Code | single tools | `.claude/settings.local.json` |
| Codex | capability packages | `.codex/config.toml` |
| Copilot CLI | whole MCP servers | `.github/copilot/settings.local.json` |
| Pi | nothing: Pi loads four tools | — |

Tool pruning writes only the entries it adds and never touches deny rules you wrote; a settings
file it creates is kept out of git (`.git/info/exclude`). The next session start says what it
left out. On Claude Code that line shows only in the detailed transcript (**Ctrl+O**), so
`/tokeneconomizer:toolprune` is the place to check. `toolprune restore` removes the plugin's
entries and no others, and what you give back stays back. If the plugin cannot edit a file
safely (a `.codex/config.toml` your repository commits, a settings file that is not valid JSON,
a path through a symlink), the list stays a proposal and the session start says why;
`toolprune apply` tries again. How each agent's hooks and settings were verified to behave is in
**[`docs/PLUGIN.md`](docs/PLUGIN.md)**, and how the package fits together in
**[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**.

### When a turn gets stuck

When a turn shows signs of looping -- the same failures repeating, or errors piling up -- the
loop guard asks its judge, and only if the judge finds the turn stuck does it add a redirect to
the model's context. (One exception: a turn far past this repository's usual length that also
keeps churning the same edits through the same failures is redirected even if the judge
disagrees.) If the turn is still stuck after that, it shows you one line, ending with how to stop
the turn:

> TokenEconomizer: this turn still looks stuck after a redirect (envelope, 12 tool calls). Press
> Esc to interrupt it.

It never stops a turn itself; whether to stop is up to you. Where the line shows, and how you
stop, in the agent's terminal UI and in a headless run (`claude -p`, `codex exec`, `copilot -p`,
`pi -p`):

| Agent | Interactive: shown | Stop with | Headless: shown | Stop with |
|---|---|---|---|---|
| Claude Code | in Claude's reply | Esc | in the final reply | Ctrl+C |
| Codex | in the conversation | Esc, then `/stop` | not shown | Ctrl+C |
| Copilot CLI | in the conversation | Esc twice | on stdout | Ctrl+C |
| Pi | as a notification | Esc | on stderr | Ctrl+C |

- **Claude Code** keeps a plugin's own lines to its detailed transcript (Ctrl+O), so the plugin
  asks Claude to repeat the line; a model may word it differently or leave it out.
- **Codex**: Esc interrupts the turn, but a command it started keeps running until `/stop`.
  `codex exec` prints no hook messages.
- **Pi**: Ctrl+C ends a headless run, but a command it started runs on until it finishes.

In a headless run nobody may be watching, so a flagged turn keeps running until it ends by
itself or reaches the agent's own limits; `te status` counts the redirects and "still stuck"
notices afterwards.

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
