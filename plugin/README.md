# TokenEconomizer plugin

How to install and use TokenEconomizer in Claude Code, Codex, the GitHub Copilot CLI and Pi. What
the plugin does and why is in the [main README](../README.md); its design and the verified
behaviour of each agent are in [`docs/PLUGIN.md`](../docs/PLUGIN.md).

- [Before you start](#before-you-start)
- Install and use it on [Claude Code](#claude-code) · [Codex](#codex) ·
  [Copilot CLI](#copilot-cli) · [Pi](#pi)
- [What to expect](#what-to-expect) · [Commands](#commands) · [Settings](#settings)
- [Repository map](#repository-map) · [Using with rtk](#using-with-rtk) ·
  [Model calls and API keys](#model-calls-and-api-keys) · [Troubleshooting](#troubleshooting)

## Before you start

- [ ] Your agent installed and signed in, any way it supports (a subscription or an API key)
- [ ] Python 3.10 or newer on `PATH` (or set `TE_PYTHON=/path/to/python3.10+`)
- [ ] Access to this repository on GitHub
- [ ] Copilot CLI only: the repository's folder trusted by Copilot (it asks the first time you
  start it there); Copilot ignores a folder's `.github/copilot` settings otherwise

No extra API key is needed. Plugins load when a session starts, so start a new session after
installing. Claude Code, Codex and the Copilot CLI install from a clone of this repository:

```bash
git clone https://github.com/microsoft/TokenEconomizer.git
```

Keep the clone where it is: the installed plugin runs from it.

> **Where commands go.** Lines starting with `/` (`/plugin …`, `/tokeneconomizer:…`, `/te …`)
> are typed into **the agent's prompt**, not your shell. Shell commands are in `bash` blocks.

## Claude Code

**1. Install.**

```bash
claude plugin marketplace add /path/to/TokenEconomizer
claude plugin install tokeneconomizer@tokeneconomizer
claude plugin list        # should show tokeneconomizer@tokeneconomizer ... enabled
```

(Inside Claude Code the same thing is `/plugin marketplace add …` and `/plugin install …`.)

**2. Check it.** Start Claude Code in any project (`cd my-project && claude`) and type
`/tokeneconomizer:status`. It should print a short status block, not an error. Claude Code may ask
permission to run the plugin's script; allow it always.

**3. Record the tool list, once.**

```text
/tokeneconomizer:toolprune refresh-catalog
```

This records which tools a Claude Code session loads, with no model call. Do it once on this
machine, and again after Claude Code updates. **Don't skip it:** without it the deny list sees
only the few tools Claude Code defers, not the ones every session loads.

**4. Work normally.** See [What to expect](#what-to-expect). When the deny list is ready, a
session start says so; then:

```text
/tokeneconomizer:toolprune            # the proposed tools and the evidence
/tokeneconomizer:toolprune apply      # accept them, from your next session
```

The rules go into this repository's `.claude/settings.local.json`, which the plugin keeps out of
git, and it never touches deny rules you wrote. `/tokeneconomizer:toolprune restore` undoes it.

Each slash command is one model turn. The same commands are free from a terminal, as
`~/.tokeneconomizer/bin/te <command>` run inside the repository ([Commands](#commands)). For one
session without installing anything: `claude --plugin-dir /path/to/TokenEconomizer/plugin`.

**Update.** `git pull` in the clone, then start a new session (or `/reload-plugins`). Claude Code
runs a plugin added from a local folder from that folder. If `/tokeneconomizer:status` still
shows the old behaviour, run `claude plugin update tokeneconomizer@tokeneconomizer`.

**Uninstall.**

1. In each repository where you accepted a deny list: `/tokeneconomizer:toolprune restore`.
2. `claude plugin uninstall tokeneconomizer@tokeneconomizer`, then
   `claude plugin marketplace remove tokeneconomizer`.
3. Optional: `rm -rf ~/.tokeneconomizer` to delete what it learned. (A line the plugin added to a
   repository's `.git/info/exclude` stays; it is harmless.)

## Codex

**1. Install.**

```bash
codex plugin marketplace add /path/to/TokenEconomizer
codex plugin add tokeneconomizer@tokeneconomizer
```

**2. Approve its hooks.** Start `codex` in a repository. Codex asks whether to trust the project,
then shows **Hooks need review**: choose **Trust all**. Hooks run from the next session on (the
session in which you approve gets no session-start injection). To approve later: `/hooks`.

**3. Check it.** Ask Codex *"what has TokenEconomizer learned here?"*, or name the skill:
`$tokeneconomizer-status`. Codex has no plugin slash commands; the plugin's commands are Codex
skills: `$tokeneconomizer-status`, `$tokeneconomizer-microskills`, `$tokeneconomizer-toolprune` and
`$tokeneconomizer-config`, each also used when you just ask. From a terminal, inside the
repository: `~/.tokeneconomizer/bin/te --host codex <command>` (the launcher appears after the
first session).

**4. Work normally.** See [What to expect](#what-to-expect). Codex's deny list removes the
sub-agent (`collaboration`), `goals` and image (`image_media`) packages, each only after it went
unused in every session where it was loaded; a package you used even once is kept. It is
written to the project's `.codex/config.toml`, which Codex reads **only in a trusted project**
(`toolprune apply` warns you otherwise, and runs `codex features list` to show the change took
effect). The web, asking you questions and the bundled skills are never proposed.
`toolprune measure` shows the difference with no model call.

In `codex exec`, Codex does not print hook messages, so the plugin's notices are not shown
there; a loop guard redirect still reaches the model. Codex cannot stop a turn from a hook, so
`loopguard=stop` acts as `nudge` on Codex.

**Update.** `git pull` in the clone, then `codex plugin add tokeneconomizer@tokeneconomizer`
again to refresh Codex's installed copy (your hook approval is kept).

**Uninstall.**

1. In each repository where you accepted a deny list:
   `~/.tokeneconomizer/bin/te --host codex toolprune restore` (it leaves an empty `.codex/`).
2. `codex plugin remove tokeneconomizer@tokeneconomizer`, then
   `codex plugin marketplace remove tokeneconomizer`.
3. Optional: `rm -rf ~/.tokeneconomizer`.

**A self-contained install.** Codex copies an installed plugin, and the copy uses the package in
your clone. To install a copy that carries the package itself, so the clone can move or go:

```bash
python3 /path/to/TokenEconomizer/scripts/build_plugin.py --out ~/te-marketplace
codex plugin marketplace add ~/te-marketplace
codex plugin add tokeneconomizer@tokeneconomizer
```

To update it, rebuild the bundle and run `codex plugin add tokeneconomizer@tokeneconomizer` again.

## Copilot CLI

**1. Install.**

```bash
copilot plugin marketplace add /path/to/TokenEconomizer
copilot plugin install tokeneconomizer@tokeneconomizer
copilot plugin list       # tokeneconomizer@tokeneconomizer ... (enabled)
```

Copilot loads the plugin from the clone directly, without copying it.

**2. Check it.** In Copilot's interactive mode, type `/tokeneconomizer:status`. Each slash command
is a model turn, and Copilot asks before running the plugin's script; choose **Yes**. Slash
commands work only in interactive mode; the plugin's notices also appear under `copilot -p`.

**3. Record the tool list, once.**

```text
/tokeneconomizer:toolprune refresh-catalog
```

With no model call. Do it once on this machine, and again after Copilot updates.

**4. Work normally.** See [What to expect](#what-to-expect). Copilot has no setting that removes a
single built-in tool, only one that turns off a whole MCP server, so the deny list proposes a
server only when **none** of its tools was used in about five sessions: one of yours from
`~/.copilot/mcp-config.json`, or the built-in `github-mcp-server`, which loads only when you are
signed in to GitHub. It is written to `.github/copilot/settings.local.json`. `toolprune measure`
shows the difference with no model call.

From a terminal, inside the repository: `~/.tokeneconomizer/bin/te --host copilot <command>` (the
launcher appears after the first session). Copilot cannot stop a turn from a hook, so
`loopguard=stop` acts as `nudge` on Copilot.

**Update.** `git pull` in the clone; Copilot loads the plugin from it, so nothing else is needed.

**Uninstall.**

1. In each repository where you accepted a deny list: `/tokeneconomizer:toolprune restore`.
2. `copilot plugin uninstall tokeneconomizer@tokeneconomizer`, then
   `copilot plugin marketplace remove tokeneconomizer`.
3. Optional: `rm -rf ~/.tokeneconomizer`.

**With your own model key (BYOK).** If Copilot answers *"Access denied by policy settings"*, your
organization's policy refuses its models, and Copilot can use your own provider instead. The
plugin works the same way there, except for its paid helpers (below). In your shell profile:

```bash
export COPILOT_PROVIDER_TYPE=anthropic                  # or openai, azure
export COPILOT_PROVIDER_BASE_URL=https://api.anthropic.com
export COPILOT_PROVIDER_API_KEY=...                     # your provider's key
```

Then start Copilot with a model id your provider knows, for example `copilot --model
claude-sonnet-5` (an Anthropic provider wants its own ids, such as `claude-haiku-4-5`, not the
subscription's `claude-haiku-4.5`). Through **OpenRouter**, keep Copilot on the Anthropic wire, so
prompt caching works, and give it both ids:

```bash
export COPILOT_PROVIDER_TYPE=anthropic
export COPILOT_PROVIDER_BASE_URL=https://openrouter.ai/api
export COPILOT_PROVIDER_BEARER_TOKEN=...                 # your OpenRouter key
export COPILOT_PROVIDER_MODEL_ID=claude-haiku-4.5        # the id Copilot configures itself for
export COPILOT_PROVIDER_WIRE_MODEL=anthropic/claude-haiku-4.5   # the id OpenRouter bills
```

Copilot does not pass these variables to the plugin's hooks, so under BYOK the paid helpers
(`paid=true`) cannot reach your provider: their calls answer nothing, and each leaves a line in
`~/.tokeneconomizer/errors.log`. Everything else, including `refresh-catalog` and `measure`,
needs no provider. `~/.tokeneconomizer/bin/te --host copilot doctor` shows the route it sees.

To check the whole plugin on your own setup:

```bash
~/.tokeneconomizer/bin/te --host copilot probe      # 7 short real sessions, in a temp repo
```

It builds a throwaway repository that is set up for pytest but has no pytest installed, and
whose tests pass under `python -m unittest`, and runs the same prompt in real Copilot sessions
with the plugin, under a state directory of its own. It reports, per session, whether the fact
the plugin learned was in the context and which test command the model ran first. Then it
applies the deny list it proposed, and shows the tool definition tokens of one more session
before restoring. It makes real model calls on your key or quota, and leaves those sessions in
Copilot's history.

## Pi

**1. Install.** The repository is a Pi package, so Pi installs it like any other:

```bash
pi install git:github.com/microsoft/TokenEconomizer
```

(Over SSH: `pi install git:git@github.com:microsoft/TokenEconomizer`. From a clone:
`pi install /path/to/TokenEconomizer`.)

**2. Check it.** Start a new Pi session and type `/te status`. The commands are `/te status`,
`/te microskills` and `/te config …`; from a terminal, inside the repository,
`~/.tokeneconomizer/bin/te --host pi <command>`. Tool pruning does not apply: Pi loads four
tools. In print and JSON modes the plugin's notices go to stderr.

**3. Work normally.** See [What to expect](#what-to-expect).

**Update.** `pi update --extensions` (plain `pi update` does not update extensions).

**Uninstall.** `pi remove git:github.com/microsoft/TokenEconomizer` (the source you installed
from); `pi config` turns it off without removing it. Optional: `rm -rf ~/.tokeneconomizer`.

## What to expect

- **Facts.** A fact forms once the same environment failure has come up in a few sessions (three
  confirming ones, among at least four where it applied), and is injected at every session start
  after that while the failure keeps occurring or keeps being prevented. A session start that
  adds facts says so once, in one line. `microskills` shows the facts injected and those forming.
- **Deny list.** Proposed after about five sessions in which a tool, package or server went
  unused. The session start after a new proposal forms says so once; nothing changes until you
  apply it, and it takes effect from the next session. `toolprune restore` removes only what the
  plugin added. A file the plugin creates is added to `.git/info/exclude`.
- **Loop guard.** Quiet on normal turns. On a turn that keeps failing the same way well past this
  repository's usual length, one redirect is attached to a tool result; after that, a notice (or,
  with `loopguard=stop` on Claude Code and Pi, a stop of a headless turn).
- **Cost.** `status` shows each session's tokens, priced after it ends, and the plugin's own
  overhead, which is zero unless you turn on the paid helpers.

## Commands

| Command | What it does |
|---|---|
| `status` | what it has learned in this repository, what it did, and what your sessions cost |
| `microskills` | the facts it injects at session start, and those still forming |
| `toolprune` | the proposed deny list and its evidence |
| `toolprune apply [ITEM …]` | accept the proposal (or the named items); from the next session |
| `toolprune restore [ITEM …]` | remove the plugin's entries |
| `toolprune refresh-catalog` | record the tools a session loads (Claude Code, Copilot) |
| `toolprune measure` | the request with and without the proposal |
| `config` | the settings: what is on, and every value in effect |
| `config set KEY=VALUE … [--global]` | change settings for this repository (or every one); see [Settings](#settings) |
| `config unset KEY … [--global]` | put a setting back to its default |
| `repomap` / `repomap refresh` | the cached repository map / make it now (see [Repository map](#repository-map)) |
| `doctor` | check Python, the state directory, the agents found and the error log |

How to run them:

| Agent | Inside the agent | From a terminal, in the repository |
|---|---|---|
| Claude Code | `/tokeneconomizer:<command>` | `~/.tokeneconomizer/bin/te <command>` |
| Codex | ask, or `$tokeneconomizer-status` (`-microskills`, `-toolprune`, `-config`) | `~/.tokeneconomizer/bin/te --host codex <command>` |
| Copilot CLI | `/tokeneconomizer:<command>` (interactive mode) | `~/.tokeneconomizer/bin/te --host copilot <command>` |
| Pi | `/te <command>` | `~/.tokeneconomizer/bin/te --host pi <command>` |

The launcher in `~/.tokeneconomizer/bin` is written by the first session; before that, use
`/path/to/TokenEconomizer/plugin/scripts/te`. From a plain terminal, `--host` defaults to the
agent that last worked in the repository.

## Settings

Each of the three mechanisms can be turned on or off on its own, and each has numbers you can
set by hand. Settings apply to the current repository, or to every repository with `--global`.
The examples use Claude Code's form; on the other agents run the same `config` command their way.

**Turn a mechanism on or off:**

```text
/tokeneconomizer:config set loopguard=off                 # the loop guard, in this repository
/tokeneconomizer:config set toolprune=off microskills=on
/tokeneconomizer:config set loopguard=off --global        # in every repository
/tokeneconomizer:config                                # what is on, and every value in effect
```

| Mechanism | Key | `on` means | Other values |
|---|---|---|---|
| Micro-skills | `microskills` | `envfacts`: facts found without model calls | `off`; `envfacts+distiller`, `distiller` add a paid fact proposer (need `paid=true`) |
| Tool pruning | `toolprune` | `propose`: proposed, applied only when you say so | `off`; `auto` applies a proposal as soon as it forms |
| Loop guard | `loopguard` | `nudge`: one mid-turn redirect | `off`; `stop` also stops a headless turn that keeps failing after the redirect (Claude Code, Pi) |

**Tune a mechanism:**

```text
/tokeneconomizer:config set microskills_max_facts=2            # inject at most two facts
/tokeneconomizer:config set toolprune_evidence=10              # wait ten sessions before proposing
/tokeneconomizer:config set loopguard_nudges=2 loopguard.min_turns=30
/tokeneconomizer:config unset loopguard.min_turns              # back to the default
```

| Key | Default | What it sets |
|---|---|---|
| `microskills_max_facts` | `4` | the most facts injected at session start |
| `microskills_max_tokens` | `150` | the size of the injected block, in tokens (about 4 characters each) |
| `microskills_propose_every` | `5` | sessions between runs of the paid fact proposer |
| `toolprune_evidence` | `5` | sessions a tool must go unused before it is proposed (three times as long for a few higher-risk tools; the web is never proposed) |
| `loopguard_aggr` | `default` | the guard's threshold preset: `conservative`, `default` or `aggressive` |
| `loopguard_nudges` | `1` | redirects per prompt-turn before the guard only notifies (or, with `loopguard=stop`, stops) |
| `loopguard.<name>` | from the preset | one guard threshold, set by hand (below) |

The guard calls a turn stuck when it keeps editing the same files and hitting the same
failures, well past how long this repository's turns that end with passing tests usually take
(the *envelope*). Until a repository has a few such turns, the preset's thresholds apply; after
that, some are learned from the repository's own turns. A value you set with `loopguard.<name>`
wins over both. Defaults at each preset:

| `loopguard.<name>` | `conservative` | `default` | `aggressive` | What it sets |
|---|---|---|---|---|
| `min_turns` | 28 | 20 | 14 | tool calls in a turn before it can count as stuck |
| `edit_churn` | 5 | 4 | 3 | edits to one file that count as churn |
| `distinct_edit_files` | 3 | 3 | 3 | files edited that count as scattered |
| `error_outputs` | 8 | 6 | 4 | failing tool results that count as sustained errors |
| `stagnation` | 0.30 | 0.20 | 0.12 | share of failures that repeat an earlier one |
| `envelope_pct` | 90 | 90 | 90 | percentile of the repository's passing turns the envelope starts from |
| `envelope_margin` | 1.5 | 1.25 | 1.0 | multiplier on that percentile |
| `envelope_min_samples` | 7 | 5 | 3 | passing turns needed before the envelope is used |
| `backstop_mult` | 3.5 | 2.5 | 1.8 | past this multiple of the envelope, a stuck turn is redirected, then stopped or flagged |
| `judge_confidence` | 0.85 | 0.7 | 0.55 | with `paid=true`: how sure the model judge must be |
| `gate_min_turns` | 24 | 18 | 12 | with `paid=true`: tool calls before the judge is asked |
| `gate_error_outputs` | 8 | 8 | 8 | with `paid=true`: failing results that ask the judge early |

**Repository map** (off by default; see [Repository map](#repository-map)):

| Key | Default | What it sets |
|---|---|---|
| `repomap` | `off` | `on`: a map ranked toward the session's first prompt; `start`: the cached map at session start |
| `repomap_tokens` | `2048` | the map's size, in tokens |
| `repomap_python` | auto | the Python that has aider installed |
| `repomap_timeout` | `8` | seconds the first prompt may wait for a ranked map before the cached one is used |

**Other settings:**

| Key | Default | Values |
|---|---|---|
| `paid` | `false` | allow the plugin's own model calls (see [Model calls](#model-calls-and-api-keys)) |
| `paid_model` | `""` | model for those calls; empty: `haiku` on Claude Code, the agent's own default model on Codex and Pi, `COPILOT_MODEL` on Copilot (unless it is `auto`) |
| `shadow` | `false` | decide and log everything, act on nothing |
| `mode` | `auto` | `interactive` or `headless` to override what the plugin detects |

**Where settings live, and when they apply.** For one session, an environment variable wins:
`TE_MICROSKILLS`, `TE_TOOLPRUNE`, `TE_LOOPGUARD`, `TE_LOOPGUARD_NUDGES`,
`TE_MICROSKILLS_MAX_FACTS`, and so on, and `TE_LOOPGUARD_THRESHOLDS='{"min_turns": 30}'` for the
guard's thresholds; `TE_DISABLE=1` turns the whole plugin off. Otherwise the repository's
settings win over the global ones, and those over the defaults; `unset` goes back one level. A
change applies from the agent's next hook call: the guard at the next tool call, facts and the
deny list at the next session start. A new fact-size cap shows once the next session has ended.

**Earlier names.** Before the mechanisms were renamed they were `inject`, `untool` and `spiral`,
with settings such as `inject_max_facts`, `untool_evidence`, `spiral_nudges`, `spiral.<name>` and
variables `TE_INJECT`, `TE_UNTOOL`, `TE_SPIRAL`, ...; all of them are still read (`spiral=guard`
as `loopguard=stop`), and a new name set alongside its old one wins. `config set` writes the new
name. The earlier commands `te facts` and `te untool`, and `/tokeneconomizer:facts` and
`/tokeneconomizer:untool`, still work.

## Repository map

An opt-in fourth mechanism: aider's map of the repository's key files and symbols (signatures
only, ranked by how much of the code refers to them), added to the session's context so the
agent knows where things are before it starts reading files. Turn it on per repository:

```bash
pip install aider-chat                      # or: pipx install aider-chat / uv tool install aider-chat
~/.tokeneconomizer/bin/te config set repomap=on      # add --host for Codex, Copilot or Pi
```

- **What goes in.** At most `repomap_tokens` tokens (default 2048), once per session. On Claude
  Code, the Copilot CLI and Pi it is ranked toward the identifiers in your first prompt; if that
  takes longer than `repomap_timeout` seconds (about 5 s on a repository of Django's size, well
  under a second on a small one), the cached map is used instead. Codex gets the cached map at
  session start. `repomap=start` does that on every agent and never delays a prompt.
- **What it reads.** The files `git ls-files` lists, in every language aider understands,
  without vendored code, minified files or files over 256 KB.
- **Where it runs.** In whichever Python has aider: `repomap_python` if you set it, else the one
  behind an installed `aider` command, else the plugin's own. `te doctor` says which it found.
  The plugin never imports aider itself.
- **What it writes.** Nothing in the repository. The map and aider's tag cache live in the
  plugin's state directory. The map is cached per commit and remade in the background when
  `HEAD` moves.
- **What it costs.** No model calls. The map is part of every request of the session, so it
  costs on sessions that already knew where to look and saves on those that would have searched.
  `te status` shows its size.

## Using with rtk

[rtk](https://github.com/rtk-ai/rtk) compresses shell output. Install it on its own (`rtk init
-g`); it does not need to be part of this plugin, and the two do not get in each other's way:
rtk rewrites shell commands before they run, and this plugin reads sessions, injects context and
watches tool results. The plugin reads `rtk`-prefixed commands as the commands they wrap, and
rtk adds no tools for the deny list to see. `te doctor` reports an installed rtk.

One interaction to know: with rtk 0.45.0, `rtk pytest` prints nothing when pytest is missing (it
exits with code 1). The agent sees no error and the plugin cannot learn the "`pytest` fails here;
`X` works" fact from it. Commands that name their interpreter (`.venv/bin/python -m pytest`) are
not rewritten, so the commands that fact recommends keep working.

## Model calls and API keys

**By default the plugin makes no model calls.** Micro-skills, tool pruning and the loop guard
all run locally.

With `paid=true`, three helpers can call a model: the fact proposer, the loop guard's judge and
the redirect writer. They run through **your own agent CLI** (`claude -p`; `codex exec` on
Codex; `copilot -p`, with every tool removed, on Copilot; `pi -p`, with nothing loaded but the
model, on Pi), so they use whatever your sessions already use: a Claude subscription or
Anthropic API key, your Codex login or OpenAI key, Copilot's subscription, or the provider and
login Pi is set up with. (Copilot with BYOK is the exception; see [Copilot CLI](#copilot-cli).)
Their cost is billed there too, and shown in `status`. A call that gets no reply leaves a line in
`~/.tokeneconomizer/errors.log`. `toolprune measure` on Claude Code makes its two calls the same
way, and only when you run it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| a command prints an import error, or the hooks seem to do nothing | run `~/.tokeneconomizer/bin/te doctor` (or `plugin/scripts/te doctor` in the clone) and read its errors.log lines |
| errors.log says the package "was not found for the plugin" | the clone the plugin was installed from moved or is gone: add it again, or use the [self-contained install](#codex) |
| `python 3.x (needs >= 3.10)` | install Python 3.10+ or set `TE_PYTHON` to one |
| the deny list proposes nothing after many sessions | run `toolprune refresh-catalog` (Claude Code, Copilot) |
| a Codex deny list was applied but nothing changed | trust the project in Codex; `~/.tokeneconomizer/bin/te --host codex doctor` says whether it is trusted |
| errors.log has `sidecall copilot: no reply` | the paid helpers cannot use a Copilot BYOK provider ([Copilot CLI](#copilot-cli)); turn `paid` off |
| `te` shows another agent's state | add `--host claude`, `codex`, `copilot` or `pi` |
