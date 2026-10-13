---
description: Show or change TokenEconomizer's settings - turn each mechanism on or off, or tune it
argument-hint: "[show | set KEY=VALUE ... [--global] | unset KEY ... [--global]]"
---
Run this exact command with the Bash tool (use `show` when no arguments were given: "$ARGUMENTS"), then show me its output verbatim in a code block:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" config $ARGUMENTS
```

Do not run `set` or `unset` unless those words appear in the arguments above. If a setting was rejected, say why in one sentence.
