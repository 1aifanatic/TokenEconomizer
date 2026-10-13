---
description: Show what TokenEconomizer has learned for this repository and what it has done
---
Run this exact command with the Bash tool, then show me its output verbatim in a code block. Add nothing unless the output contains a warning, in which case explain the warning in one sentence.

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" status
```
