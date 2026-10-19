#!/usr/bin/env bash
# The main-experiment sweep: every arm x repo x seed for ONE (agent, model, domain) tree.
#
# One invocation fills one results tree. The tree name carries the agent AND the model
# (claude_swebench_opus48, copilot_swebench_sonnet5, ...) because both are part of the cost
# basis: the aggregator averages a cell, and two models in one cell average two bases into a
# number that describes neither. Run the sweep as one invocation per (agent, model, domain)
# -- four agent-model pairs x two domains = 8 lanes.
#
# Arms (scripts/sweep_plan.py owns the list; scripts/run_experiment.py owns each flag set):
#   baseline           naive, harness-default effort (high)
#   baseline-medium    naive at TE_EFFORT_LEVEL=medium   (the arm sets it, not the caller)
#   baseline-low       naive at TE_EFFORT_LEVEL=low
#   inject-only        micro-skill synthesis + injection only
#   untool-only        evidence untooling + tool judge only
#   spiral-only        spiral guard -> judge -> nudge only   (Web-Bench: spiral-only-web)
#   trimech            all three co-activated               (Web-Bench: trimech-web)
#   rtk                naive + rtk 0.45.0                   (SWE-bench only)
#   serena             naive + Serena                       (SWE-bench only)
#   repomap            naive + aider's repo map              (SWE-bench only)
#   trimech-rtk        all three + rtk                       (SWE-bench only)
#   trimech-repomap    all three + the repo map              (SWE-bench only)
#
# The last five install an EXTERNAL TOOL (experiments/te_bench/baselines/) into the agent's
# container -- baselines, not mechanisms. They are SWE-bench only: Web-Bench reaches its
# agent through the evaluator's HTTP seam with no per-task repo container. Asking for one
# on DOMAIN=webbench fails in the plan rather than enumerating cells that cannot run.
#
# Billing: Claude Code on the Anthropic API (ANTHROPIC_API_KEY), the Copilot CLI on its
# GitHub subscription or Copilot's own BYOK (COPILOT_PROVIDER_*).
#
# Usage (one lane; README.md lists every lane of the paper):
#   AGENT=claude MODEL=claude-sonnet-5 DOMAIN=swebench \
#   RUNNER=te_harness.swebench:make_runner \
#     bash scripts/launch/main_sweep.sh
#
# Env:
#   AGENT      claude | copilot                          (default claude)
#   MODEL      model id passed to the CLI and recorded   (default claude-sonnet-5)
#              It also picks the tree suffix via te_model_tag, so it is REQUIRED to be a
#              pinned id: `auto`/`agent` gives no tag and no controlled cost basis.
#   DOMAIN     swebench | webbench                       (default swebench)
#   ARMS       space-separated arm list        (default: the domain's SWEEP_ARMS)
#   REPOS      space-separated groups          (default: every group in the workload record)
#   SEEDS      space-separated seeds           (default "0 1 2")
#   LIMIT      cap tasks per cell, 0 = all     (default 0)
#   RUNNER     'stub' or 'module:factory'      (default stub -- SYNTHETIC, not a benchmark)
#   SKIP_DONE  1 = skip a cell that already holds its full row count (default 1)
#   DRYRUN     1 = print the plan and the estimate, run nothing      (default 0)
#              The BILLED probes skip themselves, so a dry run never costs a request.
#   ON_FAIL    continue | abort                (default continue: one bad cell must not
#              take a 200-cell lane with it; the lane still exits nonzero and lists them)
#   DETACH     1 = background (default), 0 = foreground
#   TASKS_FILE task-spec JSON                (default: the domain's TOKENECONOMIZER_*_TASKS)
#   WORKLOAD   workload manifest, or 'none'    (default workloads/$DOMAIN.json)
#   TOKENECONOMIZER_RESULTS  results root     (default ./results)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"

# shellcheck source=./_env.sh
. "$HERE/_env.sh"
te_load_env

AGENT="${AGENT:-claude}"
MODEL="${MODEL:-claude-sonnet-5}"
# One spelling in the docs, the backend's spelling on the wire: the Copilot CLI's own
# catalog lists the older Opus as `claude-opus-4.8` while the Anthropic API takes
# `claude-opus-4-8`. te_model_for_agent picks the served form (and says so); the Copilot
# probe below still verifies it. The tree tag folds both, so nothing forks.
MODEL="$(te_model_for_agent "$AGENT" "$MODEL")"
DOMAIN="${DOMAIN:-swebench}"
SEEDS="${SEEDS:-0 1 2}"
LIMIT="${LIMIT:-0}"
RUNNER="${RUNNER:-stub}"
SKIP_DONE="${SKIP_DONE:-1}"
DRYRUN="${DRYRUN:-0}"
ON_FAIL="${ON_FAIL:-continue}"

case "$DOMAIN" in
  swebench) TASKS_FILE="${TASKS_FILE:-${TOKENECONOMIZER_SWEBENCH_TASKS:-}}" ;;
  webbench) TASKS_FILE="${TASKS_FILE:-${TOKENECONOMIZER_WEBBENCH_TASKS:-}}" ;;
  *) echo "!! main_sweep covers DOMAIN=swebench|webbench (got '$DOMAIN')" >&2; exit 2 ;;
esac
if [[ -z "$TASKS_FILE" && "$DRYRUN" != "1" ]]; then
  echo "!! no task-spec JSON: set TASKS_FILE or TOKENECONOMIZER_${DOMAIN^^}_TASKS" >&2
  exit 2
fi

# A pinned model id is not a nicety here: the tree suffix comes from it, and an unpinned
# router (`auto`) means the reasoning-effort arms cannot even pass --effort (Copilot's CLI
# rejects the flag on auto and exits 0-turn -- see te_harness/copilot.py:effort_applies).
MODEL_TAG="$(te_model_tag "$MODEL")"
if [[ -z "$MODEL_TAG" ]]; then
  echo "!! MODEL='$MODEL' is a router directive, not a pinned model: the tree would carry" >&2
  echo "!! no model tag and the effort arms could not pin effort. Pin an id (e.g."         >&2
  echo "!! MODEL=claude-sonnet-5 or MODEL=claude-opus-4-8)."                          >&2
  exit 2
fi

# Arms: the domain's default list unless overridden. Resolved through sweep_plan so the
# launcher and the plan can never disagree about what the sweep contains.
if [[ -n "${ARMS:-}" ]]; then
  ARM_ARGS=(--arms $ARMS)
else
  ARM_ARGS=()
fi
if [[ -n "${REPOS:-}" ]]; then
  REPO_ARGS=(--repos $REPOS)
else
  REPO_ARGS=()
fi

WORKLOAD="${WORKLOAD:-$REPO_ROOT/workloads/$DOMAIN.json}"
WORKLOAD_ARGS=()
if [[ "$WORKLOAD" != "none" ]]; then
  [[ -f "$WORKLOAD" ]] || { echo "!! workload manifest not found: $WORKLOAD" >&2; exit 2; }
  WORKLOAD_ARGS=(--workload "$WORKLOAD")
fi

RESULTS_ROOT="${TOKENECONOMIZER_RESULTS:-$REPO_ROOT/results}"
DEST="$RESULTS_ROOT/$(te_tree_name "$AGENT" "$DOMAIN" "$MODEL_TAG")"

# The plan: one `arm<TAB>repo<TAB>seed` line per cell, plus a costed summary. Computed
# before any credential check so `DRYRUN=1` works on a fresh clone with nothing configured.
#
# Captured into a variable first, NOT piped into `mapfile < <(...)`: a process substitution
# discards the producer's exit status, so an unknown arm or an unreadable workload would
# come back as an empty plan and this lane would report a clean zero-cell success.
if ! PLAN_RAW="$("$PY" "$HERE/../sweep_plan.py" --domain "$DOMAIN" "${WORKLOAD_ARGS[@]}" \
      "${ARM_ARGS[@]}" "${REPO_ARGS[@]}" --seeds $SEEDS --format cells)"; then
  echo "!! could not plan the sweep (see the error above)" >&2
  exit 2
fi
PLAN=()
[[ -n "$PLAN_RAW" ]] && mapfile -t PLAN <<<"$PLAN_RAW"
if ((${#PLAN[@]} == 0)); then
  echo "!! the plan is empty: no runnable cell for arms='${ARMS:-<default>}'" >&2
  echo "!! repos='${REPOS:-<all>}' seeds='$SEEDS' domain=$DOMAIN" >&2
  exit 2
fi

echo "[main_sweep] agent=$AGENT model=$MODEL tag=$MODEL_TAG domain=$DOMAIN"
echo "[main_sweep] dest=$DEST"
echo "[main_sweep] cells=${#PLAN[@]} seeds='$SEEDS' runner=$RUNNER skip_done=$SKIP_DONE"
# --limit must be forwarded or the estimate describes the FULL stream while the run is
# capped: a LIMIT=1 pilot printed "task-runs= 40 ... ~$14" for one task.
"$PY" "$HERE/../sweep_plan.py" --domain "$DOMAIN" "${WORKLOAD_ARGS[@]}" "${ARM_ARGS[@]}" \
  "${REPO_ARGS[@]}" --seeds $SEEDS --limit "$LIMIT" --format estimate | sed 's/^/[main_sweep] /'

# Credentials preflight (and the loud synthetic-run warning when RUNNER=stub).
te_check_routing "$AGENT"

# Copilot only: confirm the subscription actually serves this MODEL and that it accepts a
# reasoning effort. Both mistakes fail ONCE PER TASK rather than at launch -- the CLI exits
# before its first turn -- so without this a lane writes a whole tree of 0-turn rows that
# look like an agent that tried and failed. te_check_copilot_model hard-fails on an unserved
# model and only WARNS on a rejected effort (exporting TE_EFFORT_APPLIES=0, so rows record
# effort: null), because the latter is still a valid run that merely cannot hold effort
# fixed. It probes the harness default; the baseline-low / baseline-medium arms set
# TE_EFFORT_LEVEL themselves per cell, so a model that takes `high` and rejects a lower
# level would still surface at that arm rather than here.
if [[ "$AGENT" == copilot && "${RUNNER:-stub}" != "stub" ]]; then
  te_check_copilot_model "$MODEL" "${TE_EFFORT_LEVEL:-high}"
fi

# AFTER the preflights, so a dry run still refuses a missing credential. The BILLED probes
# skip themselves on a dry run (te_dryrun_skips_billed), and te_check_routing returns early
# on RUNNER=stub, so DRYRUN=1 on a fresh clone with nothing configured still costs nothing
# and still works.
if [[ "$DRYRUN" == "1" ]]; then
  echo "[main_sweep] DRYRUN -- the cells this lane would run, in order:"
  printf '  %s\n' "${PLAN[@]}"
  echo "[main_sweep] DRYRUN: nothing was run."
  exit 0
fi

mkdir -p "$DEST"

# Detach by default: the plan is resolved and credentials are checked, so everything from
# here belongs in a log rather than a terminal a laptop lid will close on.
te_detach "main_sweep__${AGENT}_${MODEL_TAG}_${DOMAIN}" "$0" "$@"

# expected_rows <repo> -- the task count of that group, from the workload manifest.
declare -A GROUP_N
while IFS=$'\t' read -r g n; do GROUP_N["$g"]="$n"; done < <(
  "$PY" "$HERE/../sweep_plan.py" --domain "$DOMAIN" "${WORKLOAD_ARGS[@]}" --format counts)

# cell_rows <arm> <repo> <seed> — how many row.json files that cell already holds.
cell_rows() {
  local tag="${2##*/}"; tag="${tag//-/_}"
  local dir="$DEST/$1/${tag}_s$3"
  [[ -d "$dir" ]] || { echo 0; return; }
  find "$dir" -name row.json 2>/dev/null | wc -l
}

FAILED=()
SKIPPED=0
RAN=0
START=$SECONDS
for line in "${PLAN[@]}"; do
  IFS=$'\t' read -r arm repo seed <<<"$line"
  want="${GROUP_N[$repo]:-0}"
  if [[ "$LIMIT" != "0" && "$want" -gt "$LIMIT" ]]; then want="$LIMIT"; fi
  have="$(cell_rows "$arm" "$repo" "$seed")"
  if [[ "$SKIP_DONE" == "1" && "$want" -gt 0 && "$have" -ge "$want" ]]; then
    echo "[main_sweep] SKIP  $arm $repo s$seed ($have/$want rows already stored)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  echo "[main_sweep] RUN   $arm $repo s$seed  (have $have of $want)"
  if "$PY" "$HERE/../run_experiment.py" \
      --arm "$arm" --agent "$AGENT" --repo "$repo" --seed "$seed" --limit "$LIMIT" \
      --adapter "$DOMAIN" --tasks-file "$TASKS_FILE" "${WORKLOAD_ARGS[@]}" \
      --results-root "$DEST" --runner "$RUNNER" \
      --model "$MODEL" --optimizer-model "$AGENT" --fresh; then
    RAN=$((RAN + 1))
  else
    echo "[main_sweep] FAILED $arm $repo s$seed"
    FAILED+=("$arm/$repo/s$seed")
    [[ "$ON_FAIL" == "abort" ]] && { echo "[main_sweep] ON_FAIL=abort -- stopping"; break; }
  fi
done

# Aggregate every arm PRESENT in the tree, not just this lane's: --arms limits the rewrite,
# so passing it would drop the other arms' rows from the index the comparisons read.
echo "[main_sweep] aggregating -> $DEST/index/"
"$PY" "$HERE/../aggregate.py" "$DEST"

echo "[main_sweep] DONE in $(( (SECONDS - START) / 60 )) min: ran=$RAN skipped=$SKIPPED failed=${#FAILED[@]}"
if ((${#FAILED[@]})); then
  printf '[main_sweep]   FAILED: %s\n' "${FAILED[@]}"
  echo "[main_sweep] re-run this same command to retry only the failed cells (SKIP_DONE=1"
  echo "[main_sweep] skips the finished ones)."
  exit 1
fi
