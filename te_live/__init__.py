"""The live harness: the three mechanisms inside a real coding-agent session, through the hooks
the agent already exposes.

A sibling of `te_harness`, not a submodule of the core, for the same reason: it reaches an agent
CLI (`claude -p`, `codex exec`) for its opt-in side calls and catalog probe, and
`import tokeneconomizer` must not be able to. `te_harness` runs the controller over benchmark
streams in Docker; `te_live` runs it inside the user's own sessions. The core never learns which.

The research driver (`experiments/scripts/run_experiment.py`) holds one `Economizer` in one process for a
whole stream of benchmark tasks, launches the sealed agent itself, and applies each decision at
launch: a prompt prefix, `--disallowed-tools`, a kill-and-resume on the live JSON stream. A
plugin has none of that. The user launches the agent, and the controller runs only when the agent
calls one of its hooks -- each call a fresh, short-lived process. This package turns the
stream-shaped core into that event-shaped one:

  * state       -- per-repository, per-host state on disk (`paths`, `store`, `config`), so what
                   is learned survives between hook processes, sessions and plugin updates;
  * hosts       -- one adapter per agent (`hosts`, `transcripts`): hook payloads in, hook
                   outputs out, and the agent's own session log turned into a trace the existing
                   `trace` seam already reads;
  * mechanisms  -- `fold` runs the SAME classes the experiments ran (`PrefixStage`,
                   `ToolingStage`) over each finished session, and `guard` replays
                   `vendor.spiral.NudgeGuard`'s control flow one tool call at a time;
  * entry points -- `hook.main` (what the plugin's hooks execute) and `cli.main` (`te`).

What changes relative to the measured setting is written down where it changes, because it is
the difference between a mechanism that ports and a number that does not:

  * there is no grader, so the spiral envelope learns from a "turn ended green" proxy;
  * a "task" is a session for injection and untooling, but a prompt-turn for the spiral guard --
    in interactive use the human already segments work into short turns;
  * tool removal is a deny list the user approves, applied from the next session, never a
    mid-session change (that would also invalidate the prompt cache);
  * a nudge is a mid-turn reminder attached to a tool result, not a resumed segment, so its
    effect has to be re-measured rather than inherited from the published rows.

See docs/PLUGIN.md. Stdlib only, like the rest of the core: a hook must start in tens of
milliseconds and must not depend on anything the user did not install.
"""
