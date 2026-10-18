/**
 * TokenEconomizer for Pi (earendil-works/pi).
 *
 * Pi has no hook protocol -- extensions are functions on its event bus -- so this extension
 * speaks the one the Claude Code and Codex integrations use, on Pi's behalf: every event
 * below becomes one call of the shared dispatcher (`../scripts/te-hook --host pi`, JSON in,
 * JSON out), and the answer is applied with Pi's own levers. All of the mechanism logic lives
 * in the dispatcher; this file only translates.
 *
 *   session_start       -> SessionStart       the learned manual, kept for the session
 *   before_agent_start  -> UserPromptSubmit   opens a prompt-turn; the manual is set as the
 *                                             `tokeneconomizer` system-prompt section, on every
 *                                             prompt, because Pi diffs sections between runs and
 *                                             an omitted one would be recorded as removed
 *   tool_result         -> PostToolUse        the guard; a nudge is delivered as a steering
 *                                             message, a stop as ctx.abort()
 *   agent_end           -> Stop               closes the turn
 *   session_shutdown    -> SessionEnd         hands the session to a detached fold
 *
 * Like the hooks on the other agents it fails open: a dispatcher that is missing, slow or
 * broken answers "no change". It does nothing under TE_SIDECALL=1 (the plugin's own side
 * calls) or TE_DISABLE=1. `/te <command>` runs the `te` command line for this repository.
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = (() => {
  try {
    return dirname(fileURLToPath(import.meta.url));
  } catch {
    return __dirname;
  }
})();
const SCRIPTS = join(HERE, "..", "scripts");
const HOOK_TIMEOUT_MS = 60_000;
const SECTION = "tokeneconomizer";

const truthy = (v: string | undefined) => ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());

/** Run one of the plugin's launchers with `input` on stdin; resolve to its stdout. */
function run(script: string, args: string[], input: string, mode: string): Promise<string> {
  return new Promise((resolve) => {
    let out = "";
    let child;
    try {
      child = spawn(join(SCRIPTS, script), args, {
        env: { ...process.env, TE_PI_MODE: mode },
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      resolve("");
      return;
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve("");
    }, HOOK_TIMEOUT_MS);
    child.stdout.on("data", (d: Buffer) => (out += d.toString()));
    child.on("error", () => {
      clearTimeout(timer);
      resolve("");
    });
    child.on("close", () => {
      clearTimeout(timer);
      resolve(out);
    });
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

/** One dispatcher call; `{}` (no change) on any failure. */
async function hook(payload: Record<string, unknown>, mode: string): Promise<any> {
  if (truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE)) return {};
  const out = await run("te-hook", ["--host", "pi"], JSON.stringify(payload), mode);
  try {
    return JSON.parse(out.trim().split("\n").pop() || "{}") ?? {};
  } catch {
    return {};
  }
}

/** A notice in the UI; on stderr in print and JSON modes, which have no UI and keep stdout for
 * the reply and the event stream -- so a headless stop, too, says why it happened. */
function notify(ctx: ExtensionContext, text: unknown) {
  if (typeof text !== "string" || !text) return;
  if (ctx.hasUI) ctx.ui.notify(text, "info");
  else console.error(text);
}

const SOURCE: Record<string, string> = {
  // A new conversation starts empty; a resumed, forked or reloaded one already carries the
  // section it was given, in its own system-message history.
  startup: "startup",
  new: "clear",
  resume: "resume",
  fork: "resume",
  reload: "resume",
};

export default function tokeneconomizer(pi: ExtensionAPI) {
  let manual = "";
  let prompts = 0;
  let turn = "";

  const base = (ctx: ExtensionContext) => ({
    session_id: ctx.sessionManager.getSessionId(),
    transcript_path: ctx.sessionManager.getSessionFile() ?? "",
    cwd: ctx.cwd,
  });

  pi.on("session_start", async (event, ctx) => {
    prompts = 0;
    const out = await hook(
      { ...base(ctx), hook_event_name: "SessionStart", source: SOURCE[event.reason] ?? "startup" },
      ctx.mode,
    );
    manual = out?.hookSpecificOutput?.additionalContext ?? "";
    notify(ctx, out?.systemMessage);
  });

  pi.on("before_agent_start", async (event, ctx) => {
    prompts += 1;
    turn = `${ctx.sessionManager.getSessionId()}:${prompts}`;
    const out = await hook(
      { ...base(ctx), hook_event_name: "UserPromptSubmit", prompt_id: turn, prompt: event.prompt },
      ctx.mode,
    );
    // The repository map arrives on a session's first prompt; it joins the manual for the rest
    // of the session, since the section is set again on every prompt.
    const extra = out?.hookSpecificOutput?.additionalContext;
    if (extra) {
      manual = manual ? `${manual}\n\n${extra}` : extra;
    }
    notify(ctx, out?.systemMessage);
    if (manual) {
      event.systemPromptOptions.sections = { ...(event.systemPromptOptions.sections ?? {}), [SECTION]: manual };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const content = (event.content ?? [])
      .filter((c: any) => c?.type === "text")
      .map((c: any) => c.text)
      .join("\n");
    const structured = (event.structuredContent ?? {}) as Record<string, unknown>;
    const out = await hook(
      {
        ...base(ctx),
        hook_event_name: "PostToolUse",
        prompt_id: turn,
        tool_name: event.toolName,
        tool_use_id: event.toolCallId,
        tool_input: event.input ?? {},
        tool_response: { content, isError: event.isError, exit_code: structured.exit_code },
      },
      ctx.mode,
    );
    const nudge = out?.hookSpecificOutput?.additionalContext;
    if (nudge) {
      pi.sendMessage({ customType: SECTION, content: nudge, display: true }, { deliverAs: "steer" });
    }
    notify(ctx, out?.systemMessage);
    if (out?.continue === false) {
      notify(ctx, out?.stopReason);
      ctx.abort();
    }
  });

  pi.on("agent_end", async (_event, ctx) => {
    await hook({ ...base(ctx), hook_event_name: "Stop", prompt_id: turn, stop_hook_active: false }, ctx.mode);
  });

  pi.on("session_shutdown", async (event, ctx) => {
    await hook({ ...base(ctx), hook_event_name: "SessionEnd", reason: event.reason }, ctx.mode);
  });

  pi.registerCommand("te", {
    description: "TokenEconomizer: status | microskills | config (what it learned here, what it did)",
    handler: async (args, ctx) => {
      const argv = (args || "status").trim().split(/\s+/).filter(Boolean);
      const out = await run("te", ["--host", "pi", "--cwd", ctx.cwd, ...argv], "", ctx.mode);
      notify(ctx, out.trim() || "te: no output (is Python 3.10+ on PATH?)");
    },
  });
}
