---
description: Review or apply TokenEconomizer's proposed deny list of tools this repository never uses
argument-hint: "[show | apply [TOOL ...] | restore [TOOL ...] | refresh-catalog | measure]"
---
Run this exact command with the Bash tool (use `show` when no arguments were given: "$ARGUMENTS"), then show me its output verbatim in a code block:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" toolprune $ARGUMENTS
```

Do not run `apply` or `restore` unless those words appear in the arguments above. If the output is a proposal, end with one line telling me how to apply it (`/tokeneconomizer:toolprune apply`) and that it takes effect from the next session.
