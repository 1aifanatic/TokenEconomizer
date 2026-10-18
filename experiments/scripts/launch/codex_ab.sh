#!/usr/bin/env bash
# Reproduce a Codex-native A/B: a BASELINE arm and one of the two independently-effective
# Codex mechanisms (or their combo), then aggregate into the index CSVs compare_arms.py reads.
#
# Exact arm configurations (the full catalog is in docs/RUNNING.md section 5):
#   baseline      : native Codex cost basis, no economizer
#   codex-untool  : --codex-recent-adaptive-untool-v7   (evidence-gated capability untooling;
#                   deferred_wait is CORE and never demoted)
#   codex-spiral  : --codex-spiral-resume, DEFENSIVE profile since 2026-09-12 (every
#                   deterministic spiral signature as a same-thread nudge, no abort, no
#                   effort change). `codex-spiral-defensive` is an alias of it.
#   codex-spiral-postgreen
#                 : the shipped post-green-v3 finalize alone (resolve-safe finalize over
#                   cleared scopes) -- the named ablation of the profile above
#   codex-combo   : both co-activated (--codex-untool-spiral-combo)
#   codex-trimech : ALL THREE Codex mechanisms -- injection (envfacts+distiller) +
#                   capability untooling + the defensive spiral. The Codex sibling of
#                   `trimech`; a PAID arm (the proposer bills).
#   rtk / codex-trimech-rtk : rtk in its Codex INSTRUCTION mode (AGENTS.md + RTK.md under
#                   CODEX_HOME, inlined; no hook -- Codex honours none). Rows carry
#                   rtk_compliance: zero compliance means the arm ran native.
#   codex-trimech-repomap : codex-trimech + aider's repo map as the project AGENTS.md
#                   (SWE-bench only). Rows carry map_ok / map_chars: map_ok false means
#                   the task ran as plain codex-trimech.
#
# The heavy harness lives below te_bench.runner.AgentRunner, in the repo's te_harness
# package (RUNNER=te_harness.swebench:make_runner, no PYTHONPATH needed). Point RUNNER at
# another factory ("module:make_runner") to substitute one; RUNNER=stub uses the offline
# StubRunner (synthetic).
#
# Usage:
#   ARM=codex-untool REPOS="pydata/xarray" SEEDS="0" \
#   RUNNER=te_harness.swebench:make_runner \
#   TOKENECONOMIZER_SWEBENCH_TASKS=/path/tasks.json TOKENECONOMIZER_RESULTS=/path/results \
#     bash scripts/launch/codex_ab.sh
#
# Env:
#   ARM     codex-untool | codex-spiral | codex-spiral-postgreen | codex-spiral-defensive |
#           codex-combo | codex-trimech | codex-inject | codex-inject-untool |
#           codex-inject-envfacts | codex-inject-distiller      (default codex-untool)
#
#           codex-inject is the composed `envfacts+distiller` profile since 2026-09-12 and
#           is therefore a PAID arm (the proposer bills; the endpoint preflight fires).
#           codex-inject-envfacts is the LLM-free ablation; codex-inject-distiller is the
#           old name of the default and resolves to the same configuration.
#   DOMAIN  swebench (default) | webbench. Web-Bench runs the agent on the HOST with no
#           per-task container, so the Codex lane is guarded there by an /etc/hosts block in
#           an unprivileged user+mount namespace rather than by a hook the CLI ignores; the
#           runner probes that at construction and refuses to pretend otherwise. Selects the
#           tasks file, the workload manifest and the RUNNER default.
#   BASELINE 1 = run baseline too (paired A/B, default); 0 = run only $ARM against a
#           baseline already stored elsewhere; only = run ONLY the baseline arm (a fresh
#           baseline lane to pair with an arm running concurrently in another lane)
#   REPOS   space-separated repo slugs                  (default "pydata/xarray")
#   SEEDS   space-separated seeds                        (default "0")
#   LIMIT   cap tasks per (repo,seed), 0 = all           (default 0)
#   RUNNER  'stub' or 'module:factory'                   (default stub)
#   TASKS_FILE  task-spec JSON (default TOKENECONOMIZER_SWEBENCH_TASKS)
#   WORKLOAD  workload manifest, or 'none'               (default workloads/swebench.json)
#   DETACH    1 = run in the background (default), 0 = foreground
#   SKIP_DONE 1 = skip a cell that already holds its full row count (default 1), so a lane
#             that died part way RESUMES instead of re-paying for the cells it finished --
#             the same contract main_sweep.sh gives the Claude/Copilot lanes
#   ON_FAIL   continue | abort  (default continue: one bad cell must not take a 360-cell
#             lane with it; the lane still exits nonzero and lists the failures)
#   DRYRUN    1 = print the cells and exit without running anything
#   TE_LOG    log path when detached (default $TOKENECONOMIZER_RESULTS/logs/<tag>__<ts>.log)
#   TOKENECONOMIZER_RESULTS  results root                (default ./results)
#   TE_FLEET_CPUS  CPU budget for EVERY lane's task containers together, in cores
#                  (default 16; 0/none = do not enforce). Written once, at launch, onto the
#                  cgroup every rootless-docker container scope shares, so it also binds
#                  lanes that are already running -- see fleet_budget.sh for why a
#                  per-container cap is not enough when several lanes run at once.
#   TE_SWEBENCH_MAX_CPUS  per-CONTAINER cap and the matching OMP/MKL/joblib thread caps
#                  (default 4 here, against the harness default of 16): it is what keeps a
#                  parallel test run from sizing its worker pool to the whole host and then
#                  thrashing inside the fleet budget.
#   OPENAI_API_KEY  the OpenAI API key Codex bills (required for a real run)
#   OPENAI_BASE_URL only for a key issued against a regional OpenAI endpoint
#                   (https://<region>.api.openai.com/v1); any other host is refused
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"

# Credentials + data roots from the git-ignored repo-root .env (shell exports win).
# shellcheck source=./_env.sh
. "$HERE/_env.sh"
# shellcheck source=./fleet_budget.sh
. "$HERE/fleet_budget.sh"
te_load_env

# A lane is one container at a time, but several lanes are several containers, and the
# per-container default (16) is a ceiling per lane rather than for the host. Pick a
# per-container cap that keeps a parallel test suite from oversubscribing, and let the
# fleet cgroup cap below hold the aggregate.
export TE_SWEBENCH_MAX_CPUS="${TE_SWEBENCH_MAX_CPUS:-4}"

ARM="${ARM:-codex-untool}"
case "$ARM" in baseline|baseline-medium|baseline-low|repomap|rtk|serena|codex-untool|codex-spiral|codex-spiral-postgreen|codex-spiral-defensive|codex-combo|codex-trimech|codex-trimech-rtk|codex-trimech-repomap|codex-inject|codex-inject-untool|codex-inject-envfacts|codex-inject-distiller) ;; *)
  echo "ARM must be baseline | baseline-medium | baseline-low | repomap | rtk | serena | codex-untool | codex-spiral | codex-spiral-postgreen | codex-spiral-defensive | codex-combo | codex-trimech | codex-trimech-rtk | codex-trimech-repomap | codex-inject | codex-inject-untool | codex-inject-envfacts | codex-inject-distiller (got $ARM)"; exit 2 ;; esac
# BASELINE=0 runs ONLY $ARM, for a lane whose baseline already exists in a stored tree
# (compare with scripts/compare_arms.py). BASELINE=only runs ONLY the baseline arm, so a
# fresh baseline and an arm can run as two concurrent lanes into the same tree. Default 1 =
# the paired A/B this script is named for.
BASELINE="${BASELINE:-1}"
case "$BASELINE" in
  0)    ARMS_TO_RUN=("$ARM") ;;
  only) ARMS_TO_RUN=(baseline) ;;
  *)    ARMS_TO_RUN=(baseline "$ARM") ;;
esac
LANE_TAG="$([[ "$BASELINE" == "only" ]] && echo baseline || echo "$ARM")"
REPOS="${REPOS:-pydata/xarray}"
SEEDS="${SEEDS:-0}"
LIMIT="${LIMIT:-0}"
RUNNER="${RUNNER:-stub}"
SKIP_DONE="${SKIP_DONE:-1}"
ON_FAIL="${ON_FAIL:-continue}"
# Probed 2026-09-09: `gpt-5.6-sol` is listed and ACCEPTED on /v1/responses by an OpenAI
# key. Do not pin a `-codex` ALIAS (gpt-5.1-codex, ...): /v1/models lists them and
# /v1/responses 404s them, which is a per-task failure rather than a launch error.
AGENT=codex
MODEL="${MODEL:-${CODEX_MODEL:-gpt-5.6-sol}}"
te_check_routing "$AGENT"

DOMAIN="${DOMAIN:-swebench}"
case "$DOMAIN" in
  swebench) DOMAIN_TASKS="${TOKENECONOMIZER_SWEBENCH_TASKS:-}" ;;
  webbench) DOMAIN_TASKS="${TOKENECONOMIZER_WEBBENCH_TASKS:-}" ;;
  *) echo "!! DOMAIN must be swebench|webbench (got '$DOMAIN')" >&2; exit 2 ;;
esac
TASKS_FILE="${TASKS_FILE:-$DOMAIN_TASKS}"
if [[ -z "$TASKS_FILE" ]]; then
  echo "!! no task-spec JSON: set TASKS_FILE or TOKENECONOMIZER_${DOMAIN^^}_TASKS" >&2
  exit 2
fi
# The runner must match the domain: a swebench factory on a webbench task list would run the
# wrong lifecycle against the right ids.
if [[ "${RUNNER:-stub}" != "stub" ]]; then
  case "$RUNNER" in *"$DOMAIN"*) ;; *)
    echo "!! RUNNER=$RUNNER does not match DOMAIN=$DOMAIN (expected te_harness.$DOMAIN:make_runner)" >&2
    exit 2 ;;
  esac
fi

# Canonical workload manifest (committed, tiny): pins the run to exactly the evaluated
# instances. WORKLOAD=none falls back to whatever TASKS_FILE contains.
WORKLOAD="${WORKLOAD:-$REPO_ROOT/workloads/$DOMAIN.json}"
WORKLOAD_ARGS=()
if [[ "$WORKLOAD" != "none" ]]; then
  if [[ ! -f "$WORKLOAD" ]]; then
    echo "!! workload manifest not found: $WORKLOAD (set WORKLOAD=none to run unfiltered)" >&2
    exit 2
  fi
  WORKLOAD_ARGS=(--workload "$WORKLOAD")
fi

RESULTS_ROOT="${TOKENECONOMIZER_RESULTS:-$REPO_ROOT/results}"
# codex_<domain>_<model-tag>. The MODEL is part of the cost basis, so it belongs in the
# tree name -- otherwise two models' lanes share a tree and aggregate.py averages two bases.
MODEL_TAG="$(te_model_tag "$MODEL")"
DEST="$RESULTS_ROOT/$(te_tree_name "$AGENT" "$DOMAIN" "$MODEL_TAG")"
mkdir -p "$DEST"

# DRYRUN=1 prints the cells and exits WITHOUT detaching -- before te_detach, so a dry run
# cannot hand back the terminal and then bill a full A/B in the background.
if [[ "${DRYRUN:-0}" == "1" ]]; then
  echo "[codex_ab] DRYRUN -- agent=$AGENT model=$MODEL"
  echo "[codex_ab] dest=$DEST"
  echo "[codex_ab] the cells this lane would run, in order:"
  for seed in $SEEDS; do for repo in $REPOS; do for arm in "${ARMS_TO_RUN[@]}"; do
    printf '  %s\t%s\t%s\n' "$arm" "$repo" "$seed"
  done; done; done
  echo "[codex_ab] DRYRUN: nothing was run."
  exit 0
fi

# Detach by default: config is resolved and credentials are checked, so anything that
# fails from here on is a run-time failure that belongs in the log, not the terminal.
te_detach "codex_ab__${LANE_TAG}" "$0" "$@"

# One budget for every lane on the host, re-asserted here so a launch cannot inherit an
# uncapped cgroup from an earlier session. Idempotent; never fatal.
te_fleet_cpu_cap "${TE_FLEET_CPUS:-16}"
te_fleet_cpu_report

echo "[codex_ab] domain=$DOMAIN agent=$AGENT model=$MODEL arms=(${ARMS_TO_RUN[*]}) repos='$REPOS' seeds='$SEEDS' runner=$RUNNER dest=$DEST"
echo "[codex_ab] cpus: per-container ${TE_SWEBENCH_MAX_CPUS}, fleet ${TE_FLEET_CPUS:-16}"
echo "[codex_ab] tasks=$TASKS_FILE workload=$WORKLOAD"
echo "[codex_ab] skip_done=$SKIP_DONE on_fail=$ON_FAIL"

# expected_rows <repo> -- the task count of that group, from the committed workload record.
# Same source main_sweep.sh uses: the counts are per GROUP and carry no arm or agent, so
# the Codex lane reads them unchanged.
declare -A GROUP_N
while IFS=$'\t' read -r g n; do GROUP_N["$g"]="$n"; done < <(
  "$PY" "$HERE/../sweep_plan.py" --domain "$DOMAIN" --format counts)

# cell_rows <arm> <repo> <seed> -- how many row.json files that cell already holds.
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
for seed in $SEEDS; do
  for repo in $REPOS; do
    for arm in "${ARMS_TO_RUN[@]}"; do
      want="${GROUP_N[$repo]:-0}"
      if [[ "$LIMIT" != "0" && "$want" -gt "$LIMIT" ]]; then want="$LIMIT"; fi
      have="$(cell_rows "$arm" "$repo" "$seed")"
      if [[ "$SKIP_DONE" == "1" && "$want" -gt 0 && "$have" -ge "$want" ]]; then
        echo "[codex_ab] SKIP  $arm $repo s$seed ($have/$want rows already stored)"
        SKIPPED=$((SKIPPED + 1))
        continue
      fi
      echo "[codex_ab] RUN   $arm $repo s$seed  (have $have of $want)"
      if "$PY" "$HERE/../run_experiment.py" \
          --arm "$arm" --agent "$AGENT" --repo "$repo" --seed "$seed" --limit "$LIMIT" \
          --adapter "$DOMAIN" --tasks-file "$TASKS_FILE" "${WORKLOAD_ARGS[@]}" \
          --results-root "$DEST" --runner "$RUNNER" \
          --model "$MODEL" --optimizer-model agent --max-turns 80 --fresh; then
        RAN=$((RAN + 1))
      else
        echo "[codex_ab] FAILED arm=$arm repo=$repo seed=$seed"
        FAILED+=("$arm/$repo/s$seed")
        [[ "$ON_FAIL" == "abort" ]] && { echo "[codex_ab] ON_FAIL=abort -- stopping"; break 3; }
      fi
    done
  done
done

# Aggregate every arm PRESENT in the tree, not just this lane's: --arms limits the rewrite,
# so passing it would drop the other arms' rows from the index the comparisons read. This
# lane runs ONE arm per invocation (docs/RUNNING.md section 5), so passing it would
# leave the index holding whichever arm was launched last.
echo "[codex_ab] aggregating -> $DEST/index/"
"$PY" "$HERE/../aggregate.py" "$DEST"
echo "[codex_ab] DONE in $(( (SECONDS - START) / 60 )) min: ran=$RAN skipped=$SKIPPED failed=${#FAILED[@]}"
if ((${#FAILED[@]})); then
  printf '[codex_ab] FAILED CELL: %s\n' "${FAILED[@]}"
fi
echo "[codex_ab] Compare an arm with its baseline, cell by cell:"
echo "    $PY scripts/compare_arms.py --baseline $DEST/baseline/<cell> --arm $DEST/<arm>/<cell>"
((${#FAILED[@]} == 0))
