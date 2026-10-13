#!/usr/bin/env bash
# One CPU budget for EVERY lane on this host, enforced where the lanes actually meet.
#
# Why this exists. `TE_SWEBENCH_MAX_CPUS` caps ONE task container (default 16 cores, plus
# the matching OMP/MKL/joblib thread caps -- see te_harness.swebench._cpu_limit_args). It is
# per-container by construction, so N concurrent lanes are an N x cap ceiling: six lanes at
# the default would reserve the right to 96 cores of a shared box, and the agent's own
# verification step really does try to take them (measured 2026-09-05: one agent-issued
# `pytest -k` on scikit-learn held 249 cores for 183 s).
#
# Under rootless Docker every container scope is a child of ONE cgroup in the user's own
# systemd manager, and cgroup v2 delegates `cpu` there. Writing `cpu.max` on that parent
# caps the whole subtree at once -- every lane already running, and every container any lane
# starts later, with no restart and nothing else in the cgroup to disturb (it holds
# container scopes only; the harness processes themselves live in app.slice and are
# network-bound anyway). Within the budget the kernel is work-conserving: an idle lane's
# share is available to a busy one, which is what makes a fleet cap better than dividing the
# cores between lanes up front.
#
# This is a CEILING, not a reservation. It throttles a burst; it cannot make a lane use
# more. Two consequences worth knowing before lowering it much further: a heavily throttled
# test run takes longer in wall-clock, and TE_TASK_TIMEOUT / TE_EVAL_TIMEOUT are wall-clock,
# so a cap tight enough to matter can in principle cost a resolve.
#
# Usage:
#   bash scripts/launch/fleet_budget.sh            # report the current budget and usage
#   bash scripts/launch/fleet_budget.sh 16         # cap every lane's containers at 16 cores
#   . scripts/launch/fleet_budget.sh; te_fleet_cpu_cap 16     # from a launcher
#
# Env:
#   TE_FLEET_CPUS    cores for the whole fleet; 0 or "none" = leave the cgroup alone
#   TE_CGROUP_ROOT   cgroup2 mount point (default /sys/fs/cgroup; overridden by the tests)

# The cgroup rootless dockerd puts container scopes under. Located rather than assumed: a
# live container's own scope names it, and the canonical path is only the fallback.
te_fleet_cgroup() {
    local root="${TE_CGROUP_ROOT:-/sys/fs/cgroup}" uid scope
    uid="$(id -u)"
    scope="$(find "$root/user.slice/user-$uid.slice/user@$uid.service" -maxdepth 3 \
             -name 'docker-*.scope' -print -quit 2>/dev/null)"
    if [[ -n "$scope" ]]; then
        dirname "$scope"
        return 0
    fi
    local canonical="$root/user.slice/user-$uid.slice/user@$uid.service/user.slice"
    [[ -d "$canonical" ]] && { printf '%s' "$canonical"; return 0; }
    return 1
}

# te_fleet_cpu_cap <cores> -- cap every TE container, collectively, at <cores>.
# Never fails a launch: an un-delegated or read-only cgroup is a warning, not an exit.
te_fleet_cpu_cap() {
    local cores="${1:-${TE_FLEET_CPUS:-16}}" cg quota
    case "$cores" in
        0|none|"") echo "[fleet] CPU budget: not enforced (TE_FLEET_CPUS=$cores)"; return 0 ;;
    esac
    cg="$(te_fleet_cgroup)" || {
        echo "[fleet] !! no rootless-docker cgroup found -- the ${cores}-core fleet cap is NOT in force" >&2
        return 0; }
    if [[ ! -w "$cg/cpu.max" ]]; then
        echo "[fleet] !! $cg/cpu.max is not writable -- the ${cores}-core fleet cap is NOT in force" >&2
        echo "[fleet] !! (cgroup v2 must delegate the cpu controller to the user manager)" >&2
        return 0
    fi
    # cgroup v2 spells a cap as "<quota_us> <period_us>"; 100000 us is the conventional period.
    quota=$(( cores * 100000 ))
    if printf '%s 100000\n' "$quota" > "$cg/cpu.max" 2>/dev/null; then
        echo "[fleet] CPU budget: every TE container, collectively, capped at ${cores} cores"
        echo "[fleet]   cgroup: $cg"
    else
        echo "[fleet] !! could not write $cg/cpu.max -- the ${cores}-core fleet cap is NOT in force" >&2
    fi
}

# What the budget is right now, and what the fleet is actually drawing against it.
te_fleet_cpu_report() {
    local cg cur n
    cg="$(te_fleet_cgroup)" || { echo "[fleet] no rootless-docker cgroup on this host"; return 0; }
    cur="$(cat "$cg/cpu.max" 2>/dev/null || echo '?')"
    case "$cur" in
        "max "*) echo "[fleet] CPU budget: UNCAPPED ($cg/cpu.max = $cur)" ;;
        *) echo "[fleet] CPU budget: $(( ${cur%% *} / 100000 )) cores ($cg/cpu.max = $cur)" ;;
    esac
    if command -v docker >/dev/null 2>&1; then
        n="$(docker ps -q 2>/dev/null | wc -l)"
        echo "[fleet] live task containers: $n"
        [[ "$n" -gt 0 ]] && docker stats --no-stream --format '{{.CPUPerc}}' 2>/dev/null \
            | tr -d '%' | awk '{s+=$1} END {printf "[fleet] drawing now: %.1f cores\n", s/100}'
    fi
    echo "[fleet] throttling since boot: $(awk '/nr_throttled|throttled_usec/ {printf "%s=%s ", $1, $2}' \
         "$cg/cpu.stat" 2>/dev/null)"
}

# Standalone: `bash fleet_budget.sh [cores]`. Sourced: defines the two functions only.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    [[ $# -gt 0 ]] && te_fleet_cpu_cap "$1"
    te_fleet_cpu_report
fi
