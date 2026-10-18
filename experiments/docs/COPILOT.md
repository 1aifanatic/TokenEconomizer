# Running the GitHub Copilot CLI as the sealed agent

TokenEconomizer's mechanisms are agent-agnostic, but each sealed agent needs its own runner
below the [`AgentRunner`](../../tokeneconomizer/runner.py) seam. This page is the Copilot path,
end to end, for **both** SWE-bench and Web-Bench. It assumes you have already done
[`SETUP.md`](SETUP.md) steps 1–3 (venv, the two outside-the-repo directories, datasets);
everything Copilot-specific is here.

Read [`SETUP.md`](SETUP.md) first if you have not. The gates below are cumulative — do not
skip one because the next looks more interesting.

---

## What is different about Copilot

Four things, and each one has bitten this harness at least once:

| | Claude Code | Copilot CLI |
|---|---|---|
| **Auth** | `ANTHROPIC_API_KEY` | GitHub, from on-disk `~/.copilot` or `COPILOT_GITHUB_TOKEN`; or Copilot's own BYOK |
| **Token accounting** | in the stream's terminal `result` event | **only** in the CLI's debug log |
| **Turn cap** | `--max-turns N` | none — wall clock and the spiral guard are the only bounds |
| **Tool exclusion** | one comma-joined `--disallowed-tools` | `--excluded-tools` repeated per tool |

The accounting difference is the dangerous one. Copilot's `result` event carries
`premiumRequests` and durations but **no token fields at all**, so a runner that reads usage
the Claude way records a row of zeros — every cost number downstream is then wrong rather
than missing. That is why every Copilot invocation here runs with `--log-level all
--log-dir …` and mines the log
([`te_harness/copilot.py`](../te_harness/copilot.py)).

---

## Step 1 — install and sign in

```bash
curl -fsSL https://gh.io/copilot-install | bash     # -> ~/.local/bin/copilot
copilot                                              # then: /login
```

`/login` runs an OAuth device flow and stores the token in your system credential store, or
in plain text under `~/.copilot/` if there is no credential store. For a headless box,
export a token instead — the CLI checks `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then
`GITHUB_TOKEN`. Fine-grained (v2) PATs need the **Copilot Requests** permission; classic
`ghp_` tokens are not supported.

**Gate:**

```bash
copilot -p "say ok" --allow-all-tools --no-color --log-level none
```

Expect `ok`. If you get `No authentication information found` on a box that has been idle,
**run it a second time** — see the token-staleness note in Step 5; the first call refreshes
the token as a side effect.

### BYOK — Copilot's own provider keys

Without a subscription, or with its quota spent, the CLI can bill **your own provider**
instead (`copilot help providers`). `COPILOT_PROVIDER_BASE_URL` is the one switch — set it
and GitHub auth is not used at all:

```bash
COPILOT_PROVIDER_TYPE=anthropic
COPILOT_PROVIDER_BASE_URL=https://api.anthropic.com
COPILOT_PROVIDER_API_KEY=<your Anthropic key>     # the CLI never reads ANTHROPIC_API_KEY
```

The launcher then prints `routing: Copilot CLI BYOK -> anthropic at …` and skips the GitHub
auth probe. With the base URL set and neither `COPILOT_PROVIDER_API_KEY` nor
`COPILOT_PROVIDER_BEARER_TOKEN`, it warns, and every task would fail — read the banner.

* **It is still the Copilot CLI.** BYOK keeps the agent `copilot`, its own trajectory shape,
  tool names and `copilot_<domain>_<tag>` trees; it is not Claude Code on the same key.
* **`--effort` is accepted under BYOK**, so the effort arms run.
* **`--model` keeps the first-party id** on the Anthropic wire (`claude-opus-4-8`, dashed).
  `COPILOT_PROVIDER_MODEL_ID` / `COPILOT_PROVIDER_WIRE_MODEL` are for a provider whose served
  id differs from the one you want recorded (a router that prefixes `anthropic/`, for
  example); leave them unset otherwise.
* **A BYOK row and a subscription row are different cost bases, and the tree name does not
  separate them.** The discriminator is `run_config.json` → `routing.base_url`: the provider
  URL under BYOK, the literal `github-copilot` on a subscription. Never pool the two; if you
  switch mid-sweep, move the finished cells to their own tree first.

The harness forwards the provider variables into the SWE-bench task container and passes
the credential by name only, so it never reaches argv; Web-Bench's agent runs on the host and
inherits them directly.

## Step 2 — pick a model, and know what that costs you

Copilot serves its own catalog, which depends on your subscription. An Anthropic model id is
not a valid value here. Check what yours serves before a sweep — a bad id fails **per task**,
not at launch:

```bash
copilot -p "say ok" --model <the-id-you-want> --effort high --no-color --log-level none
```

This is the same probe the launcher runs, so its verdict is the one you will get:

* `ok` — the model is served **and** takes your effort level. Pin it.
* `Model "<id>" … is not available.` — pick another id.
* `Model "<id>" does not support reasoning effort configuration` — the model is served but
  has no effort knob. Usable, but effort will not be applied (see below).

If nothing is available, use `--model auto` and let the CLI route.

### Reasoning effort

**If you want `TE_EFFORT_LEVEL=high` to actually take effect, you must pin a model.** The
`auto` router rejects the flag outright — `--model auto --effort high` exits with *"Model
auto does not support reasoning effort configuration"* and the task never runs. Some pinned
models refuse it too.

The launcher settles this for you with one probe before the sweep starts
(`te_check_copilot_model`), so neither mistake can cost you a run:

| Probe result | What happens |
|---|---|
| model served, effort accepted | `[env] copilot model '<id>' serves; effort=high accepted` — runs with effort, row records it |
| model served, effort refused | **warns**, exports `TE_EFFORT_APPLIES=0`, runs without the flag, row records `effort: null` |
| model not served | **hard fail, exit 2** — nothing could have run |

**A pinned model must also be priced.** `pricing.MODEL_WEIGHTS` refuses to guess ratios for
an id it does not know — deliberately, so a new model can never be silently mispriced. The
runner now checks this at construction, so an unpriced id fails at startup:

```
ValueError: pricing: no weight entry for billed model 'my-model' (agent='copilot').
            Add it to pricing.MODEL_WEIGHTS
```

Add the id to `tokeneconomizer/pricing.py` before pinning it. Ids containing
`claude` fall back to the Claude curve automatically and need no entry. `auto` needs none
either — it is a routing directive, not a billed id, so it is recorded as `null` and priced
on the Claude curve, which is the historical behaviour for Copilot rows.

Set both in the repo's `.env`:

```bash
COPILOT_MODEL=gpt-5.6-sol   # a model id YOUR subscription serves; 'auto' = router, no effort
TE_EFFORT_LEVEL=high        # takes effect only when COPILOT_MODEL is a pinned id
```

> Where effort is not applied, the row's `effort` is `null` rather than `high`, and the
> reasoning effort is whatever the model or router defaulted to. Those rows stay internally
> comparable, but must **not** be compared against effort-pinned Claude numbers as though
> effort were held equal. Check `effort` in `run_config.json` before you compare.

If you bypass the launcher and call `scripts/run_experiment.py` directly, there is no probe —
but the runner still fails fast: a refused model or effort raises on the first segment rather
than writing a tree of 0-turn rows that look like genuine task failures.

## Step 3 — point the driver at the Copilot runner

The runner factories are agent-aware: the driver passes `--agent` to them, and an unknown
label raises rather than falling back to Claude. Nothing to configure beyond
`AGENT=copilot`.

```bash
# in .env
RUNNER=te_harness.swebench:make_runner      # or te_harness.webbench:make_runner
```

**Gate** — the label really does select the Copilot runner:

```bash
python3 - <<'EOF'
import sys; sys.path.insert(0, "scripts")
from run_experiment import load_runner
r = load_runner("te_harness.swebench:make_runner", "copilot")
print(type(r).__name__, r.agent_bin)
EOF
```

Expect `CopilotSwebenchRunner /…/bin/copilot`. If you see `ClaudeSwebenchRunner`, the label
is not reaching the factory and **every row would be Claude's work filed under Copilot's
name** — stop and fix that before spending anything.

## Step 4 — launch

Same launcher as Claude; only `AGENT` changes.

```bash
# SWE-bench
AGENT=copilot DOMAIN=swebench ARM=trimech REPOS="pydata/xarray" SEEDS="0" \
RUNNER=te_harness.swebench:make_runner \
  bash scripts/launch/trimech_ab.sh

# Web-Bench (short streams -> the tuned arm)
AGENT=copilot DOMAIN=webbench ARM=trimech-web REPOS="calculator" SEEDS="0" \
RUNNER=te_harness.webbench:make_runner \
  bash scripts/launch/trimech_ab.sh
```

The preflight prints which auth path it resolved and warms the token:

```
[env] routing: native Copilot (on-disk GitHub auth, token warm)
[trimech_ab] agent=copilot routing=direct domain=swebench arms=(baseline trimech) ...
```

Results land in `copilot_swebench/` and `copilot_webbench/`, separate from the Claude trees —
a different agent is a different cost basis and the two must never average into one cell.

## Step 5 — keep the token warm for a long sweep

Copilot's token is short-lived and is refreshed into `~/.copilot/config.json` **only when a
copilot process runs on the host**. SWE-bench containers authenticate from a read-only mount
of that file, so during a long unattended sweep the token goes stale (~75 min was the
observed lifetime) and every later container copies the dead token and fast-fails —
*"No model backend available"*, a 0-byte patch, ~30s wall. Hundreds of paid-looking failures
that are really one expired credential.

Run the keepalive for the duration of any sweep longer than about an hour:

```bash
/path/to/AgentEO/swebench/copilot_auth_keepalive.sh 600 &    # refresh every 10 min
# ... run the sweep ...
kill %1
```

Setting `COPILOT_GITHUB_TOKEN` sidesteps this entirely — the token is passed into the
container by environment and never goes stale mid-run. Prefer that for unattended work.

---

## Verifying a finished run

Three checks, in order of how quietly they fail:

```bash
RUN=$(ls -d <results>/copilot_swebench/baseline/*/2*__copilot__* | head -1)

# 1. the debug log exists -> token accounting and the untooling catalog both work
ls -la "$RUN/copilot_debug.log"

# 2. the row's tokens came from the log, not the stream fallback
python3 -c "import json;r=json.load(open('$RUN/row.json'));print(r['tokens_source'], r['input_tokens'], r['cache_read_input_tokens'])"

# 3. the tool catalog is non-empty (an empty one makes untooling a paid no-op)
python3 -c "
from tokeneconomizer import trace
c = trace.parse_catalog('$RUN', 'copilot')
print(len(c), 'tools;', sum(1 for v in c.values() if v), 'with descriptions')"
```

`tokens_source` must be `debug_log`. `stream_output_only` means the log was not recovered:
the row then has output tokens and nothing else, so its cost is a large underestimate rather
than a gap you would notice in a summary. `mixed` means one spiral segment was accounted and
another was not.

Check 3 should report ~17 tools. **`0 with descriptions` is expected on a current CLI** and
is not a failure — see the next section for what it costs you.

### Tool descriptions and which untooling flag to use

Older Copilot builds dumped the full tool **schemas** into the debug log, and
`reflect.parse_tool_catalog` read `{name: description}` straight out of it. Current builds
(verified on 1.0.82) log only the tool *calls*; the loaded tool **names** moved to the
stream's `session.usage_checkpoint` event. `trace.parse_catalog` tries the log first and
falls back to those names with empty descriptions, so you get names on every version.

That distinction decides which mechanism to run:

| Flag | Needs | On a current CLI |
|---|---|---|
| `--untool-evidence N` | names only (drop what was loaded but unused for N tasks) | **works** |
| `--untool-judge` | descriptions (reason about what each tool is *for*) | **degrades to a paid no-op** — it drops nothing and still bills for the call |

So on a current CLI prefer `--untool-evidence` alone, and treat a `trimech` run that
includes `--untool-judge` as paying for a judge that cannot contribute. Restoring the judge
means capturing a description snapshot the way `tokeneconomizer/trace/claude_tool_catalog.json` does for
Claude (see `te_harness/capture_catalog.py` for the pattern).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no copilot binary found` | Not installed, or installed somewhere else — set `COPILOT_HOST_BIN`. |
| `copilot host install incomplete: …/.cache/copilot` | The package cache is missing. Run `copilot` once interactively to populate it; docker would otherwise invent an empty dir and every task would fail. |
| Every task: 0 turns, empty patch, ~30s | Stale or missing auth in the container. See Step 5. |
| `Model "auto" does not support reasoning effort` | You pinned effort against the router. The launcher probe and the runner both guard this; if you see it raw, you built the argv yourself. |
| `copilot refused this configuration: …` (run aborts) | `COPILOT_MODEL` is not served, or refuses `--effort`. Fix it per Step 2 — the run stopped deliberately rather than logging hundreds of 0-turn failures. |
| `tokens_source: stream_output_only` on every row | `--log-level all --log-dir` was dropped, or the log dir was unreadable. |
| Untooling drops nothing but still bills | Expected for `--untool-judge` on a current CLI (no descriptions). If `--untool-evidence` also drops nothing, check the catalog is non-empty (check 3). |
| Rows say `copilot` but look like Claude | The runner factory ignored `--agent`. Re-run the Step-3 gate. |

## Known limitations

* **No turn cap.** `max_turns` is recorded in `run_config.json` and the spiral envelope is
  counted in turns, but nothing enforces a cap on the agent. `TE_TASK_TIMEOUT` and the
  spiral guard are the real bounds. A Copilot baseline is therefore not turn-capped the way
  a Claude baseline is.
* **No vendor USD.** Copilot reports a premium-request count, not a dollar figure, so
  `cost_usd_vendor` stays null. Cost comparisons run on effective tokens, which is the
  basis the study uses anyway. `aggregate.py` knows this: its `vendor_cost_missing` flag
  skips any agent in `AGENTS_WITHOUT_VENDOR_USD` (`copilot`, `codex`), so a Copilot tree
  reports zero of them rather than flagging every row.
* **Effort on `auto`** — see Step 2.
* **No tool descriptions** on current CLI builds, so the instruction-aware tool judge has
  nothing to reason over. Evidence untooling is unaffected.

## Verified

A `baseline` task on each domain has been run end to end on the subscription: each resolved
by that benchmark's own official grader (the SWE-bench harness and Web-Bench's Node
evaluator), with `tokens_source=debug_log` and the full cache-aware token breakdown
recovered from the debug log — a real cost basis rather than a row of zeros.

Check the same three things on your first row (see "Verifying a finished run" above) before
launching a lane.
