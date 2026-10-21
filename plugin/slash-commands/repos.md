---
description: List every repository TokenEconomizer keeps state for, or merge one repository's state into another
argument-hint: "[list | migrate | merge KEY [INTO]]"
---
The arguments I typed after the command are: "$ARGUMENTS" (it lists the repositories when there are none).
Run this exact command with the Bash tool, then show me its output verbatim in a code block:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" repos $ARGUMENTS
```

It moves state only when I typed `migrate` or `merge` in those arguments; never add them yourself. If the list shows state recorded by an earlier version, say in one sentence that it is sorted out at the next session start, or now with `/tokeneconomizer:repos migrate`.
