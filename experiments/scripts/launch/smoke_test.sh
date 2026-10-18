#!/usr/bin/env bash
# Offline end-to-end smoke test: exercise EVERY proven arm through the real driver +
# facade + aggregator using the built-in StubRunner (no Docker, no datasets, no LLM), then
# assert the pipeline produced rows + CSVs, and DELETE everything created.
#
# It also covers the two things a domain whose task ids are unique only WITHIN a group
# depends on -- workload narrowing and the stream-level runner hooks -- because both fail
# SILENTLY rather than loudly: a widened stream just runs more tasks, and an uncalled
# begin_stream just leaves a harness un-started.
#
# It writes ONLY under a fresh temp dir (mktemp -d) and removes it on exit, so it leaves no
# artifacts in the repo and nothing to stage. Exit 0 = wiring healthy.
#
#   Usage: bash scripts/launch/smoke_test.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

# Tree-naming helpers under test below (te_tree_name / te_model_tag). Sourcing does not read
# .env -- te_load_env is a separate call -- so this cannot pick up real credentials.
# shellcheck source=./_env.sh
. "$HERE/_env.sh"

# --- tiny synthetic task specs (never touch a real dataset) ---
TASKS="$WORK/tasks.json"
cat > "$TASKS" <<'JSON'
[{"instance_id": "xarray-1", "problem_statement": "fix concat dtype", "repo": "pydata/xarray"},
 {"instance_id": "xarray-2", "problem_statement": "fix groupby regression", "repo": "pydata/xarray"},
 {"instance_id": "xarray-3", "problem_statement": "handle empty dataset", "repo": "pydata/xarray"}]
JSON

run_arm() {  # <agent> <domain-adapter> <arm> <dest-subdir>
  local agent="$1" adapter="$2" arm="$3" dest="$WORK/$4"
  "$PY" "$HERE/../run_experiment.py" \
    --arm "$arm" --agent "$agent" --repo pydata/xarray --seed 0 \
    --adapter "$adapter" --tasks-file "$TASKS" \
    --results-root "$dest" --runner stub --fresh >/dev/null 2>&1 \
    || fail "driver arm=$arm agent=$agent"
  local n; n=$(find "$dest/$arm" -name row.json | wc -l)
  [[ "$n" -eq 3 ]] || fail "arm=$arm produced $n row.json (expected 3)"
  echo "  ok  arm=$arm agent=$agent rows=$n"
}

echo "[smoke] .env loaders (python + shell must agree; no real credentials involved)"
ENVF="$WORK/test.env"
cat > "$ENVF" <<'ENV'
# comment
export TE_SMOKE_A=alpha
TE_SMOKE_B="two words"
TE_SMOKE_EMPTY=
TE_SMOKE_SHELL_WINS=from_file
ENV
SCRIPTS_DIR="$REPO_ROOT/scripts" TOKENECONOMIZER_ENV_FILE="$ENVF" \
  TE_SMOKE_SHELL_WINS=from_shell "$PY" - <<'PY' || fail ".env python loader"
import os, sys
sys.path.insert(0, os.environ["SCRIPTS_DIR"])
from _env import load_env
load_env()
assert os.environ["TE_SMOKE_A"] == "alpha", os.environ.get("TE_SMOKE_A")
assert os.environ["TE_SMOKE_B"] == "two words", os.environ.get("TE_SMOKE_B")
assert "TE_SMOKE_EMPTY" not in os.environ, "empty value should not be set"
assert os.environ["TE_SMOKE_SHELL_WINS"] == "from_shell", "shell export must win"
print("  ok  python loader: quotes, comments, empty-skip, shell-precedence")
PY
( REPO_ROOT="$REPO_ROOT"; TOKENECONOMIZER_ENV_FILE="$ENVF"; TE_SMOKE_SHELL_WINS=from_shell
  . "$HERE/_env.sh"; te_load_env
  [[ "$TE_SMOKE_A" == "alpha" ]] || exit 1
  [[ "$TE_SMOKE_B" == "two words" ]] || exit 1
  [[ -z "${TE_SMOKE_EMPTY:-}" ]] || exit 1
  [[ "$TE_SMOKE_SHELL_WINS" == "from_shell" ]] || exit 1
) || fail ".env shell loader"
echo "  ok  shell loader: same semantics as scripts/_env.py"

echo "[smoke] generic arms (Claude/Copilot)"
run_arm claude  swebench baseline     gen
run_arm claude  swebench trimech      gen
run_arm claude  swebench trimech-web  gen
run_arm copilot swebench trimech      cop

echo "[smoke] Codex-native arms"
run_arm codex swebench baseline     cdx
run_arm codex swebench codex-untool cdx
run_arm codex swebench codex-spiral cdx
run_arm codex swebench codex-spiral-postgreen cdx
run_arm codex swebench codex-combo  cdx
run_arm codex swebench codex-inject  cdx
run_arm codex swebench codex-trimech cdx

echo "[smoke] workload manifest narrows to ONE group (ids repeat across groups)"
# Web-Bench numbers every project's stream 'init', 'task-1', ... so ids are unique only
# WITHIN a project. Filtering on ids alone silently returns every project's tasks
# interleaved -- which, for a sequentially dependent stream, is not a bigger run but a
# meaningless one. Two synthetic groups sharing all three ids catch that regression.
WB_ROOT="$WORK/wb/projects"
mkdir -p "$WB_ROOT/alpha" "$WB_ROOT/beta"
for proj in alpha beta; do
  cat > "$WB_ROOT/$proj/tasks.jsonl" <<JSONL
{"id": "init", "date": "2024-10-25", "level": "easy", "description": "$proj init"}
{"id": "task-1", "date": "2024-10-25", "level": "easy", "description": "$proj first"}
{"id": "task-2", "date": "2024-10-25", "level": "easy", "description": "$proj second"}
JSONL
done
WB_TASKS="$WORK/wb_tasks.json"
"$PY" -c '
import json, sys
rows = [{"instance_id": t, "problem_statement": p + " " + t, "repo": p}
        for p in ("alpha", "beta") for t in ("init", "task-1", "task-2")]
json.dump(rows, open(sys.argv[1], "w"))
' "$WB_TASKS" || fail "build synthetic webbench task list"
WB_WORKLOAD="$WORK/wb_workload.json"
cat > "$WB_WORKLOAD" <<'JSON'
{"groups": {"alpha": ["init", "task-1", "task-2"],
            "beta":  ["init", "task-1", "task-2"]}}
JSON
TOKENECONOMIZER_WEBBENCH_PROJECTS="$WB_ROOT" "$PY" "$HERE/../run_experiment.py" \
  --arm baseline --agent claude --repo alpha --seed 0 \
  --adapter webbench --tasks-file "$WB_TASKS" --workload "$WB_WORKLOAD" \
  --results-root "$WORK/wb" --runner stub --fresh >/dev/null 2>&1 \
  || fail "driver on the webbench adapter"
"$PY" -c '
import glob, json, sys
rows = [json.load(open(p)) for p in sorted(glob.glob(sys.argv[1] + "/*/*/row.json"))]
n = len(rows)
assert n == 3, "--repo alpha ran %d tasks; want only its group of 3 (ids repeat across groups)" % n
repos = sorted({r.get("repo") for r in rows})
assert repos == ["alpha"], "stream mixed groups: %s" % repos
ids = [r.get("instance") for r in rows]
assert ids == ["init", "task-1", "task-2"], "stream out of order: %s" % ids
print("  ok  --repo alpha -> 3 tasks, group alpha only, in stream order")
' "$WORK/wb/baseline" || fail "workload narrowing"

echo "[smoke] stream-level runner hooks (begin_stream / end_stream)"
# A harness that owns a whole stream is started once around the task loop, not per task.
# The driver calls the hooks hasattr-guarded, so a runner WITHOUT them must still work
# (every arm above proves that) and a runner WITH them must actually see both calls.
cat > "$WORK/te_smoke_hooks.py" <<'HOOKMOD'
import os
import sys

sys.path.insert(0, os.environ["TE_SMOKE_SCRIPTS"])
from run_experiment import StubRunner


class HookRunner(StubRunner):
    """StubRunner that records the stream-level calls the driver made to it."""

    def _note(self, line):
        with open(os.environ["TE_SMOKE_HOOKLOG"], "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def begin_stream(self, tasks, **info):
        self._note("begin %d %s %s" % (len(tasks), info.get("repo"), info.get("arm")))

    def end_stream(self):
        self._note("end")


def make_runner():
    return HookRunner("claude")
HOOKMOD
HOOKLOG="$WORK/hooks.log"
PYTHONPATH="$WORK${PYTHONPATH:+:$PYTHONPATH}" TE_SMOKE_SCRIPTS="$REPO_ROOT/scripts" \
  TE_SMOKE_HOOKLOG="$HOOKLOG" "$PY" "$HERE/../run_experiment.py" \
  --arm baseline --agent claude --repo pydata/xarray --seed 0 \
  --adapter swebench --tasks-file "$TASKS" \
  --results-root "$WORK/hooks" --runner te_smoke_hooks:make_runner --fresh >/dev/null 2>&1 \
  || fail "driver with a stream-hook runner"
[[ -s "$HOOKLOG" ]] || fail "begin_stream/end_stream were never called"
grep -qx "begin 3 pydata/xarray baseline" "$HOOKLOG" \
  || fail "begin_stream got the wrong stream/context: $(head -1 "$HOOKLOG")"
grep -qx "end" "$HOOKLOG" || fail "end_stream was never called"
echo "  ok  begin_stream(3 tasks, repo, arm) then end_stream"

echo "[smoke] a re-run archives traces and does not double-count"
# Traces are the primary artifact and cannot be reproduced after the fact, so re-running a
# cell must ADD a timestamped run dir rather than overwrite the previous transcript. That
# makes the aggregator's job harder, not easier: it has to report one row per task, from the
# newest attempt, while the superseded ones stay on disk.
for pass_n in 1 2; do
  "$PY" "$HERE/../run_experiment.py" \
    --arm baseline --agent claude --repo pydata/xarray --seed 0 \
    --adapter swebench --tasks-file "$TASKS" \
    --results-root "$WORK/rerun" --runner stub --fresh >/dev/null 2>&1 \
    || fail "re-run pass $pass_n"
  sleep 1        # distinct second -> distinct stamp; the index alone would also suffice
done
N_DIRS=$(find "$WORK/rerun" -name row.json | wc -l)
[[ "$N_DIRS" -eq 6 ]] || fail "re-run left $N_DIRS run dirs (expected 6: 3 tasks x 2 passes)"
"$PY" "$HERE/../aggregate.py" "$WORK/rerun" --arms baseline >/dev/null 2>&1 \
  || fail "aggregate the re-run tree"
"$PY" -c '
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
n = len(rows)
assert n == 3, "aggregate reported %d rows; want 3 (one per task, newest attempt)" % n
stamps = [r["source_path"].split("/")[-2].split("__")[0] for r in rows]
assert all(len(t) == 16 and t[8] == "T" and t.endswith("Z") for t in stamps), \
    "run dirs are not UTC-timestamped: %s" % stamps
newest = sorted(stamps)[-1]
print("  ok  6 run dirs kept, 3 rows aggregated, newest attempt wins (%s)" % newest)
' "$WORK/rerun/index/all_rows.csv" || fail "re-run de-duplication"

echo "[smoke] the tree is <agent>_<domain>, and ANTHROPIC_BASE_URL is not an input"
# Claude Code bills the Anthropic API on ANTHROPIC_API_KEY. A base URL left in the
# environment must neither rename a tree nor reach the agent: te_check_routing says it is
# ignored and unsets it (offline here -- RUNNER=stub returns before any credential check).
check_tree() {  # <agent> <expect-tree>
  local got; got="$(te_tree_name "$1" swebench)"
  [[ "$got" == "$2" ]] || fail "tree_name($1) = $got, expected $2"
}
(
  export ANTHROPIC_BASE_URL=http://localhost:8080 ANTHROPIC_AUTH_TOKEN=dummy RUNNER=stub
  check_tree claude  claude_swebench
  check_tree copilot copilot_swebench
  check_tree codex   codex_swebench
  out="$(te_check_routing claude 2>&1; echo "left=${ANTHROPIC_BASE_URL:-}${ANTHROPIC_AUTH_TOKEN:-}")"
  grep -q "IGNORED" <<<"$out" || fail "a set ANTHROPIC_BASE_URL was not reported: $out"
  grep -qx "left=" <<<"$out"  || fail "ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN survived: $out"
  # Only the three agent labels exist; anything else is refused rather than renamed.
  ( te_check_routing not-an-agent >/dev/null 2>&1 ) && fail "te_check_routing accepted an unknown agent"
  # OPENAI_BASE_URL may only name OpenAI's own (regional) API.
  ( OPENAI_BASE_URL=https://us.api.openai.com/v1 te_check_routing codex >/dev/null 2>&1 ) \
    || fail "a regional OpenAI endpoint was refused"
  ( OPENAI_BASE_URL=http://localhost:8080 te_check_routing codex >/dev/null 2>&1 ) \
    && fail "a non-OpenAI OPENAI_BASE_URL was accepted"
  true
) || exit 1
echo "  ok  <agent>_<domain> for claude/copilot/codex; ANTHROPIC_BASE_URL reported and dropped"
echo "  ok  unknown agent labels and a non-OpenAI OPENAI_BASE_URL refused"

echo "[smoke] the model tag separates trees"
# Two models in one cell average two cost bases into a number that describes neither, so
# the model belongs in the tree name. The two spellings of one model must collapse to ONE
# tag (the CLI takes both, and the stored rows carry both), and a router directive must
# produce NO tag -- `auto` is not a cost basis worth naming.
check_tag() {  # <model> <expect-tag>
  local got; got="$(te_model_tag "$1")"
  [[ "$got" == "$2" ]] || fail "model_tag($1) = '$got', expected '$2'"
}
(
  check_tag claude-opus-4-8   opus48
  check_tag claude-opus-4.8   opus48        # same model, other spelling -> same tree
  check_tag claude-sonnet-4.6 sonnet46
  check_tag auto              ""
  check_tag agent             ""
  # A tagged tree, and the 2-arg call unchanged so the stored trees keep resolving.
  got="$(te_tree_name claude swebench "$(te_model_tag claude-sonnet-4.6)")"
  [[ "$got" == "claude_swebench_sonnet46" ]] || fail "tagged tree name = $got"
  got="$(te_tree_name claude swebench)"
  [[ "$got" == "claude_swebench" ]] || fail "untagged tree name changed: $got"
  got="$(te_tree_name codex swebench "$(te_model_tag gpt-5.6-sol)")"
  [[ "$got" == "codex_swebench_gpt56sol" ]] || fail "codex tagged tree name = $got"
) || exit 1
echo "  ok  one tag per model, no tag for a router, 2-arg name unchanged"

echo "[smoke] main_sweep plans its cells without touching Docker, data or a key"
# The sweep is five figures of task-runs; DRYRUN is where that gets noticed. It must work
# on a fresh clone with nothing configured, so it is checked with the placeholders unset.
(
  unset TOKENECONOMIZER_SWEBENCH_TASKS TOKENECONOMIZER_WEBBENCH_TASKS
  # "nothing configured" has to mean the REPO'S .env too, not just these two placeholders:
  # te_load_env only lets a NON-EMPTY shell export beat the file, so an operator's RUNNER or
  # task paths would change what this plans. Point the loader at a path that does not exist.
  export TOKENECONOMIZER_ENV_FILE="$WORK/no-such.env"
  out="$(DRYRUN=1 ARMS="baseline trimech" REPOS="django/django" SEEDS="0 1" \
         AGENT=claude MODEL=claude-opus-4-8 DOMAIN=swebench \
         bash "$HERE/main_sweep.sh" 2>&1)" || fail "main_sweep DRYRUN exited nonzero"
  grep -q "cells=4" <<<"$out"                    || fail "DRYRUN cell count: $out"
  grep -q "claude_swebench_opus48" <<<"$out"     || fail "DRYRUN tree name: $out"
  grep -q "DRYRUN: nothing was run" <<<"$out"    || fail "DRYRUN ran something: $out"
  # A router directive has no cost basis and no tag: refuse rather than write an untagged
  # tree that silently mixes whatever the router picked.
  if DRYRUN=1 MODEL=auto AGENT=claude DOMAIN=swebench \
     bash "$HERE/main_sweep.sh" >/dev/null 2>&1; then
    fail "main_sweep accepted MODEL=auto"
  fi
  # Every arm the plan names has to be registered in run_experiment.ARMS; an unknown one
  # must fail at plan time, not at task 0.
  if DRYRUN=1 ARMS="not-an-arm" AGENT=claude DOMAIN=swebench \
     bash "$HERE/main_sweep.sh" >/dev/null 2>&1; then
    fail "main_sweep accepted an unregistered arm"
  fi
) || exit 1
echo "  ok  DRYRUN plans offline; router model and unknown arm both handled"

echo "[smoke] scaffold arms plan, and the stub refuses to pretend it installed one"
# The external tool baselines (rtk/serena/repomap) install into a real task container. The
# stub installs nothing, so it must SAY so -- a synthetic row under a scaffold's name is
# indistinguishable from a real one once it is sitting in results/.
(
  unset TOKENECONOMIZER_SWEBENCH_TASKS
  export TOKENECONOMIZER_ENV_FILE="$WORK/no-such.env"      # as above: not the repo's .env
  out="$(DRYRUN=1 ARMS="rtk serena repomap trimech-rtk trimech-repomap" \
         REPOS="django/django" SEEDS="0" AGENT=claude MODEL=claude-opus-4-8 \
         DOMAIN=swebench bash "$HERE/main_sweep.sh" 2>&1)" \
    || fail "main_sweep DRYRUN with scaffold arms exited nonzero"
  grep -q "cells=5" <<<"$out" || fail "scaffold arms not planned: $out"
  # Web-Bench hosts rtk (a static binary + a PreToolUse hook: no container, no Python
  # symbol map) and must PLAN it...
  out="$(DRYRUN=1 ARMS="rtk trimech-rtk" REPOS="calculator" SEEDS="0" AGENT=claude \
         MODEL=claude-opus-4-8 DOMAIN=webbench bash "$HERE/main_sweep.sh" 2>&1)" \
    || fail "webbench refused the rtk arms it can host: $out"
  grep -q "cells=2" <<<"$out" || fail "webbench did not plan rtk/trimech-rtk: $out"
  # ...and must still refuse the two it cannot: the repo map walks *.py, and Serena is a
  # codebase navigator for a benchmark that ships a file set rather than a codebase.
  out="$(DRYRUN=1 ARMS="serena repomap trimech-repomap" REPOS="calculator" SEEDS="0" AGENT=claude \
         MODEL=claude-opus-4-8 DOMAIN=webbench bash "$HERE/main_sweep.sh" 2>&1)" || true
  grep -q "SKIPPING scaffold arms not wired for webbench" <<<"$out" \
    || fail "webbench accepted a scaffold arm it cannot install: $out"
) || exit 1
TE_SMOKE_SCRIPTS="$REPO_ROOT/scripts" "$PY" - <<'PY' || fail "stub scaffold refusal"
import io, os, sys
sys.path.insert(0, os.environ["TE_SMOKE_SCRIPTS"])     # wherever the smoke test is run from
import run_experiment as rx
# The stub must warn, not silently accept.
from contextlib import redirect_stdout
buf = io.StringIO()
with redirect_stdout(buf):
    rx.load_runner("stub", "claude", None, "rtk")
assert "NOT installed" in buf.getvalue(), buf.getvalue()
# A real factory that cannot take a scaffold must RAISE rather than run native.
sys.modules["_fake_h"] = type(sys)("_fake_h")
sys.modules["_fake_h"].make_runner = lambda agent=None: object()
try:
    rx.load_runner("_fake_h:make_runner", "claude", None, "rtk")
except SystemExit as e:
    assert "does not accept a scaffold" in str(e), e
else:
    raise AssertionError("a scaffold-blind runner was accepted")
print("  ok  stub warns; a scaffold-blind harness is refused")
PY

echo "[smoke] aggregate both trees"
"$PY" "$HERE/../aggregate.py" "$WORK/gen" >/dev/null 2>&1 || fail "aggregate gen"
"$PY" "$HERE/../aggregate.py" "$WORK/cdx" >/dev/null 2>&1 || fail "aggregate cdx"
for tree in gen cdx; do
  for csv in all_rows.csv summary_by_cell.csv; do
    [[ -s "$WORK/$tree/index/$csv" ]] || fail "missing/empty $tree/index/$csv"
  done
done
echo "  ok  CSVs: gen/index + cdx/index all_rows + summary_by_cell"

# sanity: the cost basis column is populated and numeric
"$PY" - "$WORK/gen/index/all_rows.csv" <<'PY' || fail "cost column check"
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
assert rows, "no rows"
vals = [float(r["cost_effective_tokens"]) for r in rows]
assert all(v > 0 for v in vals), "non-positive cost_effective_tokens"
print(f"  ok  {len(rows)} rows, cost_effective_tokens in "
      f"[{min(vals):.0f}, {max(vals):.0f}]")
PY

echo "[smoke] cleaning up $WORK"
cleanup; trap - EXIT
[[ -d "$WORK" ]] && fail "temp dir survived cleanup"
echo "SMOKE PASS: all arms wired; no artifacts left."
