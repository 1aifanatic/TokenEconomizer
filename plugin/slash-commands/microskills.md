---
description: Show the repository facts (micro-skills) TokenEconomizer learned and injects at session start
---
Run this exact command with the Bash tool, then show me its output verbatim in a code block, with no commentary:

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" microskills
```
