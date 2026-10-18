---
name: "tokeneconomizer-toolprune"
description: >
  Review, apply or undo TokenEconomizer's proposed list of Codex capabilities this repository never uses (written to the project's .codex/config.toml). Use when the user asks about TokenEconomizer's tool pruning or deny list, or asks to apply or restore it.
---

# tokeneconomizer-toolprune

Run this command in the repository's root directory:

```
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" toolprune ARGS
```

Use `show` as ARGS unless the user asked to `apply`, `restore` or `measure` (optionally naming capabilities). Never run `apply` or `restore` unless the user asked for it. Show the output verbatim in a code block; if it is a proposal, end with one line saying how to apply it and that it takes effect from the next session.

The command reads and writes only TokenEconomizer's own state (under `~/.tokeneconomizer`)
and, for `toolprune apply` or `restore`, the project's `.codex/config.toml`; it makes no model
call. If it reports that the launcher does not exist, the plugin has not run in a session yet:
ask the user to start a new Codex session in this repository first.
