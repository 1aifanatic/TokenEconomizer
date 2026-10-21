# The plugin: design and verified agent behaviour

How the plugin runs the three mechanisms inside your own Claude Code, Codex, Copilot CLI and Pi
sessions, through the hooks (on Pi, the extension events) each agent exposes: what it does at
each hook, what differs from the experiments and why, the agent behaviour each choice rests on,
where its state lives, and its known limitations. The code is `tokeneconomizer/plugin/` (what
the hooks run) and `plugin/` (the packaging for each agent). To install and use it, see
[`plugin/README.md`](../plugin/README.md).

---

## 1. What it does, hook by hook

| Hook | What the plugin does | Mechanism it drives |
|---|---|---|
| `SessionStart` | Injects the repository's micro-skills (a cached render) as `additionalContext`; announces changed facts or a new deny-list proposal in one `systemMessage` line; folds any earlier session whose end was never seen | microskills |
| `UserPromptSubmit` | Opens a prompt-turn for the loop guard | loopguard |
| `PostToolUse` | Folds the call into the turn's guard; delivers a nudge as `additionalContext`, or stops the turn | loopguard |
| `PostToolUseFailure` | The same, for a call that failed (Claude Code and Copilot report a failed call only here) | loopguard |
| `Stop` | Closes the turn; a turn that *ended green* feeds the per-repository envelope | loopguard |
| `SessionEnd` | Hands the session to a detached `te fold` and returns at once | microskills, toolprune, ledger |

`te fold` runs the **same classes the experiments ran** over the finished session's log:
`PrefixStage` (envfacts, plus the paid proposer when allowed) and `ToolingStage` (evidence
pruning). The loop guard (`loopguard.TurnGuard`) is the benchmark harness's `NudgeGuard` control
flow — `SpiralState.update`, `assess_abort`, `is_stuck`, `nudge_text`, the cumulative backstop —
one tool call at a time. None of the thresholds were re-tuned. Three things it adds:

* It counts **edits made through the shell**: `sed -i`, `perl -pi`, a redirect or `tee` into a
  source file, an inline Python script that writes a file back, `patch`/`git apply`. The loop
  state counts only edit tools, and agents that edit through Bash then looked as if they had
  edited nothing.
* **The nudge always comes before the stop.** The benchmark ladder checks a hard backstop (a
  multiple of the learned envelope: 2.5 × at the default level, 1.8 × at `aggressive`) before
  anything else. With the large envelopes of the benchmark streams, the backstop lay well past
  the stuck rule's own floor (20 calls at the default level, 14 at `aggressive`), and the nudge
  came first. An interactive repository learns a small envelope. Its backstop then falls inside
  that floor, so the first call that counts as stuck has also tripped the backstop, and the turn
  would be stopped (or, in nudge mode, merely flagged) without ever being redirected. Now an
  unspent nudge goes first, and the stop needs a second stuck segment after it.
* With `paid` on, the **stuck judge and nudge writer run in a detached `te judge` job**, not in
  the hook. The hook starts the job and returns; a later PostToolUse picks up the verdict, with
  the redirect already written. Each job's spend is charged to the ledger when it is spent.

Every hook fails open: any error becomes an empty output (no change) and a traceback in
`~/.tokeneconomizer/errors.log`. The hooks are stdlib only, and none waits on a model call.

## 2. What changes relative to the experimental setting

| | Experiments | Plugin | Why |
|---|---|---|---|
| Unit of work | one benchmark task | a **session** for micro-skills and tool pruning; a **prompt-turn** for the loop guard | In interactive use a person already splits work into short turns. A whole session folded as one trajectory would look stuck merely by being long |
| Learning signal | the official grader's `resolved` | the envelope learns from turns that **ended green**: the agent stopped on its own, was not stopped by the guard, and no verification after its last edit failed | There is no grader. Micro-skills and evidence pruning never needed one |
| Injection channel | a prefix on the task prompt | `SessionStart` `additionalContext` | Verified to reach the model (§3) |
| Tool removal | `--disallowed-tools` / Codex config at launch | a **deny list you approve**, written to the repository's own settings file — tools in `.claude/settings.local.json`, Codex capability packages in `.codex/config.toml`, Copilot MCP servers in `.github/copilot/settings.local.json` — effective from the **next** session | The plugin launches nothing. Tools are fixed at session start, and changing them mid-session would re-write the cached prompt prefix. Each agent's file can express a different unit (§3) |
| Pruning candidates | `toolprune.tools.INAPPLICABLE_TOOLS` — orchestration, Q&A, plan mode, web — "inapplicable by construction" for a sealed headless task | nothing is inapplicable by construction; only **evidence** (loaded but unused for N sessions) proposes a tool, and a person's tools — AskUserQuestion, plan mode, sub-agents, the web — are protected | A person asks questions, plans and browses in a real session. A deny rule the plugin wrote for a tool that has since become protected is given back at the next fold |
| Network | every arm, baseline included, carries an answer-key guard — a `PreToolUse` hook and an `/etc/hosts` block refuse the benchmarks' upstream sources — and drops the web tools | **no guard**: no `PreToolUse` hook (on Pi, no `tool_call` handler), no blocked command or host; the web tools stay | A benchmark's answer is a fetch away. A real session has no answer key, and the web is part of the work |
| Nudge | kill the agent, resume it with a continuation prompt | a mid-turn reminder attached to the tool result | Different intervention, so its effect is not inherited from the experiments |
| Abort | terminal, in autonomous runs | a `continue: false` stop only in **headless** sessions with `loopguard: stop`; in an interactive session, a one-line notice — the person is already the abort | The benchmark guard's own design note: "interactive callers should map it to a hand-off" |
| Optimizer model | the agent's own model, so overhead could not look cheaper than its arm | the cheapest capable model on the user's own agent CLI (`paid_model`, default `haiku`), and only when `paid` is on | Every paid call is still charged to the session ledger |

## 3. Verified agent behaviour

What the code relies on about each agent, and how it was checked. Each block names the version
and date it was verified on; §5 says what has not been exercised.

**Claude Code 2.1.286 — verified with a local plugin (`--plugin-dir`) on Haiku, 2026-09-30:**

* `SessionStart` `additionalContext` reaches the model. End to end through this plugin: four
  synthetic sessions taught it that bare `pytest` is missing; a real `claude -p` session in that
  checkout then answered "Tests should be run with `python -m pytest` since `pytest` command is
  not found in this environment", and the detached fold priced that session from its transcript.
* `PostToolUse` `additionalContext` is followed **in the same turn** — no restart needed.
* `PostToolUse` `{"continue": false}` halts the loop after the current tool (one of three
  requested commands ran) and the session ends cleanly.
* A bare-name `permissions.deny` rule **removes the tool from the session's tool list**, the same
  list `--disallowed-tools` produces, whether the rule comes from `--settings` or from a
  project's `.claude/settings.local.json`. A plugin's own `settings.json` cannot carry deny rules
  (only `agent` and `subagentStatusLine` take effect), which is why the deny-list writer edits the
  project-local file, with approval.
* An interactive transcript does not record the tool list a session loaded. Without a recorded
  catalog, evidence pruning sees only the tools Claude Code defers (one listing line each) and
  those a session used; `te toolprune refresh-catalog` records the always-loaded set, with no model
  call.
* **A call that fails fires `PostToolUseFailure`, not `PostToolUse`** (verified on 2.1.299,
  2026-10-14): `{tool_name, tool_input, tool_use_id, error, is_interrupt, duration_ms}`, where
  `error` is the text the model saw (`"Exit code 2\nls: cannot access ..."`). Until the hook
  file registered it, the guard never saw a failing call: live sessions counted 39 of 47
  calls, and a failing test run after the last edit could not make a turn end red.
* Bash `tool_response` is `{stdout, stderr, interrupted, isImage, noOutputExpected}` — **no exit
  code**, so on Claude Code failure is read from the output text, as the loop state's detectors do.
* Interactive transcripts parse with the existing `trace.load_trajectory`, but write **one entry
  per content block, each repeating its message's usage**, so an assistant message id can repeat
  many times; the ledger de-duplicates by message id. Subagents log to
  `<session>/subagents/`, and their calls and tokens are counted with their session.
* A `claude -p` session's hooks see `CLAUDE_CODE_SESSION_ATTENDED=0` and
  `CLAUDE_CODE_ENTRYPOINT=sdk-cli`; `mode: auto` uses them to tell headless from interactive.

**Codex 0.153.1 — verified live with `codex exec`** (`gpt-5.6-sol`, direct OpenAI API key, an
isolated `CODEX_HOME`, `--dangerously-bypass-hook-trust`; 2026-09-30):

* All five hooks fire — `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd` —
  and both `SessionStart` and `PostToolUse` `additionalContext` reach the model (a probe's
  codeword and its mid-turn instruction both came back in the answer).
* `PostToolUse` fires for shell commands as `tool_name: "Bash"`, `tool_input: {"command": "..."}`,
  with `tool_response` a plain string and no exit code. Issue #38850 (PostToolUse not firing in
  Code Mode) did not reproduce on this build and model.
* The plugin installs through Codex's own commands: `codex plugin marketplace add <dir>` reads
  `.claude-plugin/marketplace.json`, and `codex plugin add tokeneconomizer@tokeneconomizer`
  **copies** the plugin into `$CODEX_HOME/plugins/cache/…` and enables it, dropping the `lib`
  symlink to the package; the bootstrap then finds the package in the checkout `config.toml`
  records as the marketplace's `source` (verified 2026-10-14). `"${PLUGIN_ROOT:-…}"` in the hook
  command resolves to that copy.
* End to end: four synthetic rollouts taught it that bare `pytest` is missing; a real Codex
  session then answered "Run tests with `python -m pytest …` because the `pytest` command is
  unavailable here", the guard folded the turn's tool call, and SessionEnd's detached fold priced
  the session from its rollout: fresh, cached, written and output tokens matched the stream's own
  usage block, converted as `te_harness.codex.row_usage` converts it.
* Hooks inherit the agent's environment, including a parent Claude Code session's `CLAUDE_*`
  variables, which is why the host is detected from the payload (`turn_id`), never the env.
* `CODEX_HOME` under `/tmp` makes Codex refuse to install its helper binaries, and its sandbox then
  fails every command (`codex-linux-sandbox: No such file`); keep it out of temporary dirs.
* Codex also turns a plugin's `commands/` folder into skills of its own (`source-command-status`,
  …), which listed every command twice in its skill picker beside `codex-skills/`. The slash
  commands therefore live in `slash-commands/`, which the Claude Code and Copilot manifests name
  and Codex does not read (verified 2026-10-17).
* Codex lists a plugin's skills under the plugin's name, `tokeneconomizer:<skill>` (every release
  from 0.131.0, the first with `codex plugin add`, to 0.163.0), and in `codex exec` a `$` mention
  matches only that full name: `$tokeneconomizer-status`, the name these guides once gave, reached
  no skill. The skills are therefore named after the commands, so `$tokeneconomizer:status` reads
  as `/tokeneconomizer:status` does on the other agents (verified 2026-10-21, with arguments:
  `$tokeneconomizer:config set loopguard=off --global`). Releases before 0.141.0 install the
  plugin but never run its hooks (tried: 0.131.0, 0.135.0, 0.140.0).
  A question in plain words is a weaker route: on `gpt-5.4-mini`, *"what has TokenEconomizer
  learned here?"* ran the skill's command in none of six `codex exec` runs, under either naming
  (the model read the plugin's state files instead), so the guides name the skill.

Hooks are **trust-gated**: skipped until approved in `/hooks` (stored as
`[hooks.state.<key>].trusted_hash`), unless the run passes `--dangerously-bypass-hook-trust`.
That gate, kebab-case event names and an argv-list handler are why `te_harness` once recorded
that Codex "honours no PreToolUse hook"; with PascalCase events, a command string and trust,
every hook fires. Not exercised, from the binary's schemas and the docs: `PostToolUse`
`continue: false` does **not** stop a Codex turn mid-flight, so on Codex the guard's last rung
is a notice, matching the Codex-native finalize policies, which never abort. Code Mode presents
one `exec` tool with nested descriptions and defers MCP tools, so Codex pruning works on
capability packages (below), not on single tool schemas.

**Codex pruning — verified on 0.153.1, 2026-10-07** (`codex debug prompt-input` renders a
session's prompt input locally, with no model call; `codex features list` prints the effective
feature flags):

* A project's `.codex/config.toml` is read **only in a trusted project**. The same toggles
  changed the rendered prompt input under a `CODEX_HOME` that trusted the checkout, and changed
  nothing in an untrusted one, including a subdirectory of a trusted path. `te --host codex
  toolprune apply` checks trust and runs `codex features list` to confirm.
* The project-local toggles for `collaboration` (`[agents] enabled`, `[features] multi_agent`,
  `include_collaboration_mode_instructions`), `goals` and `image_media` take effect. `goals` and
  `view_image` remove declarations inside the code-mode tool description, which the render does
  not show, so `toolprune measure` understates them, and its output says so.
* `[[skills.config]] enabled = false` hides the bundled skills listing from the user-level
  config only. A per-repository decision does not belong there, so `skills` is not offered.
* The plugin writes each toggle as a line marked `# tokeneconomizer:<capability>` into the
  file. It never overrides a key you already set, and `restore` removes exactly its own lines.
* Code mode routes every call through one `exec` tool (`tools.view_image(…)`,
  `tools.web__run(…)`), recorded as `response_item` `custom_tool_call` records, not as rollout
  items. The fold therefore reads a capability trace that includes them.
* In a live session (gpt-5.6-sol on a Codex login), after `te --host codex toolprune apply
  collaboration goals image_media` the model reported no sub-agents, image viewer or goals, and
  could no longer view an image in the repository; `toolprune restore` returned the folder to its
  earlier state. Codex loaded those runs' hooks from the repository's `.codex/hooks.json`. A run
  trusts a project with `-c 'projects={"<path>"={trust_level="trusted"}}'`; the dotted `-c` form
  does not take.

**Copilot with a real model — 2026-10-07, over BYOK.** These checks ran on Copilot's BYOK mode
(`COPILOT_PROVIDER_TYPE=anthropic`, `_BASE_URL`, `_API_KEY`) with `claude-haiku-4-5`, through
`te --host copilot probe` (`plugin/probe.py`, which any user can run on their own setup):

* **Settings apply only in a trusted folder.** Copilot ignores a repository's `.github/copilot`
  settings outside its `trustedFolders`: a deny list applied in an untrusted directory under
  `/tmp` changed nothing, and a local capture showed the same file taking effect under a trusted
  folder. `toolprune apply` therefore warns about an untrusted folder and confirms the effect with a
  local capture, `doctor` reports trust, and the probe builds its repository inside a trusted
  folder.
* Copilot's `session.shutdown` records the request's tool-definition size as
  `toolDefinitionsTokens`; the probe reports it before and after the deny list.
* **Usage under BYOK** keeps the grand-total form: with an Anthropic provider, `inputTokens`
  includes the `cacheWriteTokens`, which the ledger splits out.
* **Paid side calls under BYOK.** With `COPILOT_MODEL=auto` (the subscription's router) in the
  environment, `copilot -p` without a model answered nothing. The side call now names a model:
  `paid_model`, else a concrete `COPILOT_MODEL`, else `claude-haiku-4-5` on an Anthropic
  provider. It answered, and was priced from its own session log. The local capture behind
  `refresh-catalog` and `measure` mimics the person's model family, so it records the same
  built-in tools (`edit`/`create` for Claude models, not `apply_patch`). Copilot 1.0.83 does not
  pass its `COPILOT_PROVIDER_*` variables to the hooks, so with BYOK set through them the side
  calls run signed out; they answer nothing and say so in `errors.log`.

**GitHub Copilot CLI 1.0.83 — verified live, 2026-10-07**, against a scripted OpenAI-compatible
endpoint on localhost (Copilot's BYOK mode, `COPILOT_PROVIDER_BASE_URL`). Every request the CLI
built was recorded, so whether a hook's output reached the model was read off the request
itself, with no model and no quota:

* Copilot loads a Claude-format plugin, and prefers `.github/plugin/plugin.json` to
  `.claude-plugin/plugin.json`: with both present, only the hook file the first one names
  ran. That is how the plugin gives Copilot its own `hooks/copilot-hooks.json` (`--host
  copilot`, plus `PostToolUseFailure`) while Claude Code keeps `hooks/hooks.json`.
  `copilot plugin marketplace add <checkout>` and `copilot plugin install` load the plugin
  live from the checkout ("nothing was copied"), and the installed copy's hooks all fired.
* PascalCase events receive Claude-shaped snake_case payloads: `session_id`, `cwd`,
  `hook_event_name`. `SessionStart` has `source: "new"`, and there is no turn id. Only `Stop`
  carries `transcript_path` (`~/.copilot/session-state/<id>/events.jsonl`). Tool names are
  Claude's (`Bash`, `Read`). A shell result is `tool_result.text_result_for_llm`, ending in
  `<shellId: N completed with exit code X>`. A failed tool fires `PostToolUseFailure` with an
  `error` string.
* Context reaches the model only as a **top-level `additionalContext`**, on `SessionStart`,
  `UserPromptSubmit` and `PostToolUse` (the last as "Additional guidance from postToolUse
  hooks"). `hookSpecificOutput.additionalContext` is ignored.
* `continue: false` from `PostToolUse` did **not** stop a turn: all three scripted calls ran.
  As on Codex, the guard's last rung is a notice.
* The session log is the `tool.execution_*` stream the trace seam already reads. Usage arrives
  in `session.shutdown` (`modelMetrics[].usage`), written just after the `SessionEnd` hook
  returns, so the detached fold waits for it. `inputTokens` is the grand input total: a scripted
  4 × (1,000 prompt, 600 cached) became `inputTokens` 4,000 and `cacheReadTokens` 2,400.
* End to end, through the installed plugin: four sessions that hit "bare `pytest` is missing"
  taught it the fact, and in the sessions after them the fact was in the request the model
  received. Every session was folded and priced from its own `session.shutdown`.
* **The interactive TUI** (driven in a pseudo-terminal, same local endpoint). `SessionStart`
  fires lazily, at the first prompt; `SessionEnd` fires **once per session**, at `/exit`, not per
  prompt. `systemMessage` is **never shown**, so a notice was invisible until the plugin printed
  it as a `{"type": "progress", "message": …}` line ahead of its JSON. Copilot displays that as a
  status line (verified on SessionStart, UserPromptSubmit and PostToolUse), and the final object
  still applies. Plugin slash commands **do** run in the TUI, as skills. `-p` does not expand
  them. Copilot substitutes only `$ARGUMENTS` in a command's text: `${CLAUDE_PLUGIN_ROOT}`,
  `${PLUGIN_ROOT}`, `${COPILOT_PLUGIN_ROOT}` and `{{plugin_root}}` arrive literally, and its shell
  exports none of them. So every session start now keeps a launcher at
  `~/.tokeneconomizer/bin/te`, and the commands fall back to it when the substituted path is not
  there. Through it, `/tokeneconomizer:status` printed Copilot's status in Copilot, and Claude
  Code's still printed Claude's.
* A side effect to know when scripting Copilot's TUI: under an editor's terminal (`TERM_PROGRAM`
  set), its first run offers to add a shift+enter binding and writes that editor's
  `keybindings.json` if accepted.
* Untooling. Copilot has no persistent per-tool filter: `--excluded-tools` is a launch flag,
  and `permissions.deny` refuses a call but leaves the schema in the request. What does work,
  from the repository's `.github/copilot/settings.local.json`, is `disabledMcpServers`: listing
  the built-in `github-mcp-server` removes all of that server's tools from the request.
  `githubMcpTools`, `disabledSkills` and `builtInAgents` changed nothing. The plugin therefore
  proposes a server only when none of its tools was used. With the catalog recorded
  (`refresh-catalog`, captured the same way, with no model call), five sessions that never used
  the GitHub tools produced that proposal, and the next session's request no longer carried the
  server's tools.

**Pi 0.99.2 — verified live with `pi --print`** (`@earendil-works/pi-coding-agent`, the
extension loaded with `--extension`, gpt-5.6-sol through an `openai-responses` provider in an
isolated `PI_CODING_AGENT_DIR`):

* Pi has no hook protocol; `plugin/pi/tokeneconomizer.ts` speaks the dispatcher's on Pi's
  behalf — `session_start`, `before_agent_start`, `tool_result`, `agent_end` and
  `session_shutdown` each become one `te-hook --host pi` call — and applies the answers with
  Pi's own levers: a system-prompt section, a steering message, `ctx.abort()`, a UI notice.
* End to end: four synthetic Pi session files taught it that bare `pytest` is missing; in a real
  session the `<tokeneconomizer>` section appeared in the session's recorded system prompt, the
  model answered "Tests should be run with `python -m pytest …` because the `pytest` command is
  unavailable here", the guard folded its `bash` call, and the detached fold priced the session
  from Pi's own session file.
* The section is re-applied on every prompt: Pi diffs prompt sections between runs and records
  the change, so one left out would be recorded as removed. Unchanged, it adds nothing after
  the first prompt.
* Pi's bash result carries a real exit code (`structuredContent.exit_code`), which the
  ended-green proxy uses; its session file is a tree, and only the active branch is read.
* Not exercised live: a nudge (`sendMessage` with `deliverAs: "steer"`) and a stop
  (`ctx.abort()`) — the dispatcher's side is covered by the offline tests, Pi's by its API.
  Untooling does not apply: Pi declares four tools by default.

**Pi 1.2.0 — verified live, 2026-10-14** (Haiku 4.5 through Pi's built-in OpenRouter provider,
set as the default provider and model in an isolated `PI_CODING_AGENT_DIR`, as a Pi user's own
settings would be):

* `pi install` of the repository root — the manifest a `git:` source reads after cloning —
  listed the package and loaded the extension. In a `--print` session its events reached the
  dispatcher, the turn closed green, the session folded and was priced from Pi's own session
  file, and `errors.log` stayed empty.
* `/te status` answered as a `notify` over RPC (the terminal UI's notice), and on stderr in
  print mode, leaving stdout to the reply.
* A paid side call — `pi --print --mode json` with no tools, extensions, skills, prompt
  templates, context files, MCP servers or saved session, and no `--model` — ran on the default
  model and returned its text and usage; the plugin's own extension did not load inside it. Pi
  0.99.2 rejects `--no-mcp`, so the call retries without it.
* Not yet exercised: `pi install git:github.com/…` against GitHub itself, which needs the
  root manifest pushed.

**End to end, as a new user, 2026-10-14** — on each agent a fresh home followed `README.md` and
this plugin's guide step by step (Claude Code 2.1.300, Codex 0.153.1, Copilot CLI 1.0.83, Pi
1.2.0; every model through OpenRouter; a small Python repository where `pytest` is missing and
`python -m pytest` works):

* Every agent: native install, hooks firing in new sessions, every session folded and priced,
  facts injected and changing the first test command, every command and its earlier name,
  settings under old and new names, uninstall leaving no hook running.
* The deny list end to end on Claude Code (20 → 10 tools), Codex (`codex debug prompt-input`
  7,021 vs 10,397 characters) and Copilot (a user MCP server gone from the next session);
  `restore` gave each back and kept the person's own entries.
* The loop guard's redirect reached the model mid-turn on all four; on Pi a `steer` message, and
  with `loopguard=stop` a headless turn ended at the next stuck verdict (nudged at call 17,
  stopped at 35) — the first live check of both on Pi. On Codex and Copilot the second fire is
  a notice.
* Paid side calls ran through each agent's own CLI (Copilot's only off BYOK, above) and were
  charged to the ledger, without recursing into the plugin.
* Pi's real git install (a local git daemon standing in for GitHub): cloned, `npm install`ed,
  listed, loaded; `pi update --extensions` fast-forwarded it; `pi remove` removed it.

What the run found, now fixed: Claude Code 2.1.300's `api-request-blob` entries doubled every
call read from its transcript; a working form was credited from runs that needed a
`VAR=value` it leaves out, once per failure × success, and never debited; Pi's and Copilot's
logs gave no per-call directory, so `cd <repo> && …` was learned as a separate form; the guard
counted file reads as failures; an absolute-path test runner was not a verification; facts
needed sessions that edited different files and, once injected, decayed (the fold never knew
what the session was given); a Codex install copy had no package; side calls failed silently
and left sessions in Claude Code's history; `te` without `--host` showed Claude Code's state.

**Again as a new user, interactively, 2026-10-21** -- one fresh home for all four agents (Claude
Code 2.1.306, Codex 0.163.0, Copilot CLI 1.0.101, Pi 1.3.0; every model through OpenRouter),
the main README's Quick start typed as written. `git clone https://github.com/microsoft/…` and
`pi install git:github.com/microsoft/…` ran unchanged, a `url.insteadOf` in the new user's
`~/.gitconfig` pointing them at a local repository of the tree. Each agent then ran in its own
terminal UI: the first-run prompts, every Everyday command, the Update row, and a session that
ended and was folded and priced. What the run showed:

* Claude Code asks a new user to trust the folder (**Yes, I trust this folder**); its default
  auto mode then ran the plugin's script with no prompt.
* Codex asks for the folder (**Trust and continue**), then **Hooks need review**: **Review
  hooks**, **Trust all and continue**, or continue without them. Its default sandbox keeps
  `~/.tokeneconomizer` read-only, and `$tokeneconomizer:config set …` failed with a traceback
  until the skills that change state asked to leave the sandbox (`sandbox_permissions:
  require_escalated`); Codex now asks the user, and `te` refuses a change it could only half
  make. `/exit` keeps a session open to `codex resume` and sends no `SessionEnd`, so such a
  session is folded at a later session start once quiet; `codex debug prompt-input` sends a
  `SessionEnd` for a thread that never started, which the plugin now ignores.
* Copilot offers **Yes, and remember this folder for future sessions**, which a deny list in
  `.github/copilot` needs. It substituted a slash command's arguments into `$ARGUMENTS`, yet the
  model ran `config show` for `config set loopguard=off` three times: the command's text said
  "use `show` when no arguments were given" and "do not run `set` … unless those words appear in
  the arguments above". Rewritten to name the typed arguments plainly, it ran what was typed.
* Pi announced `pi update --extensions` itself when the repository moved on, and `/te` answered
  in its terminal UI with no model call.

## 4. State and privacy

All state is local, under `$TOKENECONOMIZER_HOME` (default `~/.tokeneconomizer`), one directory
per repository and host: the mechanisms' own state, `ledger.jsonl` (one priced record per
session), `events.jsonl` (every injection, nudge, stop), and per-session guard state.
Uninstalling the plugin leaves this directory; delete it to forget. `te repos` lists what it
holds.

Which repository a session belongs to (`plugin/paths.py`, `plugin/repos.py`):

* **One key per repository.** A checkout with an `origin` remote is identified by the remote, so
  every clone, worktree and re-clone shares what was learned, whatever its folder is called; one
  without a remote by its main checkout's path (its worktrees share it); a directory outside any
  checkout by itself. A `.git` counts only as git counts it -- a directory holding `HEAD`, or a
  `gitdir:` file naming one that exists -- and the walk up from a session's directory stops at
  the home directory, so a versioned (or stray, empty) `~/.git` is the repository of sessions
  started in `~` and of nothing below it. `GIT_CEILING_DIRECTORIES` is honoured as git does.
* **One repository per session.** A hook's `cwd` is where the agent is now (Claude Code's
  follows its `cd`), so the first hook of a session pins it (`sessions/<host>/<id>.json`) and
  every later hook, and the fold it hands off, uses the repository it started in -- where the
  agent files its own session log. `te status` run inside a session says so when the two differ.
* **Deny lists stay per checkout.** The clones of a repository share its evidence and proposal,
  but each has its own settings file, so `untool_applied.json` records what was added per file.
* **State from earlier keys is carried over.** Up to 0.1.0 a checkout's folder name was part of
  the key, any `.git` entry made a checkout, and a session was recorded wherever the agent was.
  The first time a checkout is seen (`index.json`) the state under its earlier keys is sent
  where it belongs, and once after an upgrade a background `te repos migrate` does the same for
  every directory: each recorded session goes to the repository the first directory in its log
  resolves to now, sessions whose directory is gone stay put, and a directory none of whose
  sessions resolves to it any more is merged whole -- its learned state too, if it learned from
  more sessions than the destination. Learned state is not additive, so the other side's
  sessions are folded again from their logs where those still exist; a session folded under two
  keys counts once, at its later fold. Nothing is deleted: a merged directory moves to
  `merged/`, displaced learned state to `premerge-*`. `te repos merge KEY [INTO]` joins two by
  hand, for state whose checkouts are gone. Like the agents'
own session logs it is derived from, it is private to you: the plugin creates it `0700` and its
files `0600`, and tightens a looser one it finds.

Nothing leaves the machine unless `paid` is on. Then the proposer, judge and nudge writer send
**digests** — command templates, edited file basenames, error signatures, never file contents —
through your own agent CLI (`claude -p` / `codex exec` / `copilot -p` / `pi -p`), billed where
your session is.

The deny-list writer adds and removes only deny rules it recorded adding, never one you wrote, and
adds a `settings.local.json` it creates to `.git/info/exclude`. A repository decides what its
`.claude`, `.codex` and `.github` paths are, so the writer refuses one that is, or goes through,
a symlink leading out of the repository, and refuses to edit a settings file that does not
parse rather than write over it.

A learned fact is text injected into your agent's context, derived from your sessions' command
output — which a repository controls. The free layer only fills fixed templates with observed
command forms and hosts, and lints out completion advice; the paid proposer's facts are
open-vocabulary. Read them with `te microskills` before enabling the proposer on a repository you do
not trust.

## 5. Known limitations

* **Copilot CLI** — the real-model checks used one model (Claude Haiku 4.5, over BYOK). A
  per-tool deny list waits on Copilot gaining a persistent per-tool filter.
* **The loop guard, live** — the stop rung has not run on a real spiral, and whether a
  nudge changes a looping model's course is not established. The guard counts edits and
  repeated failures, not understanding, so with a small learned envelope it can fire on a turn
  that has already found its cause and is working around it.
* **Pi** — a live nudge and stop (steer message, `ctx.abort()`); tool pruning is not planned there,
  since Pi declares only four tools by default.
* **Known gaps:**
  * A turn that ran no recognised verification counts as ended green, so a failed turn without
    one can enter the envelope and loosen the guard for every later turn.
  * The free fact layer has no family for "this tool is absent here, with no working
    alternative" (a missing `ruff`, `mypy` or `pre-commit`, say).
  * Test runs launched from a script are not recognised as verification.
  * `refresh-catalog` is manual; until it runs in a repository, evidence pruning sees only
    deferred tools. Running it at a repository's first session would close that gap.
* Windows (the launcher is POSIX `sh`), Python 3.9, and compressed Codex rollouts (`.zst`).
* Transcript formats are internal to each agent and change between versions; the readers are
  pinned by tests against the shapes observed on the versions above.
