---
description: Earlier name of /tokeneconomizer:toolprune - review or apply the proposed deny list
argument-hint: "[show | apply [TOOL ...] | restore [TOOL ...] | refresh-catalog | measure]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it shows the proposal when there are none).
Run this exact command with the Bash tool, then show me its output verbatim in a code block:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" toolprune $ARGUMENTS
```

It applies or restores the deny list only when I typed `apply` or `restore` in those arguments; never add them yourself. If the output is a proposal, end with one line telling me how to apply it (`/tokeneconomizer:toolprune apply`) and that it takes effect from the next session.
