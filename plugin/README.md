# TokenEconomizer plugin

Runtime cost control for coding agents, from outside the agent. In each repository it works
in, it:

* **proposes removing tools you never use.** Tools that stay loaded but unused for several
  sessions are proposed as a deny list. You review it and apply it; nothing changes until you
  do. On Codex the unit is a capability package (sub-agents, goals, images); on the Copilot CLI
  it is a whole MCP server, such as the built-in GitHub server.
* **injects what it learned.** Environment facts your sessions keep rediscovering the hard way
  ("`tox …` fails here with command not found; `python -m pytest …` works") are added to the
  next session's context.
* **redirects a turn stuck in a failing loop.** When a turn keeps repeating the same failing
  edit/test cycle well past what this repository's turns usually take, the agent gets one
  mid-turn redirect.

Every action is announced in one line, every hook fails open, and by default the plugin makes no
model calls of its own. All of its state stays on your machine, under `~/.tokeneconomizer`.
Design, verified agent behaviour and limits: [`docs/PLUGIN.md`](../docs/PLUGIN.md).

## Before you start

- [ ] Claude Code installed (Codex, the Copilot CLI and Pi: see [below](#codex-copilot-and-pi))
- [ ] Python 3.10 or newer on `PATH` (or set `TE_PYTHON=/path/to/python3.10+`)
- [ ] Access to this repository on GitHub

No extra API key is needed. See [Model calls and API keys](#model-calls-and-api-keys).

## Steps (Claude Code)

**1. Get the plugin.** In a terminal:

```bash
git clone https://github.com/microsoft/TokenEconomizer.git
```

**2. Install it.** In a terminal, using the path of the clone from step 1:

```bash
claude plugin marketplace add /path/to/TokenEconomizer
claude plugin install tokeneconomizer@tokeneconomizer
claude plugin list        # should show tokeneconomizer@tokeneconomizer ... enabled
```

(Inside Claude Code the same thing is `/plugin marketplace add …` and `/plugin install …`.)
Plugins load when a session starts, so sessions already open won't have it; start a new one.

> **Where commands go.** Lines starting with `/` (`/plugin …`, `/tokeneconomizer:…`) are typed
> into **Claude Code's prompt**, not your shell. Shell commands are in `bash` blocks.

**3. Check it.** Start Claude Code in any project (`cd my-project && claude`) and type:

```text
/tokeneconomizer:status
```

It should print a short status block for the project, not an error. The first time, Claude Code
asks permission to run the plugin's script; choose to always allow it.

**4. Set up each repository, once.** In Claude Code, in the repository:

```text
/tokeneconomizer:untool refresh-catalog
```

This records which tools a session loads (no model call). **Don't skip it.** Without it the deny
list can only see the few tools Claude Code defers, not the ones every session loads.

Optional: `/tokeneconomizer:replay` estimates what the plugin would do on your past sessions in
this repository, with no model calls.

**5. Work normally.** You don't need to do anything. Across sessions the plugin learns which tools
this repository uses, learns environment facts, and prices each session after it ends.

**6. Accept the deny list (after about five sessions).** When a session starts you'll see a
notice like *"TokenEconomizer: N tool(s) went unused in this repository's recent sessions…"*.
Then:

```text
/tokeneconomizer:untool            # see the proposed tools and the evidence
/tokeneconomizer:untool apply      # accept them
```

It takes effect **from your next session**. The rules are written to this repository's
`.claude/settings.local.json`, which the plugin keeps out of git, and it never touches deny
rules you wrote yourself. To undo it, run `/tokeneconomizer:untool restore`.

**7. Check in whenever you like:** `/tokeneconomizer:status` and `/tokeneconomizer:facts`.

For one session without installing anything: `claude --plugin-dir /path/to/TokenEconomizer/plugin`.

## Commands

Inside Claude Code:

| Command | What it does |
|---|---|
| `/tokeneconomizer:status` | what it has learned in this repository, what it did, and what your sessions cost |
| `/tokeneconomizer:facts` | the facts it injects at session start, and those still forming |
| `/tokeneconomizer:untool` | the proposed deny list and its evidence |
| `/tokeneconomizer:untool apply` | accept the proposal (or `apply TOOL …` for specific tools); from the next session |
| `/tokeneconomizer:untool restore` | remove the plugin's deny rules (or `restore TOOL …`) |
| `/tokeneconomizer:untool refresh-catalog` | re-record the tools a session loads, e.g. after a Claude Code update |
| `/tokeneconomizer:untool measure` | prompt tokens per request with and without the proposal (two small model calls) |
| `/tokeneconomizer:replay` | what the plugin would do, replayed over this repository's past sessions |
| `/tokeneconomizer:config` | the settings: what is on, and every value in effect |
| `/tokeneconomizer:config set KEY=VALUE …` | change settings for this repository (add `--global` for all); see [Settings](#settings) |
| `/tokeneconomizer:config unset KEY …` | put a setting back to its default |

From a terminal, the same commands and the settings are
`/path/to/TokenEconomizer/plugin/scripts/te <command>`, run inside the repository:

| Command | What it does |
|---|---|
| `te status`, `te facts`, `te untool …`, `te replay`, `te config …` | as above |
| `te doctor` | check Python, the state directory, and the tool catalogs |

## Settings

Each of the three mechanisms can be turned on or off on its own, and each has numbers you can
set by hand. Settings apply to the current repository, or to every repository with `--global`.

**Turn a mechanism on or off:**

```text
/tokeneconomizer:config set spiral=off                 # the stuck-turn guard, in this repository
/tokeneconomizer:config set untool=off inject=on
/tokeneconomizer:config set spiral=off --global        # in every repository
/tokeneconomizer:config                                # what is on, and every value in effect
```

| Mechanism | Key | `on` means | Other values |
|---|---|---|---|
| Learned facts | `inject` | `envfacts`: facts found without model calls | `off`; `envfacts+distiller`, `distiller` add a paid fact proposer (need `paid=true`) |
| Unused-tool deny list | `untool` | `propose`: proposed, applied only when you say so | `off`; `auto` applies a proposal as soon as it forms |
| Stuck-turn guard | `spiral` | `nudge`: one mid-turn redirect | `off`; `guard` also stops a headless turn that keeps spinning after the redirect |

**Tune a mechanism:**

```text
/tokeneconomizer:config set inject_max_facts=2                 # inject at most two facts
/tokeneconomizer:config set untool_evidence=10                 # wait ten sessions before proposing
/tokeneconomizer:config set spiral_nudges=2 spiral.min_turns=30
/tokeneconomizer:config unset spiral.min_turns                 # back to the default
```

| Key | Default | What it sets |
|---|---|---|
| `inject_max_facts` | `4` | the most facts injected at session start |
| `inject_max_tokens` | `150` | the size of the injected block, in tokens (about 4 characters each) |
| `propose_every` | `5` | sessions between runs of the paid fact proposer |
| `untool_evidence` | `5` | sessions a tool must go unused before it is proposed (three times as long for a few higher-risk tools; the web is never proposed) |
| `spiral_aggr` | `default` | the guard's threshold preset: `conservative`, `default` or `aggressive` |
| `spiral_nudges` | `1` | redirects per prompt-turn before the guard only notifies (or, with `spiral=guard`, stops) |
| `spiral.<name>` | from the preset | one guard threshold, set by hand (below) |

The guard calls a turn stuck when it keeps editing the same files and hitting the same
failures, well past how long this repository's turns that end with passing tests usually take
(the *envelope*). Until a repository has a few such turns, the preset's thresholds apply; after
that, some are learned from the repository's own turns. A value you set with `spiral.<name>`
wins over both. Defaults at each preset:

| `spiral.<name>` | `conservative` | `default` | `aggressive` | What it sets |
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

**Other settings:**

| Key | Default | Values |
|---|---|---|
| `paid` | `false` | allow the plugin's own model calls (see below) |
| `paid_model` | `""` | model for those calls (`haiku` when empty) |
| `shadow` | `false` | decide and log everything, act on nothing |
| `mode` | `auto` | `interactive` or `headless` to override what the plugin detects |

**Where settings live, and when they apply.** For one session, an environment variable wins:
`TE_INJECT`, `TE_UNTOOL`, `TE_SPIRAL`, `TE_SPIRAL_NUDGES`, `TE_INJECT_MAX_FACTS`, and so on, and
`TE_SPIRAL_THRESHOLDS='{"min_turns": 30}'` for the guard's thresholds; `TE_DISABLE=1` turns the
whole plugin off. Otherwise the repository's settings win over the global ones, and those over
the defaults; `unset` goes back one level. A change applies from the agent's next hook call: the
guard at the next tool call, facts and the deny list at the next session start. A new fact-size
cap shows once the next session has ended.

## Model calls and API keys

**By default the plugin makes no model calls.** Learned facts, the deny list and the stuck-turn
check all run locally.

With `paid=true`, three helpers can call a model: the fact proposer, the stuck-turn judge and
the redirect writer. They run through **your own agent CLI** (`claude -p`; `codex exec` on
Codex; `copilot -p`, with every tool removed, on Copilot), so they use whatever your sessions
already use: a Claude subscription or Anthropic API key, your Codex login or OpenAI key, or
Copilot's subscription or BYOK provider. Their cost is billed there too, and it is shown in
`/tokeneconomizer:status`. `untool measure` makes its two calls the same way, and only when you
run it.

## Updating and uninstalling

**Update:** `git pull` in the clone, then `/plugin marketplace update tokeneconomizer` and
`/plugin install tokeneconomizer@tokeneconomizer`. Claude Code runs an installed **copy**, so
the pull alone changes nothing.

**Uninstall:**

1. In each repository where you accepted a deny list, run `/tokeneconomizer:untool restore`
   (Codex and Copilot: `te --host codex untool restore`, `te --host copilot untool restore`).
   The entries live in that repository's settings file and stay there otherwise.
2. `/plugin uninstall tokeneconomizer@tokeneconomizer`
3. Optional: `rm -rf ~/.tokeneconomizer` to delete what it learned.

## Codex, Copilot and Pi

The plugin runs on all three. What it can remove differs by agent.

| | Learned facts | Stuck-turn redirect | Removing unused tools |
|---|---|---|---|
| Claude Code | yes | yes; can also stop a headless turn | tools, in `.claude/settings.local.json` |
| Codex | yes | yes; a notice instead of a stop | capability packages, in `.codex/config.toml` |
| Copilot CLI | yes | yes; a notice instead of a stop | whole MCP servers, in `.github/copilot/settings.local.json` |
| Pi | yes | yes | no (Pi loads four tools) |

On every agent the deny list works the same way: it is proposed after about five sessions,
nothing changes until you run `untool apply`, it takes effect from the next session, and
`untool restore` removes only what the plugin added. A file the plugin creates is added to
`.git/info/exclude`.

**Codex.** Codex always copies an installed plugin, so install from the self-contained bundle:

```bash
python /path/to/TokenEconomizer/scripts/build_plugin.py --out ~/te-marketplace
codex plugin marketplace add ~/te-marketplace
codex plugin add tokeneconomizer@tokeneconomizer
```

Then approve its hooks once in `/hooks` inside Codex. Commands are
`~/.tokeneconomizer/bin/te --host codex <command>`, run inside the repository. The launcher is
written at the first session; until then, use `~/te-marketplace/tokeneconomizer/scripts/te`.

Codex's deny list removes the sub-agent (`collaboration`), `goals` and image (`image_media`)
packages, each only after it went unused in every session where it was loaded. A package you
used even once is kept. Two things to know:

- Codex reads a project's `.codex/config.toml` **only in a trusted project** (Codex asks the
  first time you start it in a repository). `te --host codex untool apply` warns you if the
  project is not trusted, and runs `codex features list` to show that the change took effect.
- The web and asking you questions are never proposed. The bundled skills are not proposed
  either: Codex honours that switch only in your user-level config, and the plugin changes
  per-repository files only.

`te --host codex untool measure` shows the difference with no model call. It costs nothing
because Codex renders its prompt locally.

**Copilot CLI.** Copilot loads a plugin from a local marketplace directly from the clone,
without copying it:

```bash
copilot plugin marketplace add /path/to/TokenEconomizer
copilot plugin install tokeneconomizer@tokeneconomizer
copilot plugin list       # tokeneconomizer@tokeneconomizer ... (enabled)
```

Copilot uses the plugin's own manifest (`plugin/.github/plugin/plugin.json`) and hook file. In
Copilot's interactive mode the slash commands work as on Claude Code (`/tokeneconomizer:status`,
`…:untool`, …), and the plugin's notices appear as status lines. `copilot -p` runs neither.
From a terminal, run `~/.tokeneconomizer/bin/te --host copilot <command>` inside the
repository. Every session start keeps that launcher pointing at the plugin.

Set up each repository once, as on Claude Code:

```bash
~/.tokeneconomizer/bin/te --host copilot untool refresh-catalog   # no model call
```

Copilot has no setting that removes a single built-in tool; it only has one that turns off a
whole MCP server. So the deny list proposes a server only when **none** of its tools was used
in about five sessions. The built-in `github-mcp-server` (18 tools on 1.0.83) is the usual
candidate. `te --host copilot untool measure` shows the difference for your setup. It costs
nothing, because the plugin captures the request Copilot would send on your machine, and no
model is called.

**Copilot with your own model key (BYOK).** If Copilot answers *"Access denied by policy
settings"*, your organization's policy refuses its models. Copilot can use your own provider
instead, and the plugin works the same way there. Set this in your shell profile:

```bash
export COPILOT_PROVIDER_TYPE=anthropic                  # or openai, azure
export COPILOT_PROVIDER_BASE_URL=https://api.anthropic.com
export COPILOT_PROVIDER_API_KEY=...                     # your provider's key
```

Then start Copilot with a model id your provider knows, for example `copilot --model
claude-sonnet-5` (an Anthropic provider wants its own ids, such as `claude-haiku-4-5`, not the
subscription's `claude-haiku-4.5`).

Through **OpenRouter**, keep Copilot on the Anthropic wire, so prompt caching works, and give it
both ids:

```bash
export COPILOT_PROVIDER_TYPE=anthropic
export COPILOT_PROVIDER_BASE_URL=https://openrouter.ai/api
export COPILOT_PROVIDER_BEARER_TOKEN=...                 # your OpenRouter key
export COPILOT_PROVIDER_MODEL_ID=claude-haiku-4.5        # the id Copilot configures itself for
export COPILOT_PROVIDER_WIRE_MODEL=anthropic/claude-haiku-4.5   # the id OpenRouter bills
```

The plugin's hooks inherit the variables. Its paid helpers (`paid=true`) go to the same
provider, on `paid_model`, or on your `COPILOT_MODEL` unless that is `auto`, or on
`claude-haiku-4-5` for an Anthropic provider (`anthropic/claude-haiku-4.5` through OpenRouter).
`refresh-catalog` and `measure` never call your provider. `te --host copilot doctor` shows the
route it sees.

To check the whole plugin on your own setup, run it once:

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

**Pi.** This directory is also a Pi package:

```bash
pi install /path/to/TokenEconomizer/plugin
```

Inside Pi: `/te status`, `/te facts`, `/te replay`, `/te config …`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/tokeneconomizer:status` prints an import error | reinstall (see Updating); the copy is stale or incomplete |
| it prints nothing useful, or the hooks seem to do nothing | run `te doctor`; check `~/.tokeneconomizer/errors.log` |
| `python 3.x (needs >= 3.10)` | install Python 3.10+ or set `TE_PYTHON` to one |
| the deny list proposes nothing after many sessions | run `/tokeneconomizer:untool refresh-catalog` (step 4); on Copilot, `te --host copilot untool refresh-catalog` |
| a Codex deny list was applied but nothing changed | trust the project in Codex; `te --host codex doctor` says whether it is trusted |
