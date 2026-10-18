# Plugin runtime

What the plugin's hooks and the `te` command run. The agent launches itself; this package runs
only when the agent calls a hook, each call a fresh process, so what is learned lives on disk and
every handler returns fast and fails open.

| Hook | What happens (`controller.py`) |
|---|---|
| SessionStart | inject the repository's micro-skills; mention a pending deny-list proposal; fold any earlier session whose end was missed |
| UserPromptSubmit | open a prompt-turn for the loop guard |
| PostToolUse / PostToolUseFailure | fold the call into the turn's guard; redirect or stop when it says so |
| Stop | close the turn; a turn that ended green feeds the envelope |
| SessionEnd | fold the session in a detached `te fold` |

| Module | |
|---|---|
| `hook.py`, `cli.py` | entry points: the hook dispatcher and `te` |
| `controller.py` | what happens at each hook |
| `hosts.py` | per-agent hook payloads and outputs (Claude Code, Codex, Copilot CLI, Pi) |
| `fold.py` | a finished session through `PrefixStage` and `ToolingStage` |
| `denylist.py` | writing an approved proposal into each agent's settings, and taking it back |
| `toolcheck.py` | asking the agent CLIs what a session loads, and what a deny list removed |
| `config.py` | settings, their sources, and the earlier names they are still read under |
| `context.py`, `paths.py`, `store.py` | where state lives, which repository a session is in, locked atomic JSON |
| `adapter.py` | the `DomainAdapter` for a real repository |
| `accounting.py` | the session ledger |
| `sidecall.py`, `judge.py` | opt-in paid calls through the agent's own CLI, off the hook's critical path |
| `repomap.py`, `repomap_gen.py` | the opt-in repository map |
| `probe.py` | `te --host copilot probe`: the plugin end to end in real Copilot sessions |

The installable plugin directory -- manifests, hook files and launchers -- is `plugin/` at the
repository root; see `plugin/README.md` there and `docs/PLUGIN.md`.
