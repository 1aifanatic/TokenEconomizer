# Shell half of the .env loader (sourced by the launchers; see scripts/_env.py for the
# Python half — the two agree on the same file format).
#
#   . "$HERE/_env.sh"        # defines te_load_env / te_require_key / te_check_routing
#
# Format: plain `KEY=value`, optional leading `export`, `#` comments, quote values with
# spaces. Shell-sourced, so keep the file free of command substitution.
# Precedence: variables ALREADY exported in your shell win over the file.

# Load $TOKENECONOMIZER_ENV_FILE (default: experiments/.env, else the repository root's .env)
# without clobbering existing exports.
te_env_file() {
  if [[ -n "${TOKENECONOMIZER_ENV_FILE:-}" ]]; then echo "$TOKENECONOMIZER_ENV_FILE"
  elif [[ -f "${REPO_ROOT:-.}/.env" || ! -f "${REPO_ROOT:-.}/../.env" ]]; then echo "${REPO_ROOT:-.}/.env"
  else echo "${REPO_ROOT:-.}/../.env"; fi
}
te_load_env() {
  local file; file="$(te_env_file)"
  [[ -f "$file" ]] || return 0
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"                 # ltrim
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    [[ "$line" == export\ * ]] && line="${line#export }"
    key="${line%%=*}"; val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"                    # rtrim key
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    [[ "$val" == \"*\" || "$val" == \'*\' ]] && val="${val:1:${#val}-2}"
    [[ -z "$key" || -z "$val" ]] && continue                # KEY= means "not set here"
    [[ -n "${!key:-}" ]] && continue                        # shell export wins
    export "$key=$val"
  done < "$file"
}

# te_dryrun_skips_billed <what> — true when this is a DRYRUN, so a BILLED probe must not run.
#
# A dry run is how a lane is costed before spending anything, so a probe that BILLS during
# one contradicts the whole point: on a Copilot lane the auth and model probes are up to
# three premium requests and ~240 s.
te_dryrun_skips_billed() {
  [[ "${DRYRUN:-0}" == "1" ]] || return 1
  echo "[env] DRYRUN: skipping the billed $1 probe" >&2
  return 0
}

# te_require_key <VAR> [...] — fail fast before burning Docker time on a missing credential.
te_require_key() {
  local missing=() k
  for k in "$@"; do [[ -n "${!k:-}" ]] || missing+=("$k"); done
  if (( ${#missing[@]} )); then
    echo "!! missing credential(s): ${missing[*]}" >&2
    echo "   set them in $(te_env_file) (copy .env.example)" >&2
    exit 2
  fi
}

# te_detach <tag> <this-script> [args...] — re-exec the launcher DETACHED and return the
# terminal, unless DETACH=0 or we are already the detached copy.
#
# A real sweep is hours of Docker + paid agent time; losing it to a closed laptop or a
# dropped ssh session is the expensive failure. `setsid` puts the run in its own session,
# so it survives SIGHUP, and the whole tree (launcher -> driver -> docker exec -> agent)
# shares one process group that a single `kill -TERM -<pid>` stops cleanly.
#
# Call it AFTER the config + credential preflight, so a bad key or missing task file still
# fails in the user's terminal instead of silently in a log file.
te_detach() {
  local tag="$1"; shift
  [[ "${DETACH:-1}" == "0" || -n "${TE_DETACHED:-}" ]] && return 0
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local log="${TE_LOG:-${RESULTS_ROOT:-$PWD}/logs/${tag}__${stamp}.log}"
  mkdir -p "$(dirname "$log")" || { echo "!! cannot create log dir for $log" >&2; exit 2; }
  local pidfile="${log%.log}.pid"
  export TE_DETACHED=1
  export PYTHONUNBUFFERED=1          # else the driver's progress sits in a pipe buffer
  # The inner shell records its OWN pid (the session leader) -- $! here would be setsid's,
  # which is not the group the run ends up in.
  setsid nohup bash -c 'echo $$ > "$1"; shift; exec bash "$@"' _ "$pidfile" "$@" \
    >"$log" 2>&1 &
  disown 2>/dev/null || true
  echo "[detach] $tag started in the background (DETACH=0 to run in the foreground)"
  echo "[detach] log:    $log"
  echo "[detach] follow: tail -f $log"
  echo "[detach] stop:   kill -TERM -\$(cat $pidfile)     # leading '-' = whole group"
  exit 0
}

# te_tree_name <agent> <domain> [model-tag] — the results tree for one (agent, domain, model).
#
# `<agent>_<domain>`, plus `_<tag>` when a model tag is given: claude_swebench_opus48,
# copilot_webbench_sonnet5, codex_swebench_gpt56sol. The MODEL is in the name because it
# is part of the cost basis: the aggregator averages a cell, and two models in one cell
# average two bases into a number that describes neither. Omitted (2-arg call) the name is
# `<agent>_<domain>`.
te_tree_name() {
  local agent="$1" domain="$2" tag="${3:-}" name
  name="${agent}_${domain}"
  [[ -n "$tag" ]] && name="${name}_${tag}"
  echo "$name"
}

# te_model_tag <model-id> — the short, filesystem-safe tag te_tree_name appends.
#
# `claude-opus-4-8` and `claude-opus-4.8` are the same model spelled two ways (the CLI takes
# both; the stored rows carry both), so the tag normalizes separators away and they cannot
# produce two trees for one model. A router directive is NOT a model: `auto`/`agent` means
# "whatever the CLI picks per request", which is not a cost basis worth naming, so it maps
# to the empty tag and the tree keeps its 2-arg name.
te_model_tag() {
  local m
  m="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$m" in
    ""|agent|auto|copilot) echo "" ; return 0 ;;
  esac
  m="${m#claude-}"                       # opus-4-8 / sonnet-4.6
  # `/` GOES TOO, and not merely for tidiness: a provider-prefixed id (`vendor/model`, as a
  # Copilot BYOK provider may spell one) would otherwise turn the tree name into a nested
  # PATH, which splits one cell across two directories and hides it from the tree walk.
  printf '%s' "$m" | tr -d './-'         # opus48 / sonnet46 / gpt56sol
  echo
}

# te_model_alt_spelling <model-id> — the same id with its version separator swapped, or
# nothing when the id has only one spelling.
#
#   claude-opus-4-8  -> claude-opus-4.8        claude-haiku-4.5 -> claude-haiku-4-5
#   claude-sonnet-5  -> (empty)
#
# Two catalogs serve the same model under two spellings: the Copilot CLI's own list uses
# a DOT (`claude-opus-4.8`, measured healthy on 63 stored subscription rows), while the
# Anthropic API uses a DASH (`claude-opus-4-8`) and 404s the dotted form. te_model_tag folds
# both to one tree, so switching spelling never forks a tree; only the upstream call cares.
te_model_alt_spelling() {
  local m="${1:-}" alt
  if printf '%s' "$m" | grep -q '[0-9]\.[0-9]'; then
    alt="$(printf '%s' "$m" | sed 's/\([0-9]\)\.\([0-9]\)/\1-\2/')"
  else
    alt="$(printf '%s' "$m" | sed 's/\([0-9]\)-\([0-9]\)/\1.\2/')"
  fi
  [[ "$alt" != "$m" ]] && printf '%s\n' "$alt"
  return 0
}

# te_model_for_agent <agent> <model-id> — the spelling the agent's backend serves.
#
# The docs and the launchers name every model ONCE, in the Anthropic API's dashed form
# (`claude-opus-4-8`). The one backend that spells it differently is the Copilot CLI's
# native catalog on a subscription (`claude-opus-4.8`), so a copilot lane gets the dotted
# form there -- and only there: under Copilot BYOK the CLI speaks the Anthropic wire API,
# which takes the dash. Any other agent is normalised TO the dash, so a dotted id typed from
# an older doc cannot 404 once per task. Ids with a single spelling (`claude-sonnet-5`)
# pass through untouched, as does anything that is not a versioned
# Claude id. te_check_copilot_model still VERIFIES the result against the CLI and switches
# back if a vendor rotates a catalog, so this is a default, not a bet.
te_model_for_agent() {
  local agent="${1:-}" m="${2:-}" alt out
  out="$m"
  # TE_STRICT_MODEL_ID: send the id exactly as typed -- no re-spelling here, and the probe
  # refuses rather than switches. For the operator who needs the wire id to be literal.
  [[ -n "${TE_STRICT_MODEL_ID:-}" ]] && { printf '%s\n' "$m"; return 0; }
  case "$m" in claude-*[0-9][.-][0-9]*) ;; *) printf '%s\n' "$m"; return 0 ;; esac
  alt="$(te_model_alt_spelling "$m")"
  case "$agent" in
    copilot)
      if [[ -z "${COPILOT_PROVIDER_BASE_URL:-}" ]]; then
        printf '%s' "$m" | grep -q '[0-9]\.[0-9]' || out="$alt"      # subscription: dot
      else
        printf '%s' "$m" | grep -q '[0-9]-[0-9]' || out="$alt"        # BYOK wire: dash
      fi ;;
    *)  printf '%s' "$m" | grep -q '[0-9]-[0-9]' || out="$alt" ;;     # Anthropic API
  esac
  [[ -n "$out" ]] || out="$m"
  if [[ "$out" != "$m" ]]; then
    echo "[env] model id: '$m' -> '$out' (the spelling this agent's backend serves; the" >&2
    echo "[env]           tree tag is $(te_model_tag "$m") either way)"                  >&2
  fi
  printf '%s\n' "$out"
}

# te_drop_anthropic_endpoint — ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN are not inputs.
#
# Claude Code bills the Anthropic API on ANTHROPIC_API_KEY, and nothing here selects another
# endpoint. Claude Code itself still honours both variables wherever it inherits them, so a
# value left in an older .env or the shell is removed from this launcher's environment --
# nothing it starts sees it -- and said once, rather than silently.
te_drop_anthropic_endpoint() {
  local set=() k
  for k in ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN; do
    [[ -n "${!k:-}" ]] && set+=("$k")
  done
  (( ${#set[@]} )) || return 0
  echo "[env] ${set[*]} set but IGNORED: Claude Code bills the Anthropic API on" >&2
  echo "[env]   ANTHROPIC_API_KEY. Unset for this run; remove from $(te_env_file)." >&2
  unset "${set[@]}"
}

# te_check_openai_base_url — OPENAI_BASE_URL may only name OpenAI's own API.
#
# It exists for a key issued against a regional endpoint (https://us.api.openai.com/v1),
# which 401s on the global one. Any other host would bill something that is not the OpenAI
# API under a tree that says it is, so it is refused at launch.
te_check_openai_base_url() {
  local url="${OPENAI_BASE_URL:-}" host
  [[ -n "$url" ]] || return 0
  host="${url#*://}"; host="${host%%/*}"; host="${host%%:*}"
  host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  [[ "$url" == https://* && ( "$host" == "api.openai.com" || "$host" == *.api.openai.com ) ]] \
    && return 0
  echo "!! OPENAI_BASE_URL=$url is not an OpenAI API endpoint." >&2
  echo "   It is only for a regional OpenAI key (https://<region>.api.openai.com/v1);" >&2
  echo "   unset it for a global key." >&2
  exit 2
}

# te_check_routing <agent> — say what this lane bills, and preflight the credential it needs.
#
#   claude   the Anthropic API on ANTHROPIC_API_KEY
#   codex    the OpenAI API on OPENAI_API_KEY (OPENAI_BASE_URL only for a regional key)
#   copilot  the GitHub Copilot subscription, or Copilot's own BYOK (COPILOT_PROVIDER_*)
te_check_routing() {
  case "$1" in
    claude)  te_drop_anthropic_endpoint ;;
    codex)   te_check_openai_base_url ;;
    copilot) ;;
    *) echo "!! unknown agent '$1' (claude | copilot | codex)" >&2; exit 2 ;;
  esac
  # The stub writes plausible rows with a FIXED resolve pattern and no agent behind them.
  # Its output is indistinguishable from a real sweep once it is sitting in results/, so say
  # so at launch rather than letting synthetic numbers be mistaken for a benchmark.
  if [[ "${RUNNER:-stub}" == "stub" ]]; then
    echo "[env] RUNNER=stub -- SYNTHETIC run: no agent, no grading, numbers are NOT a benchmark" >&2
    echo "[env]   set RUNNER=<module>:<factory> for a real run" >&2
    return 0
  fi
  case "$1" in
    copilot)
      # The Copilot CLI authenticates to GitHub, NOT to Anthropic: it reads a token from
      # COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN, or the credentials `copilot` itself
      # wrote under COPILOT_HOME. Demanding ANTHROPIC_API_KEY here would refuse to start on
      # a box perfectly able to run the lane (te_harness/copilot.py says the same about the
      # runner). te_check_copilot_auth prints the routing line and gives the better error:
      # it checks the binary, the token, and probes the CLI, exiting 2 with the remedy.
      te_check_copilot_auth ;;
    claude)
      te_require_key ANTHROPIC_API_KEY
      echo "[env] routing: Anthropic API (key ...${ANTHROPIC_API_KEY: -4})" ;;
    codex)
      te_require_key OPENAI_API_KEY
      echo "[env] routing: OpenAI API at ${OPENAI_BASE_URL:-https://api.openai.com/v1} (key ...${OPENAI_API_KEY: -4})" ;;
  esac
}

# te_copilot_refused <probe-output> — did the CLI refuse the probe outright?
#
# Both checks below look for one known error string, and its ABSENCE is not success: any
# other refusal would read as one. A spent subscription is that case, and it is not rare on
# a long sweep: the CLI prints "You have exceeded your monthly quota", the JSON stream
# carries {"errorType":"quota","statusCode":402,"errorCode":"quota_exceeded"}, and the auth
# probe still EXITS 0 -- on its own the launcher would print `token warm` and `model serves`
# for an account that cannot run a single task, and the lane would fill its cells with
# turns=1 / eff_tok=0 rows that nothing aborts. Keyed on the vendor's own error text and
# structured code, so it covers both the JSON and the plain-text probe shapes.
te_copilot_refused() {
  local out="$1"
  printf '%s' "$out" | grep -qiE 'quota_exceeded|exceeded your monthly quota|"errorType"[[:space:]]*:[[:space:]]*"quota"|"statusCode"[[:space:]]*:[[:space:]]*402'
}

te_copilot_refused_die() {
  local out="$1" where="$2"
  te_copilot_refused "$out" || return 0
  echo "!! the Copilot subscription REFUSED the $where probe: monthly quota exceeded (HTTP 402)." >&2
  echo "   Nothing aborts a lane on this, so launching now would fill cells with" >&2
  echo "   turns=1 / eff_tok=0 rows." >&2
  echo "   Wait for the quota to reset, or run the Copilot CLI on your own provider:" >&2
  echo "     COPILOT_PROVIDER_TYPE=anthropic COPILOT_PROVIDER_BASE_URL=https://api.anthropic.com \\" >&2
  echo "     COPILOT_PROVIDER_API_KEY=...   (see README.md)" >&2
  exit 2
}

# te_check_copilot_auth — the GitHub Copilot CLI is signed in and its token is warm.
#
# Copilot does NOT use ANTHROPIC_API_KEY; it authenticates against GitHub from ~/.copilot
# (or COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN). Probing for on-disk artifacts is not
# enough: a ~/.copilot/session-state directory survives a sign-out, so a stale box would
# pass and then fail every task with "No model backend available".
#
# TWO attempts, because one is not conclusive: the short-lived token is refreshed into
# ~/.copilot/config.json only when a copilot process RUNS on the host, so on a box that has
# been idle a while the FIRST invocation can report "No authentication information found"
# and warm the token as a side effect. The probe therefore doubles as the warm-up -- and
# for a long unattended sweep on the subscription, keep the token warm (or use BYOK).
te_check_copilot_auth() {
  local bin="${COPILOT_HOST_BIN:-$HOME/.local/bin/copilot}" out ok=0
  command -v "$bin" >/dev/null 2>&1 || [[ -x "$bin" ]] || bin="$(command -v copilot || true)"
  if [[ -z "$bin" ]]; then
    echo "!! copilot CLI not found." >&2
    echo "   install: curl -fsSL https://gh.io/copilot-install | bash" >&2
    echo "   or set COPILOT_HOST_BIN to its path" >&2
    exit 2
  fi
  # BYOK: COPILOT_PROVIDER_BASE_URL points the CLI at a custom provider, which it bills
  # INSTEAD of the subscription -- GitHub auth is then not required at all
  # (`copilot help providers`). The sweep's default is still the subscription; this branch
  # exists for a host without one. Say so plainly, because the cost basis differs and
  # run_config.json records the provider URL rather than "github-copilot".
  if [[ -n "${COPILOT_PROVIDER_BASE_URL:-}" ]]; then
    echo "[env] routing: Copilot CLI BYOK -> ${COPILOT_PROVIDER_TYPE:-openai} at $COPILOT_PROVIDER_BASE_URL (GitHub auth not used)"
    if [[ -z "${COPILOT_PROVIDER_API_KEY:-}${COPILOT_PROVIDER_BEARER_TOKEN:-}" ]]; then
      echo "!! BYOK is active but neither COPILOT_PROVIDER_API_KEY nor COPILOT_PROVIDER_BEARER_TOKEN is set." >&2
      echo "   a remote provider will refuse every task; unset COPILOT_PROVIDER_BASE_URL to go back to the subscription." >&2
    fi
    return 0
  fi
  if [[ -n "${COPILOT_GITHUB_TOKEN:-}${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]]; then
    echo "[env] routing: native Copilot (token from the environment)"
    return 0
  fi
  if te_dryrun_skips_billed "copilot auth"; then
    echo "[env] routing: native Copilot (on-disk GitHub auth, not probed on a dry run)"
    return 0
  fi
  local try rc
  for try in 1 2; do
    rc=0
    out="$(timeout 120 "$bin" -p "ok" --no-color --output-format json \
             --log-level none 2>&1)" || rc=$?
    if ! printf '%s' "$out" | grep -qi "No authentication information found"; then
      ok=1; break
    fi
    [[ "$try" == 1 ]] && echo "[env] cold copilot token; retrying once to let it refresh..." >&2
  done
  if (( ok != 1 )); then
    echo "!! the copilot CLI is not signed in on this host." >&2
    echo "   run 'copilot' then '/login' (OAuth device flow), or" >&2
    echo "   export COPILOT_GITHUB_TOKEN (or GH_TOKEN / GITHUB_TOKEN)." >&2
    exit 2
  fi
  te_copilot_refused_die "$out" "auth"
  te_copilot_probe_exit "$rc" "auth"
  echo "[env] routing: native Copilot (on-disk GitHub auth, token warm)"
}

# An unrecognized process failure (including timeout's 124) is not a successful probe.
te_copilot_probe_exit() {
  if (( $1 != 0 )); then
    echo "!! copilot $2 probe failed (exit $1); validation did not complete." >&2
    exit 2
  fi
}

# te_copilot_model_unserved <probe-output> — did the CLI say the model does not exist?
# Subscription: "Model 'x' is not available". BYOK: "Model 'x' not found on provider at
# <url> (HTTP 404)". Anything else is not this failure.
te_copilot_model_unserved() {
  printf '%s' "$1" | grep -qiE "is not available|not found on provider|\(HTTP 404\)"
}

# te_check_copilot_model <model> <effort> — the pinned model exists AND accepts this effort.
#
# Both mistakes fail PER TASK, not at launch: the CLI exits before its first turn, so a
# sweep would write a whole tree of 0-turn rows that look like the agent tried and failed.
# One probe up front turns that into a launch-time error.
#
# The two cases are not equally fatal, so they are not treated equally:
#   * model not served      -> hard fail; nothing can run.
#   * model rejects effort  -> WARN and export TE_EFFORT_APPLIES=0. The run is still valid,
#                              it just cannot control reasoning effort, and the rows record
#                              effort: null so nobody later assumes it was held fixed.
te_check_copilot_model() {
  local model="$1" effort="$2" bin out rc=0
  # 'auto' (the router) never takes an effort level; the runner already records null.
  [[ -z "$model" || "$model" == "auto" ]] && return 0
  te_dryrun_skips_billed "copilot model/effort" && return 0
  # Idempotent, but keyed to the MODEL it was decided for: te_detach re-execs this script
  # and the exports below survive into the detached copy, so without this the probe is paid
  # for twice per launch -- while a bare "already set" check would let a stale value from a
  # previous launch with a DIFFERENT model silently skip validation.
  [[ "${TE_EFFORT_APPLIES_FOR:-}" == "$model" && -n "${TE_EFFORT_APPLIES:-}" ]] && return 0
  bin="${COPILOT_HOST_BIN:-$HOME/.local/bin/copilot}"
  [[ -x "$bin" ]] || bin="$(command -v copilot || true)"
  [[ -n "$bin" ]] || return 0                # te_check_copilot_auth already reported this
  out="$(timeout 120 "$bin" -p "ok" --no-color --log-level none \
           --model "$model" ${effort:+--effort "$effort"} 2>&1)" || rc=$?
  # Before the model verdict: a refused probe says nothing about the model, and reading it
  # as "serves" would put a spent quota on the run banner as all-green.
  te_copilot_refused_die "$out" "model"
  # Two "not served" shapes, one per billing path. The subscription says "is not
  # available"; a BYOK provider says "Model 'x' not found on provider at <url> (HTTP 404)".
  # Keying on the first alone would let a BYOK lane print `serves; effort accepted` for an
  # id the provider 404s, then write a tree of turns=1 / eff_tok=0 rows.
  if te_copilot_model_unserved "$out"; then
    # The same model under the other separator, once: the Copilot CLI's own catalog spells
    # the older Opus `claude-opus-4.8` while an Anthropic-wire BYOK provider takes
    # `claude-opus-4-8`. te_model_for_agent already picked the likely one; this is the
    # verified fallback for when a catalog moves. One more billed probe, only on failure.
    local alt out2 rc2=0
    alt="$(te_model_alt_spelling "$model")"
    if [[ -n "$alt" && -z "${TE_STRICT_MODEL_ID:-}" ]]; then
      case "$model" in claude-*) ;; *) alt="" ;; esac
    else
      alt=""
    fi
    if [[ -n "$alt" ]]; then
      out2="$(timeout 120 "$bin" -p "ok" --no-color --log-level none \
                --model "$alt" ${effort:+--effort "$effort"} 2>&1)" || rc2=$?
      te_copilot_refused_die "$out2" "model"
      if ! te_copilot_model_unserved "$out2"; then
        if ! printf '%s' "$out2" | grep -qi "does not support reasoning effort"; then
          te_copilot_probe_exit "$rc2" "alternate model"
        fi
        echo "[env] !! copilot does not serve '$model' but DOES serve '$alt' -- the same model," >&2
        echo "[env] !! other separator. Using '$alt' for this lane (tree tag $(te_model_tag "$model")" >&2
        echo "[env] !! either way; TE_STRICT_MODEL_ID=1 to refuse instead)."                    >&2
        MODEL="$alt"; export MODEL
        model="$alt"; out="$out2"; rc="$rc2"
      fi
    fi
  fi
  if te_copilot_model_unserved "$out"; then
    if [[ -n "${COPILOT_PROVIDER_BASE_URL:-}" ]]; then
      echo "!! copilot model '$model' is not served by the BYOK provider at $COPILOT_PROVIDER_BASE_URL." >&2
      echo "   $(printf '%s' "$out" | grep -i 'not found on provider' | head -1)" >&2
      echo "   Under COPILOT_PROVIDER_TYPE=anthropic the provider takes Anthropic's own ids" >&2
      echo "   (claude-sonnet-5, claude-opus-4-8); pin one it serves." >&2
    else
      echo "!! copilot model '$model' is not available on this subscription." >&2
      echo "   pick one it serves, or set COPILOT_MODEL=auto (which cannot apply an" >&2
      echo "   effort level)." >&2
    fi
    exit 2
  fi
  if printf '%s' "$out" | grep -qi "does not support reasoning effort"; then
    echo "[env] WARNING: model '$model' does not accept --effort $effort." >&2
    echo "[env]   running WITHOUT an effort level; rows record effort: null, so these" >&2
    echo "[env]   numbers are NOT comparable to effort-pinned runs." >&2
    export TE_EFFORT_APPLIES=0 TE_EFFORT_APPLIES_FOR="$model"
    return 0
  fi
  te_copilot_probe_exit "$rc" "model"
  export TE_EFFORT_APPLIES=1 TE_EFFORT_APPLIES_FOR="$model"
  echo "[env] copilot model '$model' serves; effort=${effort:-<unset>} accepted"

}
