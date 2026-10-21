---
description: Show or change TokenEconomizer's settings - turn each mechanism on or off, or tune it
argument-hint: "[show | set KEY=VALUE ... [--global] | unset KEY ... [--global]]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it shows every setting when there are none).
Run this exact command with the Bash tool, then show me its output verbatim in a code block:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" config $ARGUMENTS
```

It changes a setting only when I typed `set` or `unset` in those arguments; never add them yourself. If a setting was rejected, say why in one sentence.
