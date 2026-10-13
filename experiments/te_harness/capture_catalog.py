"""Mine `claude_tool_catalog.json` from a REAL Claude Code run.

Why this exists: the economizer's tool judge needs `{tool_name: description}` for the tools
the agent loaded. The Copilot CLI dumps raw tool schemas into its debug log, so
`reflect.parse_tool_catalog` just reads them. Claude Code has no such log -- its
stream-json transcript carries the loaded tool NAMES but not their descriptions -- so
`reflect.parse_tool_catalog_claude` joins those names against a snapshot captured off the
wire. This module makes that snapshot.

Without it the judge is handed 25 lines of `- ToolName: ` with empty descriptions and drops
nothing, while still billing for the call.

The capture runs the agent EXACTLY as a task run does -- same image, same container, same
`--dangerously-skip-permissions --effort ...` invocation -- so the captured tool set is the
one those runs actually load, not the larger set a host session with MCP servers and skills
would send. The only addition is a tiny stdlib recorder running INSIDE the container:
Claude Code is pointed at `127.0.0.1`, the recorder copies the `tools` array off the
outbound request and passes the call on to api.anthropic.com with the real key, so the run
completes normally, bills the Anthropic API like any task run, and what is captured is what
the agent was sent.

(In-container rather than host-side because the container->host route is firewalled on
this machine; keeping the recorder inside also means the capture needs no host networking
at all.)

    python -m te_harness.capture_catalog

Env: ANTHROPIC_API_KEY; TE_CATALOG_OUT (default: the repo's vendor/claude_tool_catalog.json),
TE_CATALOG_PORT (default 8770), TE_CATALOG_DESC_CHARS (default 300 -- comfortably above
reflect's max_desc=200 default, keeps the committed file small).
"""
import json
import os
import subprocess
import sys
import time

from tokeneconomizer import vendor as _vendor
from .swebench import (_ANTHROPIC_DIRECT, _LIVE_CONTAINERS, _log, _run, claude_bin,
                       image_for)

# The vendored catalog the judge reads, inside the shared core:
# <repo>/tokeneconomizer/vendor/claude_tool_catalog.json.
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(_vendor.__file__)),
                           "claude_tool_catalog.json")
CAPTURE_INSTANCE = os.environ.get("TE_CATALOG_INSTANCE", "pydata__xarray-6461")

# Runs on the container's own python (3.10, stdlib only). UNION-merges the `tools` array
# across every request: different requests carry different subsets, and a deferred tool's
# schema only reaches the wire once ToolSearch has fetched it.
RECORDER_SRC = r'''
import json, os, sys, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, OUT, UPSTREAM = int(sys.argv[1]), sys.argv[2], sys.argv[3].rstrip("/")
KEY = os.environ.get("REAL_ANTHROPIC_KEY", "")
best = {"tools": {}, "n": 0}
lock = threading.Lock()

def offer(tools):
    got = {t["name"]: (t.get("description") or "").strip()
           for t in (tools or []) if isinstance(t, dict) and t.get("name")}
    with lock:
        best["n"] += 1
        changed = False
        for n, d in got.items():
            if len(d) > len(best["tools"].get(n, "")):
                best["tools"][n] = d
                changed = True
        if changed:
            json.dump(best["tools"], open(OUT, "w"))

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        try: offer(json.loads(body).get("tools"))
        except Exception: pass
        self.forward(body)
    def do_GET(self): self.forward(None)
    def forward(self, body):
        req = urllib.request.Request(UPSTREAM + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() in ("host","content-length","authorization","x-api-key",
                             "accept-encoding","connection"): continue
            req.add_header(k, v)
        req.add_header("x-api-key", KEY)
        req.add_header("anthropic-version",
                       self.headers.get("anthropic-version", "2023-06-01"))
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding","content-length","connection",
                                     "content-encoding"): continue
                    self.send_header(k, v)
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                while True:
                    c = r.read(4096)
                    if not c: break
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(c), c)); self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
        except urllib.error.HTTPError as e:
            p = e.read(); self.send_response(e.code)
            self.send_header("content-type","application/json")
            self.send_header("content-length", str(len(p))); self.end_headers()
            self.wfile.write(p)
        except Exception as e:
            p = json.dumps({"error": str(e)}).encode(); self.send_response(502)
            self.send_header("content-type","application/json")
            self.send_header("content-length", str(len(p))); self.end_headers()
            self.wfile.write(p)

ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
'''


def system_tools(stream_json_text):
    """Tool NAMES from a stream-json run's `system` init event."""
    for line in (stream_json_text or "").splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "system" and isinstance(ev.get("tools"), list):
            return ev["tools"]
    return []


def capture(instance_id=CAPTURE_INSTANCE, port=None, out_path=None, desc_chars=None,
            model=None, effort=None):
    """Run Claude Code once in a task container behind an in-container capture recorder.
    Returns the captured `{name: description}` and writes it to `out_path`."""
    port = int(port or os.environ.get("TE_CATALOG_PORT", "8770"))
    out_path = out_path or os.environ.get("TE_CATALOG_OUT", DEFAULT_OUT)
    desc_chars = int(desc_chars or os.environ.get("TE_CATALOG_DESC_CHARS", "300"))
    model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
    effort = effort or os.environ.get("TE_EFFORT_LEVEL", "high")
    # Must match what a task run uses (te_harness.swebench.run_task) -- see pass 1 below.
    turn_cap = int(os.environ.get("TE_MAX_TURNS", "80"))
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is unset (see the repo's .env)")

    name = "te-catalog-capture"
    _run(["docker", "rm", "-f", name])
    _LIVE_CONTAINERS.add(name)
    try:
        p = _run(["docker", "run", "-d", "--name", name, "--entrypoint", "bash",
                  "-v", f"{claude_bin()}:/usr/local/bin/claude:ro",
                  "-e", "HOME=/tmp/agent-home", "-w", "/testbed",
                  image_for(instance_id), "-lc", "sleep infinity"], timeout=300)
        if p.returncode != 0:
            raise SystemExit(f"docker run failed: {p.stderr[-400:]}")
        _run(["docker", "exec", name, "mkdir", "-p", "/tmp/agent-home"])

        # install + start the recorder (container python; no host networking involved)
        _run(["docker", "exec", name, "bash", "-lc",
              "cat > /tmp/capture_recorder.py <<'TE_EOF'\n" + RECORDER_SRC + "\nTE_EOF"])
        subprocess.Popen(
            ["docker", "exec", "-e", f"REAL_ANTHROPIC_KEY={key}", name, "bash", "-lc",
             f"python /tmp/capture_recorder.py {port} /tmp/tools.json {_ANTHROPIC_DIRECT} "
             f">/tmp/recorder.log 2>&1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):                       # wait for the listener
            if _run(["docker", "exec", name, "bash", "-lc",
                     f"curl -s -o /dev/null -m 2 http://127.0.0.1:{port}/ ; echo $?"]
                    ).stdout.strip() != "7":
                break
            time.sleep(1)

        def agent(prompt, max_turns, fmt="json"):
            """The agent, invoked exactly as a task run invokes it -- pointed at the
            in-container recorder, which swaps in the real key on the way out."""
            return subprocess.run(
                ["docker", "exec", "-i", "-w", "/testbed",
                 "-e", f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port}",
                 "-e", "ANTHROPIC_API_KEY=dummy",
                 "-e", "HOME=/tmp/agent-home", "-e", "IS_SANDBOX=1",
                 name, "claude", "-p", "--dangerously-skip-permissions",
                 "--model", model, "--effort", effort, "--max-turns", str(max_turns),
                 "--output-format", fmt] + (["--verbose"] if fmt == "stream-json" else []),
                input=prompt, capture_output=True, text=True, timeout=600)

        # What a REAL (first-party) task run loads -- the set the snapshot must cover.
        fp_probe = subprocess.run(
            ["docker", "exec", "-i", "-w", "/testbed",
             # name only -- docker takes the value from the client env, keeping the key
             # out of argv (and therefore out of every other user's `ps`)
             "-e", "ANTHROPIC_API_KEY", "-e", "HOME=/tmp/agent-home",
             "-e", "IS_SANDBOX=1",
             name, "claude", "-p", "--dangerously-skip-permissions",
             "--model", model, "--effort", effort, "--max-turns", str(turn_cap),
             "--output-format", "stream-json", "--verbose"],
            input="Reply with exactly: probe", capture_output=True, text=True, timeout=600)
        loaded_firstparty = system_tools(fp_probe.stdout)
        _log(f"first-party run loads {len(loaded_firstparty)} tools")

        def captured():
            raw = _run(["docker", "exec", name, "bash", "-lc",
                        "cat /tmp/tools.json 2>/dev/null || true"]).stdout.strip()
            return json.loads(raw) if raw else {}

        # Pass 1: a trivial prompt, but with the REAL turn cap. The CLI's tool set depends
        # on it -- at --max-turns 1 it offers agent-orchestration tools (Agent, Monitor,
        # ListAgents, ...) and withholds the Task*/ToolSearch family, which is the opposite
        # of what an 80-turn task run loads. Capturing under a different cap would snapshot
        # a catalog the real runs never see. The trivial prompt still ends after one turn,
        # so the cap costs nothing.
        proc = agent("Reply with exactly: catalog-capture", turn_cap, fmt="stream-json")
        loaded = system_tools(proc.stdout)

        # Pass 2: deferred tools are LISTED by name but their schemas only reach the wire
        # once ToolSearch fetches them -- so ask for exactly the ones still missing. Without
        # this they land in the snapshot with an empty description, which is the very hole
        # this module exists to close.
        missing = sorted(set(loaded) - set(captured()))
        if missing:
            _log(f"{len(missing)} deferred tool(s) not yet on the wire: {missing}")
            agent(f"Call ToolSearch once with query \"select:{','.join(missing)}\" "
                  f"and then reply DONE. Do nothing else.", turn_cap)
            still = sorted(set(loaded) - set(captured()))
            _log(f"after ToolSearch fetch, still missing: {still or 'none'}")

        # Residual: tools the recorder can never see. Pointing the CLI at ANY base URL --
        # including this local recorder -- changes which tools it offers: first-party features
        # (the Task* family, ToolSearch) drop out. Their schemas reach only the model's
        # context, never the transcript (a ToolSearch tool_result is empty by design), so
        # short of intercepting first-party TLS there is nowhere to read them from. They
        # stay in the catalog as names with no description, which the judge tolerates.
        merged = captured()
        gap = sorted(set(loaded_firstparty or []) - set(merged))
        if gap:
            _log(f"NOT captured (first-party-only, no readable source): {gap}")
        got = json.dumps(merged)
        if got in ("", "{}"):
            _log(f"agent exit={proc.returncode} stdout={proc.stdout[-300:]!r}")
            _log("recorder log: " + _run(["docker", "exec", name, "bash", "-lc",
                                          "tail -5 /tmp/recorder.log"]).stdout[-300:])
            raise SystemExit("captured no tool schemas")
        tools = json.loads(got)
    finally:
        _run(["docker", "rm", "-f", name])
        _LIVE_CONTAINERS.discard(name)

    prior = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as fh:
                prior = json.load(fh)
        except Exception:
            prior = {}
    merged_all = dict(prior)
    for n, d in tools.items():
        if len(d) > len(merged_all.get(n, "")):
            merged_all[n] = d
    added = sorted(set(merged_all) - set(prior))
    if prior:
        _log(f"merged with existing snapshot ({len(prior)} tools); new: {added or 'none'}")
    catalog = {n: d[:desc_chars] for n, d in sorted(merged_all.items())}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2, sort_keys=True)
    _log(f"wrote {len(catalog)} tool descriptions -> {out_path}")
    return catalog


if __name__ == "__main__":
    cat = capture()
    for n, d in cat.items():
        print(f"  {n:22} {d[:66]}")
    print(f"\n{len(cat)} tools captured", file=sys.stderr)
