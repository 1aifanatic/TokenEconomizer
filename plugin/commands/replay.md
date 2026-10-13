---
description: Estimate what TokenEconomizer would do, by replaying it over this repository's past sessions (no LLM calls)
argument-hint: "[--limit N] [--commit]"
---
Run this exact command with the Bash tool. It reads past session transcripts and can take a few minutes on a long history, so allow it a timeout of up to 10 minutes. Arguments given: "$ARGUMENTS".

```
te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; "$te" --cwd "$(pwd)" replay $ARGUMENTS
```

Show me the output verbatim in a code block, then summarize in at most three sentences what each mechanism would have done. Do not run anything else.
