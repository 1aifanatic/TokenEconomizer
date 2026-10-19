"""Shared OpenAI Codex CLI support for the te_harness runners.

Why a separate module, exactly as `te_harness.copilot` is one: nothing here is shared
with Claude Code, and every piece of it is a place where a wrong guess produces a tree of
plausible rows instead of an error.

  1. **Host install resolution.** The `codex` on PATH is a small Node shim that execs a
     platform-specific native binary. That binary is a self-contained static-PIE ELF (no
     Node, no dynamic libs), so the task container gets the vendor triple dir bind-mounted
     read-only and invokes the ELF directly -- nothing is installed in the image, the same
     posture as the bind-mounted `claude` binary.
  2. **Provider config is a FILE, not environment.** Codex reads its provider and base URL
     from `$CODEX_HOME/config.toml`, so the endpoint cannot be expressed as env vars.
     `config_toml` builds that file, naming the OpenAI API the lane bills directly.
  3. **A different usage schema.** `codex exec --json` reports usage on `turn.completed`
     with `input_tokens` as the GRAND input total -- it already INCLUDES
     `cached_input_tokens` and `cache_write_input_tokens`. Subtracting both is what makes
     fresh input actually fresh; skipping that step prices ~90% cached re-reads at 1.0x and
     every cost number downstream is wrong rather than missing. See `row_usage`.
  4. **A capability mask, not a tool mask.** Codex has no `--disallowed-tools`. The
     Codex-native untooling mechanism (`tokeneconomizer.toolprune.codex`) emits
     logical CAPABILITY packages and leaves the mapping onto concrete Codex config toggles
     to the runner, because the runner is what owns the request contract. `config_toggles`
     is that mapping.

THE TOGGLE NAMES ARE VERSION-SPECIFIC, and this is the failure this module works hardest to
prevent. Verified against the installed codex-cli 0.153.1 with `codex exec --strict-config`
(which errors on an unrecognized configuration field), two toggles carried over from the
AgentEO study's codex-cli 0.146 driver are NO LONGER ACCEPTED:

    web_search_mode = "disabled"      -> unknown configuration field   (now: web_search = "disabled")
    [tools] view_image = false        -> unknown configuration field   (now: [features] view_image = false)

A renamed key does not fail: Codex ignores unknown fields unless `--strict-config` is
passed, so the pruning silently becomes a no-op and the economized arm runs as a baseline
wearing its name. That is why `validate_config` runs the real CLI over the generated file at
CONSTRUCTION time and refuses the lane rather than discovering it per task.

One limit worth knowing about that gate: `[[hooks.pre_tool_use]]` is NOT strictly
validated (a bogus field inside it is accepted), so `--strict-config` proves the pruning
toggles are real but proves nothing about the net guard. The guard is verified at RUNTIME
instead, the same way the Claude and Copilot lanes verify theirs.

Stdlib only, like the rest of the harness.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from tokeneconomizer import pricing

# What `model_reasoning_effort` accepts. Validated by the runner at construction so a typo
# fails before the run rather than silently falling back to the CLI default -- effort is
# part of the cost basis, so a silent default makes the run unreproducible.
EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})

# Codex's own work surface. Without a shell and an edit path there is no task, so no
# capability mask may ever remove these: a `resolved: false` caused by a missing
# `apply_patch` is a harness artifact, not a mechanism result. These are CAPABILITY names
# rather than tool names because that is the granularity Codex config operates at.
PROTECTED_CAPABILITIES = frozenset({
    "deferred_wait",      # CORE in codex_untool: pruning it provoked shell-polling
    "apply_patch", "exec_command", "planning", "write_stdin",
})

# Tool names the shared lifecycle treats as unmaskable. Codex is masked by capability, not
# by tool, so this exists only to satisfy the base runner's `protected_tools` slot and to
# document what the mask must never reach.
PROTECTED_TOOLS = frozenset({"shell", "apply_patch"})

# In-container paths. The vendor dir is mounted read-only; CODEX_HOME must be writable
# because Codex writes session rollouts there (and the rollout is where the resolved model
# id is recorded, which is what prices an unpinned run).
C_VENDOR = "/opt/codex-vendor"
C_BIN = C_VENDOR + "/bin/codex"
# NOT under /tmp, and that is load-bearing rather than tidiness. With CODEX_HOME on a
# temporary dir the CLI refuses to install its PATH helper binaries and says so on every
# invocation ("Refusing to create helper binaries under temporary dir"), observed on 0.153.1
# -- so the agent runs without the helpers Codex expects to put on its PATH. The container's
# HOME stays /tmp/agent-home (the scaffold profile's home, shared with the Claude lane);
# only CODEX_HOME moves.
C_HOME = "/opt/codex-home"

# The OpenAI Platform's global Responses endpoint. Only a DEFAULT: a region-scoped key
# 401s here and must set OPENAI_BASE_URL to its own region.
OPENAI_DIRECT = "https://api.openai.com/v1"

# The one credential the Codex lane bills on. config.toml names it (`env_key`) and the
# container is handed it BY NAME, so the value never reaches the file or argv.
KEY_ENV = "OPENAI_API_KEY"

_INSTALL_HINT = (
    "\n  install:  npm i -g @openai/codex   (needs Node on the host)"
    "\n  override: TE_CODEX_BIN (the node shim) / TE_CODEX_VENDOR (the vendor triple dir)")


# --------------------------------------------------------------------------------------
# host install
# --------------------------------------------------------------------------------------
def codex_bin():
    """Host `codex` entry point (the Node shim), for version reporting only."""
    p = os.environ.get("TE_CODEX_BIN") or shutil.which("codex")
    if not p:
        raise RuntimeError("no codex binary found on PATH" + _INSTALL_HINT)
    return p


def codex_vendor(binary=None):
    """The host vendor triple dir holding the static-PIE ELF the container runs.

    Resolved from the Node shim's install tree rather than hardcoded: the AgentEO driver
    pinned an absolute path under one machine's nvm prefix, which is exactly the kind of
    machine-specific path the code must not carry. TE_CODEX_VENDOR overrides.

    Raises when the ELF is absent. Docker invents an EMPTY directory for a missing bind
    mount source rather than failing, so an unchecked bad path here becomes "codex: not
    found" inside every container -- a whole tree of 0-turn rows behind a green launch.
    """
    v = os.environ.get("TE_CODEX_VENDOR")
    if not v:
        shim = os.path.realpath(binary or codex_bin())
        # <prefix>/lib/node_modules/@openai/codex/bin/codex.js
        #   -> <prefix>/lib/node_modules/@openai/codex/node_modules/@openai/codex-<triple>/vendor/<triple>
        pkg = os.path.dirname(os.path.dirname(shim))
        base = os.path.join(pkg, "node_modules", "@openai")
        try:
            plats = sorted(d for d in os.listdir(base) if d.startswith("codex-"))
        except OSError:
            plats = []
        for plat in plats:
            vend = os.path.join(base, plat, "vendor")
            try:
                triples = sorted(os.listdir(vend))
            except OSError:
                continue
            for triple in triples:
                if os.path.isfile(os.path.join(vend, triple, "bin", "codex")):
                    v = os.path.join(vend, triple)
                    break
            if v:
                break
    if not v or not os.path.isfile(os.path.join(v, "bin", "codex")):
        raise RuntimeError(
            f"no codex native binary under vendor dir {v!r} -- a bind mount of a missing "
            "path becomes an EMPTY dir in the container, so every task would fail with "
            "'codex: not found' behind a green launch banner." + _INSTALL_HINT)
    return v


def require_install():
    """(shim, vendor_dir) with both proven present. Called at runner construction."""
    binary = codex_bin()
    return binary, codex_vendor(binary)


def cli_version(binary):
    """`codex --version`, so a run records the exact CLI build that produced it.

    Recorded rather than decorative: the accepted config-toggle names moved between 0.146
    and 0.153 (see the module docstring), so the build is part of what a pruning arm means.
    """
    try:
        p = subprocess.run([binary, "--version"], capture_output=True, text=True,
                           timeout=60)
        return (p.stdout or p.stderr or "").strip().splitlines()[0][:80]
    except Exception:
        return ""


def container_mounts(vendor):
    """host->container `-v` specs. Only the vendor dir: the ELF finds its siblings
    (`rg`, the sandbox helpers) relative to itself, so one read-only mount is the whole
    install."""
    return ["-v", f"{vendor}:{C_VENDOR}:ro"]


# --------------------------------------------------------------------------------------
# the endpoint: the OpenAI API, directly
# --------------------------------------------------------------------------------------
def resolve_codex_routing(env=None):
    """The OpenAI endpoint and key this run bills, decided from the environment alone.

    Returns {base_url, api_key}. `base_url` is OPENAI_BASE_URL when set -- OpenAI's own
    regional endpoint, which a region-scoped key needs -- else the global one; it is what
    config.toml names and what the mechanism calls go to. `api_key` is OPENAI_API_KEY.

    Raises when the key is unset -- at construction, rather than once per task.
    """
    env = os.environ if env is None else env
    key = env.get(KEY_ENV, "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is unset, so this Codex lane has nothing to bill -- see the "
            "repo's .env. (A region-scoped key also needs OPENAI_BASE_URL: it 401s on the "
            "global endpoint.)")
    base = (env.get("OPENAI_BASE_URL") or OPENAI_DIRECT).strip().rstrip("/")
    return {"base_url": base, "api_key": key}


def validate_priceable(model, agent):
    """Fail at construction if the billed id has no weight entry.

    Same early check the Claude and Copilot runners make: `pricing.effective_tokens`
    otherwise raises at the END of run_task, after the rollout has been paid for and
    graded.
    """
    pricing.weights_for({"model": model, "agent": agent})


# --------------------------------------------------------------------------------------
# catalog preflight
# --------------------------------------------------------------------------------------
_CATALOG = {}


def fetch_catalog(base_url, api_key, timeout=10):
    """frozenset of served model ids at `base_url`, or None when unreadable.

    Cached per (base_url, key): one lane asks once, not once per task.
    """
    key = (base_url, api_key)
    if key in _CATALOG:
        return _CATALOG[key]
    out = None
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                     headers={"authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode())
        # The isinstance guards keep a payload that is not an object at all from raising
        # AttributeError, which the except clause below would NOT catch.
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            ids = frozenset(m["id"] for m in data
                            if isinstance(m, dict) and isinstance(m.get("id"), str))
            out = ids or None
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        out = None
    _CATALOG[key] = out
    return out


def verify_model(model, routing):
    """Refuse a lane whose endpoint does not serve the id it intends to bill.

    An unserved id is a per-call FAILURE, not a fallback: every task would run, bill and
    fail while the launcher's banner stayed green.

    A catalog that cannot be read is NOT treated as a refusal -- a network hiccup at
    construction must not refuse a lane -- but it is reported so the caller can log it,
    because "could not check" and "checked and absent" must not look the same in the
    record.
    """
    served = fetch_catalog(routing["base_url"], routing["api_key"])
    if served is None:
        return None, "catalog unreadable -- model not verified"
    if model in served:
        return True, ""
    sample = " ".join(sorted(served)[:12])
    return False, (
        f"{routing['base_url']} does not serve {model!r}. An unserved id fails per task "
        f"rather than at startup, so this lane is refused. Served (first 12): {sample}")


# --------------------------------------------------------------------------------------
# the request contract: config.toml
# --------------------------------------------------------------------------------------
# Capability package -> the concrete Codex config toggles that remove it.
#
# EVERY key below was probed against the installed CLI with `codex exec --strict-config`,
# which reports an unrecognized configuration field as an error. Two keys the AgentEO
# 0.146 driver used are rejected by 0.153.1 and are NOT here:
#     web_search_mode = "disabled"   ->  now top-level  web_search = "disabled"
#     [tools] view_image = false     ->  now  [features] view_image = false
# Both were silently ignored by the newer CLI, which is the whole hazard: the pruning
# becomes a no-op and the arm measures itself as useless while still being called
# economized. `validate_config` is the gate that keeps this honest across CLI upgrades.
#
# Only the packages `codex_untool.CONFIG_CAPABILITIES` nominates appear here. The rest of
# the mask is relay-scoped and is applied by the transcript-side controller, not by config
# -- mapping a capability with no supported toggle would be a lie in run_config.json.
# `goals` (2026-09-11): `[features] goals = false` is accepted by 0.153.1 and removes the
# product-goal tool docs from the code-mode exec description. 0.153.1 REJECTS a toggle for
# the plan tool, for skills and for request_user_input, so none of those is here.
# `skills` (2026-09-11): the CLI installs its bundled "system skills" into
# $CODEX_HOME/skills/.system at startup and lists every enabled one in a
# <skills_instructions> developer message. There is no switch for
# the listing itself (`[features] skill_search`, `skip_host_skill_discovery` and `[skills]
# enabled` leave it intact or are rejected), but a per-skill `[[skills.config]] enabled =
# false` is accepted and, applied to every bundled skill, removes the whole block. The bundled
# set is discovered from the installed CLI at runner construction (`bundled_system_skills`);
# `DEFAULT_SYSTEM_SKILLS` is the 0.153.1 set, used only when discovery fails.
_TOP_LEVEL_TOGGLES = {
    "collaboration": ("include_collaboration_mode_instructions = false",),
    "web": ('web_search = "disabled"',),
    "goals": (),
    "skills": (),
    "image_media": (),
    "interactive_guidance": (),
}
_TABLE_TOGGLES = {
    "collaboration": (("agents", "enabled = false"),
                      ("features", "multi_agent = false")),
    # Only the TOP-LEVEL `web_search = "disabled"` (in _TOP_LEVEL_TOGGLES) remains.
    # codex-cli 0.153.1 emits a deprecation ERROR ITEM into the transcript for
    # `[features].web_search` -- "deprecated because web search is enabled by default; set
    # `web_search` to live/indexed/cached/disabled at the top level" -- observed on a live
    # preflight 2026-09-10. An error item per task is trace noise the mechanisms then read,
    # and a deprecation becomes a hard failure sooner or later, so the redundant table
    # entries are dropped in favour of the spelling the CLI itself names.
    "web": (),
    "goals": (("features", "goals = false"),),
    "skills": (),
    "image_media": (("features", "view_image = false"),),
    "interactive_guidance": (),
}
CONFIG_CAPABILITIES = frozenset(_TABLE_TOGGLES)

# `interactive_guidance` (2026-09-11): part of the base prompt is guidance for an
# interactive chat -- persona, writing style, the commentary/final channels, how to handle
# a user message arriving mid-turn, answer formatting, visualizations. A detached
# `codex exec` run has no user reading commentary and its final message is not graded.
# The CLI accepts
# `model_instructions_file`, which REPLACES the base prompt, so the package is removed by
# writing a lean copy of the base prompt with those sections cut and every operational
# section (rules for getting work done, file editing, autonomy, destructive actions) kept
# byte-for-byte. The base prompt is version-specific: it is read from
# data/codex_base_prompt/<cli version>.md, captured by recording the CLI's own request, and
# the package is simply unavailable -- left loaded, logged -- on a CLI build without a
# capture, so an upgrade can never apply a stale prompt. The skills how-to sections are
# cut by the same file whenever `skills` is pruned: they describe a listing that is no
# longer there.
INSTRUCTIONS_PATH = C_HOME + "/instructions.md"
INSTRUCTION_SECTIONS = {
    "interactive_guidance": (
        "# Personality", "## Writing style", "## Technical communication",
        "# Working with the user", "## Intermediate commentary", "## Final answer",
        "### Formatting rules", "### Visualizations"),
    "skills": ("# Using skills", "### How to use skills"),
}
# Kept even inside a dropped section, because both paragraphs are operational rather than
# presentational: what to do after a context compaction, and -- the only statement anywhere
# in the prompt of how a turn ENDS -- that the agent yields by sending a final message.
# Dropping the latter removes the agent's stopping rule, and an agent with no stated way
# to end a turn spends extra model calls finding one -- so both stay, however short the
# rest of the lean prompt gets.
_KEEP_PARAGRAPH_RE = re.compile(r"When you run out of context|end your turn", re.I)
_SECTION_RE = re.compile(r"(?m)^(?=#{1,6} )")


def base_prompt_for(cli_version):
    """The captured base prompt for this CLI build, or None when there is no capture."""
    m = re.search(r"(\d+\.\d+\.\d+)", str(cli_version or ""))
    if not m:
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                        "codex_base_prompt", m.group(1) + ".md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def lean_instructions(base_text, capabilities):
    """The base prompt minus the sections the pruned packages make inapplicable.

    Returns None when no section is dropped (no instructions file is written and the CLI
    keeps its built-in prompt). Everything not dropped is kept byte-for-byte."""
    drop = set()
    for cap in capabilities or ():
        drop.update(INSTRUCTION_SECTIONS.get(cap, ()))
    if not base_text or not drop:
        return None
    out, dropped = [], 0
    for section in _SECTION_RE.split(base_text):
        header = section.split("\n", 1)[0].strip()
        if header in drop:
            dropped += 1
            kept = [p.strip() for p in section.split("\n\n") if _KEEP_PARAGRAPH_RE.search(p)]
            if kept:
                out.append("\n\n".join(kept) + "\n\n")
            continue
        out.append(section)
    if not dropped:
        return None
    return "".join(out).rstrip() + "\n"

DEFAULT_SYSTEM_SKILLS = ("imagegen", "openai-docs", "plugin-creator", "review-agent",
                         "skill-creator", "skill-installer")


def skills_config_entries(system_skills=None):
    """`[[skills.config]]` blocks that disable every bundled system skill, in-container
    paths. One block per skill; a path the CLI does not have is harmless."""
    names = tuple(system_skills) if system_skills else DEFAULT_SYSTEM_SKILLS
    return [f'[[skills.config]]\npath = {json.dumps(f"{C_HOME}/skills/.system/{n}/SKILL.md")}\n'
            f'enabled = false\n' for n in names]


def bundled_system_skills(binary=None, home=None, timeout=45):
    """The system skills the installed CLI writes into a fresh CODEX_HOME, by name.

    One offline `codex exec` against a throwaway CODEX_HOME whose provider points at a dead
    port. The CLI installs its bundled skills (and drops a `.codex-system-skills.marker`)
    before it touches the network; a dead port then leaves it "waiting for network"
    indefinitely, so the process is killed as soon as the marker appears rather than waited
    for. Returns [] when the CLI cannot be run or the marker never shows, so callers fall
    back to DEFAULT_SYSTEM_SKILLS and say so. Nothing is billed."""
    binary = binary or codex_bin()
    # mkdtemp + an explicit RETRYING cleanup, not TemporaryDirectory. The killed CLI (or a
    # child it spawned) can still be writing under CODEX_HOME when we come to remove it, and
    # a "Directory not empty" here failed the offline suite about one run in four.
    # `ignore_cleanup_errors=True` also makes that non-fatal, but it gives up on the first
    # error and LEAVES the directory: 899 MB of throwaway CODEX_HOMEs had accumulated in
    # /tmp on this host by 2026-09-13. `_rmtree_quietly` retries the transient case first
    # and only then ignores, so the common case is actually cleaned up.
    tmp = tempfile.mkdtemp(dir=home)
    try:
        with open(os.path.join(tmp, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('model = "gpt-5.6-terra"\nmodel_provider = "dead"\n'
                     '[model_providers.dead]\nname = "OpenAI"\n'
                     'base_url = "http://127.0.0.1:9/v1"\nenv_key = "TE_DEAD_KEY"\n'
                     'wire_api = "responses"\nrequest_max_retries = 0\n')
        env = dict(os.environ, CODEX_HOME=tmp, TE_DEAD_KEY="dead")
        root = os.path.join(tmp, "skills", ".system")
        marker = os.path.join(root, ".codex-system-skills.marker")
        try:
            proc = subprocess.Popen(
                [binary, "exec", "--skip-git-repo-check", "-C", tmp, "probe"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        except OSError:
            return []
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and proc.poll() is None:
                if os.path.exists(marker):
                    break
                time.sleep(0.2)
        finally:
            _kill_process_group(proc)
        if not os.path.isdir(root):
            return []
        return sorted(n for n in os.listdir(root)
                      if os.path.isfile(os.path.join(root, n, "SKILL.md")))
    finally:
        _rmtree_quietly(tmp)


def _kill_process_group(proc):
    """Kill everything a probe started -- not just the process `Popen` returned.

    `codex` on PATH is the npm package's Node shim, which execs the native ELF as a CHILD.
    `proc.kill()` reached only the shim: the native binary, pointed at a dead port, was
    reparented to init and sat "waiting for network" for good. Every offline suite run
    leaked one (six were alive on this host on 2026-09-30, the oldest five days old).
    The probes therefore start in their own session, and the whole group goes -- also when
    the shim has already exited, because its child can outlive it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _rmtree_quietly(path):
    for _ in range(3):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(path, ignore_errors=True)


# Dropped on EVERY arm, baseline included, and not part of any mechanism's saving.
#
# The answers to both benchmarks are a fetch away (a SWE-bench instance's own fix commit is
# public), so the Claude and Copilot lanes drop WebSearch/WebFetch on every arm on top of
# whatever untooling decided -- swebench.network_tool_block. Codex gets the same treatment
# through the only seam it has, its config.
#
# STATE THE CONSEQUENCE, because it changes what a Codex untooling number means: with web
# search off in the baseline too, the `web` package is ALREADY absent from the contract when
# the mask prunes it, so `web` contributes no saving on this benchmark and the measured
# effect comes from the collaboration and image/media packages and the relay-scoped
# capabilities. TE_ALLOW_NETWORK_TOOLS=1 lifts it for a run that genuinely wants a browsing
# agent, exactly as it does on the other two lanes.
def network_block_toggles():
    """Contamination-control toggles applied to every arm, or () when lifted."""
    if os.environ.get("TE_ALLOW_NETWORK_TOOLS") == "1":
        return (), ()
    return _TOP_LEVEL_TOGGLES["web"], _TABLE_TOGGLES["web"]


def config_toggles(capabilities):
    """(top_level_lines, table_lines) for a frozen capability mask.

    `capabilities` is the logical mask the mechanism froze for this task. A package with no
    supported config toggle contributes nothing here rather than a guess.
    """
    # Checked against the RAW mask, before the CONFIG_CAPABILITIES filter. Filtering first
    # made this guard dead code -- a protected package is not in CONFIG_CAPABILITIES, so it
    # was silently dropped and the refusal never fired. The point is to reject a mask that
    # ASKS for something protected, not to quietly honour the part of it that is safe.
    bad = PROTECTED_CAPABILITIES.intersection(capabilities or ())
    if bad:
        # The mechanism asserts this on its own side too; assert it here as well because
        # this is the layer that would actually remove the agent's ability to work.
        raise RuntimeError(
            f"refusing to prune protected Codex capabilities {sorted(bad)}: without an "
            "edit path and a shell there is no task, and the resulting 'resolved: false' "
            "would be a harness artifact rather than a mechanism result")
    caps = [c for c in (capabilities or ()) if c in CONFIG_CAPABILITIES]
    top, tables = [], []
    for cap in sorted(caps):
        top.extend(_TOP_LEVEL_TOGGLES.get(cap, ()))
        tables.extend(_TABLE_TOGGLES.get(cap, ()))
    net_top, net_tables = network_block_toggles()
    top.extend(net_top)
    tables.extend(net_tables)
    # De-duplicate while keeping a stable order: `web` can arrive from both the mask and
    # the unconditional network block, and Codex rejects a duplicated key outright.
    return _dedupe(top), _dedupe(tables)


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def config_toml(*, routing, model, effort, project="/testbed", capabilities=(),
                system_skills=None, instructions=None, instructions_path=None,
                mcp_servers=None):
    """The `$CODEX_HOME/config.toml` this run's contract is expressed in.

    One provider block, `[model_providers.openai_direct]`, at the endpoint
    `resolve_codex_routing` resolved, reading the key from OPENAI_API_KEY by NAME -- the
    value never goes in the file.
    """
    top, tables = config_toggles(capabilities)
    grouped = {}
    for table, line in tables:
        grouped.setdefault(table, []).append(line)

    out = [f'model = {json.dumps(model)}\n',
           'model_provider = "openai_direct"\n',
           f'model_reasoning_effort = {json.dumps(effort)}\n']
    if instructions:
        # The lean base prompt the runner writes beside this file (see lean_instructions).
        # The default is the CONTAINER path, which is where the SWE-bench lane puts it.
        # The Web-Bench lane runs the agent on the host with a per-task CODEX_HOME, so it
        # passes its own absolute path -- a container path there would name a file that does
        # not exist, and `model_instructions_file` pointing at nothing means the base prompt
        # is not replaced and the untooling arm quietly keeps the sections it claims to have
        # dropped.
        out.append('model_instructions_file = '
                   f'{json.dumps(instructions_path or INSTRUCTIONS_PATH)}\n')
    out += [line + "\n" for line in top]
    out.append('\n[model_providers.openai_direct]\n'
               'name = "OpenAI"\n'
               f'base_url = {json.dumps(routing["base_url"])}\n'
               f'env_key = {json.dumps(KEY_ENV)}\n'
               'wire_api = "responses"\n')
    # Trusting the project is what lets `codex exec` run unattended in a fresh checkout.
    out.append(f'\n[projects.{json.dumps(project)}]\ntrust_level = "trusted"\n')
    # MCP servers a scaffold asked for (serena). Codex takes these from config.toml -- there
    # is no argv flag, which is why this lane's AgentProfile.mcp_args is empty -- and
    # `--strict-config` validates the table (command, args, env, cwd, startup_timeout_sec
    # accepted; an unknown key refused; probed on 0.153.1). Emitted before the
    # array-of-tables skills entries, which must stay last.
    for name in sorted(mcp_servers or {}):
        spec = mcp_servers[name]
        out.append(f"\n[mcp_servers.{name}]\n")
        out.append(f"command = {json.dumps(spec['command'])}\n")
        out.append("args = [" + ", ".join(json.dumps(a) for a in spec.get("args", ())) + "]\n")
        if spec.get("cwd"):
            out.append(f"cwd = {json.dumps(spec['cwd'])}\n")
        # Serena starts a language server before it answers; its own docs say 15 s, but in
        # a cold task container the first start has been longer. Generous rather than a
        # per-task race that reads as "the agent never used the tools".
        out.append(f"startup_timeout_sec = {int(spec.get('startup_timeout_sec', 90))}\n")
        if spec.get("env"):
            out.append(f"\n[mcp_servers.{name}.env]\n")
            for k in sorted(spec["env"]):
                out.append(f"{k} = {json.dumps(str(spec['env'][k]))}\n")
    for table in sorted(grouped):
        out.append(f"\n[{table}]\n")
        out += [line + "\n" for line in _dedupe(grouped[table])]
    # Array-of-tables entries go last (they would otherwise swallow the keys after them).
    if "skills" in (capabilities or ()):
        out.append("\n" + "\n".join(skills_config_entries(system_skills)))
    return "".join(out)


def validate_config(text, *, binary=None, home=None, timeout=90):
    """Prove the installed CLI recognizes every field in `text`. ("", ) on success.

    Returns an error string when it does not. This is the gate that keeps a CLI upgrade
    from turning the pruning toggles into a silent no-op: `codex exec --strict-config`
    errors on an unrecognized configuration field, and without it Codex simply ignores one.

    Runs against a THROWAWAY CODEX_HOME and never reaches the network -- config loading
    fails before any request is made, so this costs nothing and bills nothing.
    """
    binary = binary or codex_bin()
    tmp = tempfile.mkdtemp(dir=home)
    try:
        with open(os.path.join(tmp, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(text)
        env = dict(os.environ, CODEX_HOME=tmp)
        try:
            proc = subprocess.Popen(
                [binary, "exec", "--strict-config", "--skip-git-repo-check",
                 "-C", tmp, "probe"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, start_new_session=True)
        except OSError as e:
            return f"could not run the config validator: {e}"
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return f"could not run the config validator: {e}"
        finally:
            _kill_process_group(proc)      # the CLI's helpers can outlive it, as can the ELF
        blob = (out or "") + (err or "")
        for line in blob.splitlines():
            if ("unknown configuration field" in line or "invalid type" in line
                    or "unknown variant" in line or "Error loading config.toml" in line):
                return blob.strip()[:800]
        return ""
    finally:
        # The CLI's helpers (plugin/skill installers) can still be writing into the
        # throwaway home when the process has exited; a leftover temp dir is not an error.
        _rmtree_quietly(tmp)


# --------------------------------------------------------------------------------------
# invocation
# --------------------------------------------------------------------------------------
# Codex has NO turn-cap flag (no `--max-turns`), so `max_turns` is recorded in
# run_config.json and counted by the spiral envelope but is NOT enforced on the agent --
# the same caveat the Copilot lane carries. TE_TASK_TIMEOUT and the spiral guard are the
# only bounds. The model and the reasoning effort come from config.toml rather than from
# flags so that the file this run wrote is the single source of truth for its contract.
def build_argv(*, hook=False):
    """argv for one fresh `codex exec` segment, reading the prompt from stdin.

    `--dangerously-bypass-approvals-and-sandbox`: the container IS the sandbox (ephemeral,
    per task, network-guarded), the same posture as the Claude lane's IS_SANDBOX=1. Codex's
    own sandbox layer inside it would only forbid the edits the task requires.
    """
    argv = [C_BIN, "exec", "--json", "--color", "never", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox"]
    if hook:
        # Enabled hooks otherwise require persisted per-source trust, which an unattended
        # container has no way to grant -- the guard would simply not run.
        argv.append("--dangerously-bypass-hook-trust")
    return argv + ["-C", "/testbed", "-"]


def build_resume_argv(session_id, *, model=None, effort=None, hook=False):
    """argv to continue the EXACT thread the Codex spiral guard stopped.

    Resuming the thread rather than starting a fresh session is the whole mechanism: a new
    session would re-pay for the context it already built and lose the state the nudge is
    written against. `codex exec resume` has a smaller option surface than `exec` -- no
    `-C` and no `--color` -- and the working directory comes from `docker exec -w` instead.
    """
    argv = [C_BIN, "exec", "resume", str(session_id), "--json", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox"]
    if hook:
        argv.append("--dangerously-bypass-hook-trust")
    if model:
        argv += ["-m", model]
    if effort:
        # The policy may resume at a HIGHER effort than the segment that stalled; that is a
        # deliberate part of the mechanism, so it is applied per invocation rather than
        # rewritten into config.toml where it would also affect the first segment.
        argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    return argv + ["-"]


def run_env(extra=None):
    """Non-secret `docker exec -e` values. The CREDENTIAL is never here.

    The key is forwarded by NAME only (`docker exec -e KEY_ENV` with no `=value` takes the
    value from the client's environment), so it never reaches argv -- which on a shared
    host is readable by every other user through `ps`.
    """
    env = {"CODEX_HOME": C_HOME, "HOME": "/tmp/agent-home",
           # Codex writes session rollouts under CODEX_HOME and the resolved model id is
           # recorded there, not in the --json stream. The spiral resume needs the thread
           # id and pricing needs the id, so the home must stay writable and persistent
           # for the whole task -- `--ephemeral` would break both.
           "RUST_BACKTRACE": "0"}
    env.update(extra or {})
    return env


# --------------------------------------------------------------------------------------
# accounting
# --------------------------------------------------------------------------------------
# Codex's `--json` stream carries usage on `turn.completed`, and the shape is NOT the
# Claude one:
#
#     "usage": {input_tokens, cached_input_tokens, cache_write_input_tokens,
#               output_tokens, reasoning_output_tokens}
#
# `input_tokens` is the GRAND input total and already INCLUDES both cache fields (verified
# across probes in the AgentEO study, whose accounting the published Codex rows used). So
# fresh input is input - cached - cache_write, and folding the cache fields into
# input_tokens instead would price ~90% cached re-reads at 1.0x where the vendor bills
# 0.10x -- the same one-directional error that made Copilot rows look cheaper than Claude
# rows before the 2026-09-05 cache-write fix. Reasoning tokens are folded into output
# because that is how they are billed.
_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
               "output_tokens", "reasoning_output_tokens")


def row_usage(stream_text):
    """Token/turn row fields from one segment's `codex exec --json` stream.

    Leaves the token fields None when the stream carried no usage block, so a caller can
    record "unknown" rather than a confident zero -- `usage_incomplete` on the row is what
    makes such a task filterable instead of silently averaged in as cheap.
    """
    tot = dict.fromkeys(_USAGE_KEYS, 0)
    steps = 0
    tool_calls = {}
    saw_turn = saw_usage = False
    thread_id = ""
    errors = []
    for line in (stream_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "item.completed":
            item = ev.get("item") or {}
            itype = item.get("type")
            if itype == "command_execution":
                tool_calls["shell"] = tool_calls.get("shell", 0) + 1
                steps += 1
            elif itype == "file_change":
                tool_calls["apply_patch"] = tool_calls.get("apply_patch", 0) + 1
                steps += 1
            elif itype == "agent_message":
                steps += 1
        elif kind == "turn.completed":
            saw_turn = True
            usage = ev.get("usage") or {}
            if usage:
                saw_usage = True
                for k in _USAGE_KEYS:
                    tot[k] += int(usage.get(k) or 0)
        elif kind == "thread.started":
            thread_id = ev.get("thread_id") or thread_id
        elif kind == "error":
            errors.append(str(ev.get("message") or "")[:200])
        # The thread id also rides on session/thread metadata in some builds; take the
        # first non-empty one seen so a resume never falls back to a FRESH session, which
        # would re-pay for the whole context and is the harmful path codex_spiral guards.
        for key in ("thread_id", "session_id", "conversation_id"):
            if not thread_id and isinstance(ev.get(key), str):
                thread_id = ev[key]

    row = {
        # Codex emits ONE turn.completed per exec however many tools ran, so that
        # undercounts agent effort by an order of magnitude and is not comparable to the
        # other agents' num_turns. Count agent STEPS (messages + tool calls) instead, and
        # say so on the row rather than letting the basis be inferred.
        "num_turns": steps or None,
        "turns_basis": "codex_steps",
        "tool_calls": dict(sorted(tool_calls.items())),
        "thread_id": thread_id,
        "is_error": bool(errors) and not saw_turn,
        # Codex does not report a USD figure, so the vendor cost stays null rather than a
        # fabricated zero.
        "cost_usd_vendor": None,
    }
    if errors:
        row["api_error_status"] = errors[0]
    if saw_usage:
        fresh = max(0, tot["input_tokens"] - tot["cached_input_tokens"]
                    - tot["cache_write_input_tokens"])
        row.update({
            "input_tokens": fresh,
            "cache_read_input_tokens": tot["cached_input_tokens"],
            "cache_creation_input_tokens": tot["cache_write_input_tokens"],
            "output_tokens": tot["output_tokens"] + tot["reasoning_output_tokens"],
            "reasoning_output_tokens": tot["reasoning_output_tokens"],
            "tokens_source": "stream_usage",
        })
        row["tokens_total"] = (row["input_tokens"] + row["cache_read_input_tokens"]
                               + row["cache_creation_input_tokens"]
                               + row["output_tokens"])
    else:
        row.update({
            "input_tokens": None, "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None, "output_tokens": None,
            "tokens_total": None,
            "tokens_source": "no_usage_event" if saw_turn else "no_turn_event",
        })
    return row


def merge_usage(a, b):
    """Combine two spiral segments' usage into one.

    None + number must not become the number: a segment whose usage was unrecoverable makes
    the TOTAL unknown, and reporting the half we have would understate the task's cost --
    precisely on the guarded arm the spiral mechanism is meant to show saving on. So a None
    on either side keeps the field None, and `tokens_source` records the degradation.
    """
    a = a or {}
    b = b or {}
    if not a:
        return dict(b)
    if not b:
        return dict(a)
    out = dict(a)
    for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
              "output_tokens", "reasoning_output_tokens", "tokens_total"):
        x, y = a.get(k), b.get(k)
        out[k] = None if (x is None or y is None) else (x or 0) + (y or 0)
    for k in ("num_turns",):
        x, y = a.get(k), b.get(k)
        out[k] = None if (x is None and y is None) else (x or 0) + (y or 0)
    calls = dict(a.get("tool_calls") or {})
    for name, n in (b.get("tool_calls") or {}).items():
        calls[name] = calls.get(name, 0) + n
    out["tool_calls"] = dict(sorted(calls.items()))
    # The FIRST segment's thread is the one a later resume must continue.
    out["thread_id"] = a.get("thread_id") or b.get("thread_id") or ""
    srcs = [s for s in (a.get("tokens_source"), b.get("tokens_source")) if s]
    out["tokens_source"] = ("stream_usage" if set(srcs) == {"stream_usage"}
                            else "+".join(srcs) or None)
    out["is_error"] = bool(a.get("is_error")) or bool(b.get("is_error"))
    out["cost_usd_vendor"] = None
    return out


# --------------------------------------------------------------------------------------
# the mechanisms' between-task call
# --------------------------------------------------------------------------------------
def optimizer_call(prompt, *, configured_model, routing, max_tokens=16384,
                   timeout=180):
    """One proposer / judge / nudge-writer call on THIS lane's endpoint. (text, eff_tokens).

    The Codex twin of `swebench.anthropic_optimizer_call`, and it exists for the same two
    reasons that one does. It must bill what the TASKS bill -- `ledger.overhead` charges
    these calls to the same basis as the task rows, and a cheaper proposer would understate
    the overhead the arm actually incurs -- and it must be a pure text completion that
    cannot reach the testbed, which a `codex exec` session would not be.

    Over `/v1/responses`, the API the CLI itself speaks, with no tools offered at all --
    so the call is a pure text completion whatever the model answers.

    Never raises: a transport failure returns ("", 0.0), which every mechanism reads as
    "no verdict" and falls back to its LLM-free path. That silent-degradation property is
    why `preflight` below spends one probe call before task 0.
    """
    from .swebench import _log, _log_optimizer_call    # deferred: swebench imports us

    # `configured_model` is already the lane's own model; the driver's "agent" / "codex"
    # label never reaches here, because the runner substitutes its model before calling.
    body = json.dumps({
        "model": configured_model,
        "input": [{"role": "user",
                   "content": [{"type": "input_text", "text": prompt}]}],
        "max_output_tokens": max_tokens,
        "store": False,
    }).encode()
    headers = {"content-type": "application/json",
               "authorization": f"Bearer {routing['api_key']}"}
    req = urllib.request.Request(
        routing["base_url"].rstrip("/") + "/responses",
        data=body, method="POST", headers=headers)
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            _log(f"codex optimizer_call HTTP {e.code}: {e.read()[:1200]!r}")
            return "", 0.0
        except Exception as e:
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            _log(f"codex optimizer_call failed: {e}")
            return "", 0.0
    if data is None:
        return "", 0.0

    text = _responses_text(data)
    usage = data.get("usage") or {}
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    total_in = int(usage.get("input_tokens") or 0)
    # A reasoning model can spend the whole cap on reasoning and emit a ZERO-character
    # answer -- the caller then parses nothing and acts on nothing while the call is billed
    # in full. The Claude path hit exactly this (7 of 40 manual-distill calls at an 8192
    # cap) and answered it by raising the cap and saying so, not by lowering effort: effort
    # is part of the cost basis and cutting it here would price mechanism overhead below
    # what the arm really spends. So warn, loudly, and leave the knob to the operator.
    if data.get("status") == "incomplete" or not text.strip():
        why = ((data.get("incomplete_details") or {}).get("reason")
               or ("empty response" if not text.strip() else "unknown"))
        _log(f"codex optimizer response unusable ({why}) at max_output_tokens="
             f"{max_tokens}: the caller will act on nothing while the call still bills. "
             "Raise TE_OPTIMIZER_MAX_TOKENS.")
    _log_optimizer_call(configured_model, prompt, text, {
        "input_tokens": max(total_in - cached, 0),
        "cache_read_input_tokens": cached,
        "output_tokens": int(usage.get("output_tokens") or 0),
    }, stop_reason=data.get("status"))
    eff = pricing.effective_tokens({
        "model": configured_model,
        "input_tokens": max(total_in - cached, 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "output_tokens": int(usage.get("output_tokens") or 0),
    })
    return text, eff


def _responses_text(data):
    """Concatenate the assistant text out of a Responses payload.

    Tolerates both shapes builds emit -- a top-level `output_text` convenience field and
    the `output[].content[].text` array -- because returning "" from a successful call is
    indistinguishable from a dead endpoint to every mechanism downstream.
    """
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    if isinstance(direct, list):
        joined = "".join(x for x in direct if isinstance(x, str))
        if joined.strip():
            return joined
    out = []
    for item in data.get("output") or ():
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or ():
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                out.append(block["text"])
    return "".join(out)


_SHELL_WRAPPER_RE = re.compile(r"^(?:/bin/|/usr/bin/)?(?:ba|z|da)?sh\s+-l?c\s+", re.S)


def _unwrap_shell(cmd):
    """`/bin/bash -lc "rtk git status"` -> `rtk git status`. Codex records every shell call
    through the login-shell wrapper; the command the agent wrote is the quoted payload."""
    cmd = (cmd or "").strip()
    m = _SHELL_WRAPPER_RE.match(cmd)
    if m:
        cmd = cmd[m.end():].strip()
        if len(cmd) >= 2 and cmd[0] == cmd[-1] and cmd[0] in "\"'":
            cmd = cmd[1:-1]
    return cmd.strip()


def rtk_compliance(transcript_path):
    """How much of this task's shell traffic the agent routed through `rtk`.

    On Codex rtk is INSTRUCTION-based: no hook rewrites anything, the agent is told to
    prefix its commands and either does or does not. So the only honest measure of the arm
    is read off the trace afterwards, exactly the way `answer_key_attempts` is: every shell
    command the agent issued, and how many began with `rtk`. A `rtk proxy ...` call counts
    as prefixed (it went through rtk, unfiltered by request). None in every field when the
    transcript is missing -- a lane of zeros must stay distinguishable from a lane of no
    data.
    """
    from tokeneconomizer import trace
    fields = {"rtk_shell_commands": None, "rtk_prefixed_commands": None,
              "rtk_compliance": None}
    if not transcript_path or not os.path.exists(transcript_path):
        return fields
    try:
        traj = trace.load_trajectory(transcript_path)
    except Exception:
        return fields
    total = prefixed = 0
    for _name, args, _res, _ok in traj:
        if not isinstance(args, dict):
            continue
        cmd = args.get("command") or args.get("cmd") or ""
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(x) for x in cmd)
        cmd = _unwrap_shell(str(cmd))
        if not cmd:
            continue
        total += 1
        first = cmd.split(None, 1)[0]
        if first == "rtk" or first.endswith("/rtk"):
            prefixed += 1
    fields.update({"rtk_shell_commands": total, "rtk_prefixed_commands": prefixed,
                   "rtk_compliance": (round(prefixed / total, 3) if total else None)})
    return fields


def mcp_tool_calls(transcript_path, server=None):
    """MCP tool calls the agent made this task, read off the `codex exec --json` stream.

    A configured MCP server is not a used one, so a scaffold that is an MCP server (serena)
    is measured the way rtk's compliance is: from the trace, after the fact. The item type
    Codex emits for these is `mcp_tool_call` (core/src/mcp_tool_call.rs; the binary carries
    the begin/end/output events), but its field names have not been observed on this build
    yet, so the reader is deliberately tolerant about where the server and tool names sit
    and keeps ONE raw sample of the first item it could not read -- the shape gets pinned in
    a test from a real transcript, not guessed. None in every field with no transcript.
    """
    fields = {"mcp_tool_calls": None, "mcp_tools_used": None, "mcp_unrecognised_items": None,
              "mcp_item_sample": None}
    if server:
        fields[f"{server}_tool_calls"] = None
    if not transcript_path or not os.path.exists(transcript_path):
        return fields
    calls, used, unrecognised, sample = 0, {}, 0, None
    for_server = 0
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or "mcp" not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") if isinstance(ev, dict) else None
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if "mcp" not in kind:
                continue
            if str(ev.get("type") or "").endswith("started"):
                continue                          # count each call once, at completion
            inv = item.get("invocation") if isinstance(item.get("invocation"), dict) else {}
            srv = (item.get("server") or item.get("server_name") or inv.get("server") or "")
            tool = (item.get("tool") or item.get("tool_name") or item.get("name")
                    or inv.get("tool") or "")
            if not tool:
                unrecognised += 1
                if sample is None:
                    sample = line[:400]
                continue
            calls += 1
            key = f"{srv}/{tool}" if srv else str(tool)
            used[key] = used.get(key, 0) + 1
            if server and (srv == server or key.startswith(server + "/") or server in key):
                for_server += 1
    fields.update({"mcp_tool_calls": calls, "mcp_tools_used": used,
                   "mcp_unrecognised_items": unrecognised, "mcp_item_sample": sample})
    if server:
        fields[f"{server}_tool_calls"] = for_server
    return fields


def preflight_optimizer(*, routing, configured_model):
    """Prove the mechanism endpoint answers, BEFORE task 0 -- but only if it will be used.

    `optimizer_call` never raises, so a dead endpoint returns ("", 0.0), every mechanism
    reads that as "no verdict" and falls back to its LLM-free path, and the lane runs to
    completion as an economized arm whose proposer and judges never fired. A whole tree of
    rows that look like a mechanism result and are not. One 16-token call is cheaper than
    discovering that afterwards.

    Skipped for arms that make no paid call (baseline, the scaffolds), which the driver
    signals with TE_OPTIMIZER_PREFLIGHT=0.
    """
    from .swebench import _log

    if os.environ.get("TE_OPTIMIZER_PREFLIGHT", "1") == "0":
        _log("codex: no mechanism on this arm makes a paid call -- "
             "skipping the endpoint preflight")
        return
    probe, _eff = optimizer_call(
        "Reply with the single word: ok", configured_model=configured_model,
        routing=routing, max_tokens=16)
    if not (probe or "").strip():
        raise RuntimeError(
            f"the mechanism-call endpoint {routing['base_url']} did not answer a 16-token "
            "probe. Every mechanism would read that as 'no verdict' and quietly fall back "
            "to its LLM-free path, so an economized arm would run as a baseline wearing "
            "its name. Check the endpoint and the model id, then re-launch.")
    # Say so in the lane log: a silent pass and a never-run preflight look the same.
    _log(f"codex: mechanism endpoint answered the 16-token preflight probe "
         f"({routing['base_url']}, model {configured_model}, {_eff:.0f} eff tokens)")
