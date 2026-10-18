#!/usr/bin/env bash
# Reproduce a Claude/Copilot tri-mechanism A/B on one domain: the BASELINE arm (no
# mechanisms) and the TRIMECH arm (injection=manual4 + evidence untooling + spiral guard),
# then aggregate both into the index CSVs compare_arms.py reads.
#
# The exact arm configurations (the full catalog is in docs/RUNNING.md):
#   baseline  : native cost basis, no economizer
#   trimech   : --mode manual4 --untool-evidence 3 --untool-judge
#               --spiral-guard --spiral-judge --spiral-nudge
#   (Web-Bench streams are short, so the driver's `trimech-web` arm additionally sets
#    --spiral-nudge-llm --spiral-frozen-envelope 30; pass ARM=trimech-web for those.)
#
# The heavy task-execution harness (Docker + agent CLI + gold grading) lives below
# te_bench.runner.AgentRunner, in the repo's te_harness package, so
# RUNNER=te_harness.swebench:make_runner (or .webbench) needs no PYTHONPATH. Point RUNNER at
# your own factory ("module:make_runner") to substitute another. With RUNNER=stub you get the offline
# StubRunner (no Docker/LLM) used by smoke_test.sh -- synthetic, for wiring checks only.
#
# Usage:
#   AGENT=claude DOMAIN=swebench REPOS="pydata/xarray django/django" SEEDS="0 1 2" \
#   RUNNER=te_harness.swebench:make_runner \
#   TOKENECONOMIZER_SWEBENCH_TASKS=/path/tasks.json \
#   TOKENECONOMIZER_RESULTS=/path/results \
#     bash scripts/launch/trimech_ab.sh
#
# Env (all have safe defaults except the dataset/results placeholders):
#   AGENT     claude | copilot                       (default claude)
#   DOMAIN    swebench | webbench | creddata | reposvul   (default swebench)
#   ARM       trimech | trimech-web                   (default trimech)
#   ARMS      explicit space-separated arm list, overriding the default "baseline $ARM".
#             Use it to run ONE arm when a comparable baseline already exists in the tree
#             -- e.g. ARMS=trimech to add just the economized arm. Without it the launcher
#             re-runs baseline, which both costs an extra pass and supersedes the stored
#             baseline rows in the index (run dirs are timestamped and the aggregator keeps
#             the newest attempt per arm/cell/instance).
#   REPOS     space-separated repo slugs              (default "pydata/xarray")
#             on DOMAIN=webbench these are Web-Bench PROJECT names -- one project is one
#             stream of 20-21 sequentially dependent tasks (e.g. "calculator zustand react")
#   SEEDS     space-separated seeds                   (default "0")
#   LIMIT     cap tasks per (repo,seed), 0 = all      (default 0)
#   RUNNER    'stub' or 'module:factory'              (default stub)
#   TASKS_FILE   task-spec JSON  (default: the domain's TOKENECONOMIZER_*_TASKS env var)
#   WORKLOAD  workload manifest, or 'none'            (default workloads/$DOMAIN.json)
#   DRYRUN    1 = print the cells this lane would run and exit, run nothing (default 0)
#             The BILLED probes skip themselves, so it costs nothing.
#   DETACH    1 = run in the background (default), 0 = foreground
#   TE_LOG    log path when detached  (default $TOKENECONOMIZER_RESULTS/logs/<tag>__<ts>.log)
#   TOKENECONOMIZER_RESULTS   results root            (default: ./results)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"

# Credentials + data roots from the git-ignored repo-root .env (shell exports win).
# shellcheck source=./_env.sh
. "$HERE/_env.sh"
te_load_env

AGENT="${AGENT:-claude}"
DOMAIN="${DOMAIN:-swebench}"
ARM="${ARM:-trimech}"
# Arms to run, in order. Default is the paired A/B; ARMS lets a caller run a single arm.
ARMS="${ARMS:-baseline $ARM}"
REPOS="${REPOS:-pydata/xarray}"
SEEDS="${SEEDS:-0}"
LIMIT="${LIMIT:-0}"
RUNNER="${RUNNER:-stub}"
# Credentials preflight for a real run; for RUNNER=stub this prints the synthetic-run
# warning instead, so stub output is never mistaken for a benchmark.
te_check_routing "$AGENT"

# Model matched to the stored runs; only labels the cost, does not change the comparison.
case "$AGENT" in
  claude)  MODEL="${MODEL:-${ANTHROPIC_MODEL:-claude-opus-5}}" ;;
  # Native Copilot serves its OWN catalog, so an Anthropic model id is not a valid value
  # here. 'auto' lets the CLI route; pin COPILOT_MODEL (or MODEL) to fix the cost basis --
  # with an id your subscription serves. `copilot --model <id>` fails per task, not at
  # launch, so check first.
  copilot) MODEL="${MODEL:-${COPILOT_MODEL:-auto}}" ;;
  *)       MODEL="${MODEL:-agent}" ;;
esac
# The backend's spelling of a two-spelling id (claude-opus-4-8 / claude-opus-4.8); the
# probes below verify it. See te_model_for_agent.
MODEL="$(te_model_for_agent "$AGENT" "$MODEL")"

# Copilot only: confirm the pinned model is served and takes the requested effort. Both
# mistakes otherwise fail once per task instead of once at launch. Skipped for RUNNER=stub
# (no agent runs) and for MODEL=auto (the router takes no effort level).
if [[ "$AGENT" == "copilot" && "${RUNNER:-stub}" != "stub" ]]; then
  te_check_copilot_model "$MODEL" "${TE_EFFORT_LEVEL:-high}"
fi

# Dataset task-spec: default to the domain's placeholder env var.
case "$DOMAIN" in
  swebench) TASKS_FILE="${TASKS_FILE:-${TOKENECONOMIZER_SWEBENCH_TASKS:-}}" ;;
  webbench) TASKS_FILE="${TASKS_FILE:-${TOKENECONOMIZER_WEBBENCH_TASKS:-}}" ;;
  creddata) TASKS_FILE="${TASKS_FILE:-${TOKENECONOMIZER_CREDDATA_TASKS:-}}" ;;
  reposvul) TASKS_FILE="${TASKS_FILE:-${TOKENECONOMIZER_REPOSVUL_TASKS:-}}" ;;
  *) echo "unknown DOMAIN=$DOMAIN"; exit 2 ;;
esac
if [[ -z "$TASKS_FILE" ]]; then
  echo "!! no task-spec JSON: set TASKS_FILE or the domain placeholder env var" >&2
  exit 2
fi

# Canonical workload manifest (committed, tiny): pins the run to exactly the evaluated
# instances, so TASKS_FILE may hold a superset. WORKLOAD=none restores the legacy behavior
# of taking whatever TASKS_FILE contains, filtered by the --repo prefix match.
WORKLOAD="${WORKLOAD:-$REPO_ROOT/workloads/$DOMAIN.json}"
WORKLOAD_ARGS=()
if [[ "$WORKLOAD" != "none" ]]; then
  if [[ ! -f "$WORKLOAD" ]]; then
    echo "!! workload manifest not found: $WORKLOAD (set WORKLOAD=none to run unfiltered)" >&2
    exit 2
  fi
  WORKLOAD_ARGS=(--workload "$WORKLOAD")
fi

# `trimech-web` is the arm tuned for Web-Bench's short streams (it adds the LLM nudge and a
# cold-start frozen envelope). Running plain `trimech` there is a valid choice, not an error,
# so say which one is about to run rather than second-guessing it.
if [[ "$DOMAIN" == "webbench" && "$ARM" != "trimech-web" ]]; then
  echo "[trimech_ab] note: DOMAIN=webbench with ARM=$ARM -- the arm tuned for Web-Bench's" >&2
  echo "[trimech_ab]       short streams is ARM=trimech-web (see docs/RUNNING.md)" >&2
fi

RESULTS_ROOT="${TOKENECONOMIZER_RESULTS:-$REPO_ROOT/results}"
# <agent>_<domain>_<model-tag>. The MODEL is part of the cost basis, so it belongs in the
# tree name exactly as it does in main_sweep.sh: without the tag an opus lane and a sonnet
# lane land in ONE tree and aggregate.py averages two cost bases into a number that
# describes neither.
MODEL_TAG="$(te_model_tag "$MODEL")"
DEST="$RESULTS_ROOT/$(te_tree_name "$AGENT" "$DOMAIN" "$MODEL_TAG")"
mkdir -p "$DEST"

# DRYRUN=1 prints the cells this launcher would run and exits WITHOUT detaching. It has to
# come before te_detach: this script used to detach first, so `DRYRUN=1 bash
# scripts/launch/trimech_ab.sh` returned the terminal and then billed a full A/B in the
# background -- the opposite of what a dry run is for.
if [[ "${DRYRUN:-0}" == "1" ]]; then
  echo "[trimech_ab] DRYRUN -- agent=$AGENT domain=$DOMAIN model=$MODEL"
  echo "[trimech_ab] dest=$DEST"
  echo "[trimech_ab] the cells this lane would run, in order:"
  for seed in $SEEDS; do for repo in $REPOS; do for arm in $ARMS; do
    printf '  %s\t%s\t%s\n' "$arm" "$repo" "$seed"
  done; done; done
  echo "[trimech_ab] DRYRUN: nothing was run."
  exit 0
fi

# Detach by default: config is resolved and credentials are checked, so anything that
# fails from here on is a run-time failure that belongs in the log, not the terminal.
te_detach "trimech_ab__${AGENT}_${DOMAIN}_${ARM}" "$0" "$@"

echo "[trimech_ab] agent=$AGENT domain=$DOMAIN arms=($ARMS) repos='$REPOS' seeds='$SEEDS' runner=$RUNNER"
echo "[trimech_ab] dest=$DEST tasks=$TASKS_FILE workload=$WORKLOAD"

for seed in $SEEDS; do
  for repo in $REPOS; do
    for arm in $ARMS; do
      "$PY" "$HERE/../run_experiment.py" \
        --arm "$arm" --agent "$AGENT" --repo "$repo" --seed "$seed" --limit "$LIMIT" \
        --adapter "$DOMAIN" --tasks-file "$TASKS_FILE" "${WORKLOAD_ARGS[@]}" \
        --results-root "$DEST" --runner "$RUNNER" \
        --model "$MODEL" --optimizer-model "$AGENT" --fresh \
      || { echo "[trimech_ab] FAILED arm=$arm repo=$repo seed=$seed"; exit 1; }
    done
  done
done

echo "[trimech_ab] aggregating -> $DEST/index/"
# Aggregate ALL arms present, not just the ones this invocation ran: --arms limits the
# rewrite and would drop every other arm's rows from the index the comparisons read.
"$PY" "$HERE/../aggregate.py" "$DEST"
echo "[trimech_ab] DONE. Compare an arm with its baseline, cell by cell:"
echo "    $PY scripts/compare_arms.py --baseline $DEST/baseline/<cell> --arm $DEST/<arm>/<cell>"
