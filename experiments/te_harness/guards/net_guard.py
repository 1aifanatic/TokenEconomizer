#!/usr/bin/env python3
"""PreToolUse guard: refuse Bash commands that fetch the benchmark's own answers.

Web-Bench is a PUBLIC repository. `bytedance/web-bench` ships, for every project, the exact
Playwright spec each task is graded by -- so an agent that can reach GitHub is not solving
the task, it is reading the answer key. Measured 2026-09-07 in development runs: three
Web-Bench tasks curled their own grading spec
(`raw.githubusercontent.com/bytedance/web-bench/main/projects/<p>/test/task-N.spec.js`) and
two of them then passed.

ALL THREE AGENTS SHARE THIS HOOK, and the payload spelling is no longer a guess. Codex's
`PreToolUse` contract was read out of the CLI's own embedded JSON schema
(`pre-tool-use.command.input` in the 0.153.1 binary): the required fields are `cwd`,
`hook_event_name`, `model`, `permission_mode`, `session_id`, `tool_input`, `tool_name`,
`tool_use_id`, `transcript_path`, `turn_id` -- i.e. `tool_name`/`tool_input`, Claude Code's
spelling exactly. So `hook_command` reads Codex payloads with the branch it already had.

ONE REFUSAL PROTOCOL, exit status 2 with the reason on stderr. Verified against all three
live CLIs -- Claude Code and Copilot on 2026-09-08, Codex on 2026-09-30.

CODEX CALLS IT TOO -- configured the way Codex reads hooks. Verified live on codex-cli 0.153.1
(2026-09-30, gpt-5.6-sol): with `{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks":
[{"type": "command", "command": "<python> net_guard.py"}]}]}}` in `$CODEX_HOME/hooks.json`
and `--dangerously-bypass-hook-trust` on the command line, Codex refused `echo
https://github.com/probe` before running it ("Command blocked by PreToolUse hook: Blocked:
...") and this script logged the refusal, while a control command ran. Its payload is
`tool_input: {"command": "echo https://github.com/probe"}` -- a string, read by the first
branch of `hook_command`.

The 2026-09-09 probes that concluded Codex ignores the hook got three things wrong, not the
CLI: they keyed the event kebab-case (`pre-tool-use`) where Codex wants PascalCase, gave the
handler an argv list where it wants a command string, and ran without the trust bypass --
and Codex skips every hook a person has not approved in /hooks, which an unattended run
cannot do. Rows stored before 2026-09-30 on the Codex lanes therefore carry
`net_guard_installed: false` and were guarded by the DNS layer below alone.

WHY A HOOK AND NOT A TOOL MASK. `WebSearch`/`WebFetch` are masked out separately, but the
agent simply used `curl` inside `Bash` -- and `Bash` is in `PROTECTED_TOOLS`, so it can
never be dropped. The only place left to intervene is ahead of the shell.

WHY THIS PATTERN AND NOTHING WIDER. Derived from the traces, not from taste. Originally:
across 774 stored transcripts the agent made 335 network calls, 226 package-manager and 109
loopback to its own dev server, NONE mentioning GitHub, while every abusive call did.

RE-DERIVED 2026-09-09 on the full corpus -- 3,538 stored run dirs, 558 network-shell calls:

    352  matched `github`   (63%)   every one of them reading a repository's source,
                                    history, PR diffs or `.patch` files
      0  false positives            no dependency install (pip 180, npx 14, yarn, npm)
                                    was blocked
      0  misses                     no call naming a `.patch`, a `/commit/`, a `/pull/`,
                                    a mirror (gitee/gitlab/codeload/jsdelivr) or a
                                    benchmark repo escaped the rule

So one case-insensitive substring still separates the two sets exactly, on a corpus 4.5x
larger than the one it was derived from. That is the whole argument for keeping it this
narrow: the alternative -- disabling web access outright -- also deletes the tool surface
that the untooling mechanism is measured on, and charges the baseline for a restriction the
cheat route never needed.

This is deliberately NOT bullet-proof: an IP literal, a redirect, a mirror or a base64'd
URL all walk past both layers. It defends the pattern that actually occurred, and the row
telemetry is what makes a future model routing around it visible -- `net_guard_blocks`
counts hook refusals, and `answer_key_attempts` counts answer-key-shaped commands found in
the transcript afterwards, which is agent-independent and therefore the number to watch on
any row whose `net_guard_installed` is false.
"""
import json
import os
import re
import sys

# One substring, covering github.com, raw.githubusercontent.com and api.github.com.
DENY = re.compile(r"github", re.I)

# THE SAME POLICY, ENFORCED WHERE THE AGENT CANNOT SPELL AROUND IT.
#
# The hook above is only as good as the CLI's cooperation, and each CLI cooperates on its own
# terms: a per-agent file, key and matcher, and on Codex a trust bypass as well. Each of those
# has failed silently at least once -- the Copilot lane's hook file sat in a directory its CLI
# never reads, and every Codex row before 2026-09-30 ran unhooked on a misread contract --
# while the provenance fields read healthy.
#
# So the primary control is DNS, installed into the task container's /etc/hosts. It needs no
# cooperation from any CLI, is identical on all three agents, and catches every route the
# corpus actually used -- curl, wget, `git clone`, `python -c "import urllib.request"` --
# because all of them resolve a hostname first. Verified in a real SWE-bench container:
# each of those fails with "Connection refused" afterwards, while pypi.org,
# files.pythonhosted.org and a real `pip download` still work.
#
# The host list is the `github` rule written out. Only hosts that appear in the measured
# abuse set, plus the sibling CDNs that serve the same content:
ANSWER_KEY_HOSTS = (
    "github.com", "www.github.com", "api.github.com", "codeload.github.com",
    "raw.githubusercontent.com", "objects.githubusercontent.com",
    "patch-diff.githubusercontent.com", "gist.githubusercontent.com",
)
# Marker so the block is idempotent and removable, and so a container can be inspected for
# it after the fact.
HOSTS_MARKER = "# TokenEconomizer answer-key guard"


def hosts_block():
    """The /etc/hosts stanza that black-holes the answer-key hosts.

    127.0.0.1 rather than 0.0.0.0: nothing in a task container listens on 443, so the
    connection is REFUSED immediately instead of hanging until a timeout. A refusal the
    agent sees at once is also the honest signal -- it reads as "this host is unreachable",
    which is true of the sealed environment the benchmark claims to be.
    """
    lines = [HOSTS_MARKER]
    for host in ANSWER_KEY_HOSTS:
        lines.append(f"127.0.0.1 {host}")
    return "\n".join(lines) + "\n"
# Where the run collects block records; the runner reads this into the row.
LOG_ENV = "TE_NET_GUARD_LOG"


def decide(command):
    """(blocked, reason). The whole policy, so a test can exercise it without a subprocess."""
    if not command or not DENY.search(command):
        return False, ""
    return True, ("Blocked: this benchmark's grading specs are public on GitHub, so fetching "
                  "from it would read the answer key rather than solve the task. Work from "
                  "the files in the workspace.")


def hook_command(payload):
    """The shell command out of a PreToolUse payload, in EITHER CLI's spelling.

    The two agents do not agree, and this is the one place that has to know:

      Claude Code   {"tool_name": "Bash", "tool_input": {"command": ...}}
      Copilot CLI   {"toolName": "bash", "toolArgs":   {"command": ...}}

    Captured off a live `preToolUse` hook on Copilot CLI 1.0.83, 2026-09-08.

    Reading only `tool_input` is the worst failure available here, which is why this is a
    named function with its own test rather than an inline `.get`: the hook installs
    successfully, the CLI runs it on every `bash` call, it finds no command, `decide("")`
    returns not-blocked and it exits 0. Every provenance field reads healthy, the block
    count is 0 -- indistinguishable from "armed and never fired" -- and nothing is guarded.
    That is exactly the failure `net_guard_installed` and this block count exist to make
    visible, so it must not be reintroduced by a key name.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("tool_input", "toolArgs", "input", "arguments", "params"):
        args = payload.get(key)
        if isinstance(args, dict) and args.get("command"):
            return _as_command(args["command"])
    if payload.get("command"):
        return _as_command(payload["command"])
    # Bounded recursive fallback for a payload whose spelling changes under us. Codex's was
    # captured off a live hook on 0.153.1 (`tool_input.command`, a string, read above), but
    # a CLI release can move it, and guessing the next key name would reproduce precisely
    # the failure this docstring warns about. Searching for a `command` field depends on no
    # key name at all. Depth-limited so a large payload cannot make the hook look hung.
    return _find_command(payload, depth=4)


def _as_command(value):
    """A command as a string, whether the payload spells it as one or as argv."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return ""


def _find_command(node, depth):
    """First `command` value at or below `node`, as a string. "" when there is none."""
    if depth < 0:
        return ""
    if isinstance(node, dict):
        got = _as_command(node.get("command"))
        if got:
            return got
        for value in node.values():
            got = _find_command(value, depth - 1)
            if got:
                return got
    elif isinstance(node, (list, tuple)):
        for value in node:
            got = _find_command(value, depth - 1)
            if got:
                return got
    return ""


def main():
    if os.environ.get("TE_ALLOW_NETWORK_TOOLS", "").strip() in ("1", "true", "yes"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                      # unreadable input must never wedge the agent
    command = hook_command(payload)
    blocked, reason = decide(command)
    if not blocked:
        return 0
    path = os.environ.get(LOG_ENV)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"command": command[:400]}) + "\n")
        except Exception:
            pass                      # logging is telemetry, never a reason to fail open
    # Exit 2 with the reason on stderr is the CLI's "block this call and show the model
    # why" contract.
    sys.stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
