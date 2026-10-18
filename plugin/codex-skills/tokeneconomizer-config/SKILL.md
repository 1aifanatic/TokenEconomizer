---
name: "tokeneconomizer-config"
description: >
  Show or change TokenEconomizer's settings: turn micro-skills, tool pruning or the loop guard on or off, or tune them, for this repository or (with --global) every repository. Use when the user asks to configure, enable or disable TokenEconomizer or one of its mechanisms.
---

# tokeneconomizer-config

Run this command in the repository's root directory:

```
"${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te" --host codex --cwd "$(pwd)" config ARGS
```

Use `show` as ARGS unless the user asked to change a setting; then `set KEY=VALUE ...` or `unset KEY ...`, adding `--global` only if they asked for every repository. Show the output verbatim in a code block; if a setting was rejected, say why in one sentence.

The command reads and writes only TokenEconomizer's own state (under `~/.tokeneconomizer`)
and, for `toolprune apply` or `restore`, the project's `.codex/config.toml`; it makes no model
call. If it reports that the launcher does not exist, the plugin has not run in a session yet:
ask the user to start a new Codex session in this repository first.
