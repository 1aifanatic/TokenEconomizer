#!/usr/bin/env bash
# THE WHOLE SWEEP: every lane of the main experiment, end to end, for real.
#
# main_sweep.sh runs ONE lane -- one (agent, model, domain) -- and with ARMS unset it
# already runs every arm of that lane. This driver is the layer above it: it crosses the
# agents, the models and the domains and runs the lanes in a defined order, so the whole
# matrix is one command instead of eight.
#
#   default matrix = 2 agents x 2 models x 2 domains = 8 lanes
#                    agents : claude (Claude Code)  + copilot (Copilot CLI)
#                    models : claude-opus-4-8       + claude-sonnet-5
#                    domains: swebench              + webbench
#   BOTH models run on BOTH harnesses -- that is the main sweep, not a choice between
#   them. Each (agent, model, domain) gets its own results tree, because the model is part
#   of the cost basis and two of them must never be averaged into one cell.
#
#   THESE ARE 8 OF THE 12 REQUIRED LANES. claude-opus-5 runs 4 arms per domain on both
#   agents (baseline / baseline-medium / baseline-low / trimech, or trimech-web) and is
#   NOT optional -- see MODELS below and docs/RUNNING.md (the frontier lane). This script does not launch
#   them, so a green run here is not a finished experiment.
#                  `sweep_plan.py --format matrix` prints the exact cell and task-run counts.
#
# RUN IT WITH DRYRUN=1 FIRST. A total dollar figure and a total wall-clock are deliberately
# not quoted, because both would be one repo-dominated per-task mean multiplied by the whole
# matrix. Budget from the PER-ARM runtime instead: time one arm on your host and multiply --
# it is nearly the same for every arm in a domain.
#
# Usage:
#   DRYRUN=1 bash scripts/launch/full_sweep.sh          # the plan and the cost, run nothing
#   bash scripts/launch/full_sweep.sh                   # the whole matrix, for real
#
#   # one agent, one model, one domain -- still every arm and every seed of that lane
#   AGENTS=copilot MODELS=claude-opus-4-8 DOMAINS=swebench \
#     bash scripts/launch/full_sweep.sh
#
#   # seed 0 only: a complete, publishable slice at 1/5 the bill
#   SEEDS=0 bash scripts/launch/full_sweep.sh
#
# Env:
#   AGENTS    space-separated            (default "claude copilot")
#   MODELS    space-separated            (default "claude-opus-4-8 claude-sonnet-5")
#             One spelling here for both agents; main_sweep.sh spells each lane's id the
#             way its backend serves it (the Copilot CLI's catalog says claude-opus-4.8,
#             the Anthropic API claude-opus-4-8) and both land in the same `_opus48` tree.
#   DOMAINS   space-separated            (default "swebench webbench")
#   SEEDS     space-separated            (default "0 1 2 3 4")
#   ARMS      restrict the arms          (default: every arm the domain defines)
#   REPOS     restrict the repo groups   (default: every group the domain defines)
#             NOTE: repo groups are domain-specific -- SWE-bench takes slugs
#             ("sympy/sympy"), Web-Bench takes project names ("zustand"). Set REPOS only
#             alongside a single DOMAIN, or one of the two lanes will plan zero cells.
#   LANES     serial | parallel          (default serial -- see the warning below)
#   DRYRUN    1 = print each lane's plan and cost, run nothing        (default 0)
#   Everything else (SKIP_DONE, LIMIT, ON_FAIL, TE_*) is passed through to main_sweep.sh.
#
# LANES=serial is the default ON PURPOSE. Every SWE-bench task starts a container, every
# Web-Bench stream starts dev servers and a Playwright browser, the Copilot lanes share ONE
# subscription quota and the Claude lanes ONE API key's rate limit -- so lanes contend for
# Docker, ports and rate limits long before they contend for CPU, and a throttled task
# shows up as a bad ROW (usage_incomplete), not as a launcher error.
# Raise it deliberately, one lane at a time, watching n_usage_incomplete.
#
# Resuming is free: SKIP_DONE=1 is main_sweep.sh's default, so re-running this command
# after an interruption fills only the cells that are missing.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/_env.sh"
REPO_ROOT="$(cd "$HERE/../.." && pwd)" te_load_env

AGENTS="${AGENTS:-claude copilot}"
# The main sweep's two models -- sweep_plan.SWEEP_MODELS is the same pair, and
# tests/test_sweep_arms.py holds them together.
#
# claude-opus-5 is deliberately absent, which does NOT mean optional: its 4 lanes are
# required (baseline / baseline-medium / baseline-low / trimech, both domains, both
# agents -- docs/RUNNING.md) and are launched by their own main_sweep.sh calls. So this
# script covers 8 of the 12 required lanes; a green run here is not a finished
# experiment. Adding the id here would plan a third FULL 21-arm lane instead.
MODELS="${MODELS:-claude-opus-4-8 claude-sonnet-5}"
DOMAINS="${DOMAINS:-swebench webbench}"
SEEDS="${SEEDS:-0 1 2 3 4}"
LANES="${LANES:-serial}"
DRYRUN="${DRYRUN:-0}"

runner_for() {
  case "$1" in
    swebench) echo "te_harness.swebench:make_runner" ;;
    webbench) echo "te_harness.webbench:make_runner" ;;
    *) echo "!! unknown domain '$1'" >&2; exit 2 ;;
  esac
}

# The whole bill, once, before anything runs -- and before any credential is needed.
echo "[full_sweep] agents='$AGENTS' models='$MODELS' domains='$DOMAINS' seeds='$SEEDS'"
echo "[full_sweep] lanes=$LANES dryrun=$DRYRUN"

# `sweep_plan --format matrix` always costs the FULL cross of its model list, so printing it
# under a narrowed scope would quote tens of thousands of dollars for a twelve-cell run.
# Show it only when nothing is narrowed; otherwise let each lane print its own estimate,
# which main_sweep.sh already does WITH --limit applied.
NARROWED=""
[[ "$DOMAINS" == "swebench webbench" ]]                 || NARROWED="domains"
[[ "$SEEDS"   == "0 1 2 3 4" ]]                         || NARROWED="$NARROWED seeds"
[[ "$AGENTS"  == "claude copilot" ]]                    || NARROWED="$NARROWED agents"
[[ -z "${ARMS:-}"  ]]                                   || NARROWED="$NARROWED arms"
[[ -z "${REPOS:-}" ]]                                   || NARROWED="$NARROWED repos"
[[ -z "${LIMIT:-}" || "${LIMIT:-0}" == "0" ]]           || NARROWED="$NARROWED limit"
if [[ -z "$NARROWED" ]]; then
  TE_SWEEP_MODELS="$MODELS" "${PYTHON:-python3}" "$HERE/../sweep_plan.py" --format matrix \
    2>/dev/null | sed 's/^/[full_sweep] /' || true
else
  echo "[full_sweep] scope narrowed ($NARROWED) -- the full-matrix estimate does not apply;"
  echo "[full_sweep] each lane prints its own costed plan below."
fi
echo

LANE_N=0 FAILED=()
for domain in $DOMAINS; do
  for model in $MODELS; do
    for agent in $AGENTS; do
      LANE_N=$((LANE_N + 1))
      tag="$agent/$model/$domain"
      echo "===== [full_sweep] lane $LANE_N: $tag ====="
      # DETACH=0: this driver owns the ordering, so each lane must run to completion in the
      # foreground. Detaching every lane would launch all eight at once and silently turn
      # LANES=serial into LANES=parallel.
      if AGENT="$agent" MODEL="$model" DOMAIN="$domain" SEEDS="$SEEDS" \
         RUNNER="$(runner_for "$domain")" DETACH=0 DRYRUN="$DRYRUN" \
         bash "$HERE/main_sweep.sh"; then
        echo "[full_sweep] lane $LANE_N OK: $tag"
      else
        rc=$?
        echo "[full_sweep] !! lane $LANE_N FAILED (rc=$rc): $tag" >&2
        FAILED+=("$tag")
        # A launch-time refusal (exit 2: dead quota, unserved model, missing key) applies to
        # every remaining lane on that credential, so stop rather than repeat it eight times.
        if [[ $rc -eq 2 ]]; then
          echo "[full_sweep] !! exit 2 is a launch refusal, not a task failure -- stopping." >&2
          echo "[full_sweep] !! fix the cause and re-run; SKIP_DONE=1 resumes what finished." >&2
          break 3
        fi
      fi
    done
  done
done

echo
if ((${#FAILED[@]})); then
  echo "[full_sweep] DONE with ${#FAILED[@]} failed lane(s):"
  printf '  %s\n' "${FAILED[@]}"
  exit 1
fi
echo "[full_sweep] DONE: $LANE_N lane(s), no failures."
