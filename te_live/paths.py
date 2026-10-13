"""Where live state lives, and which repository a session belongs to.

State is keyed by REPOSITORY, not by directory: two worktrees of one clone, or a session started
in a subdirectory, are the same stream of work and should share what was learned about it. The
key is derived from the `origin` remote when there is one (so a re-clone keeps its history) and
from the real path of the checkout otherwise. Reading the remote parses `.git/config` directly --
a hook that shelled out to `git` would pay a subprocess on every tool call.

The state root is `$TOKENECONOMIZER_HOME`, else `~/.tokeneconomizer`. It is deliberately not a
host's plugin data directory: the CLI runs from the agent's Bash tool, where those variables are
not exported, and one root lets Claude Code and Codex sessions on the same repository sit side
by side (each host keeps its own subdirectory, because a turn envelope learned on one agent says
nothing about another's turn scale).
"""
import hashlib
import os
import re
from dataclasses import dataclass

HOME_ENV = "TOKENECONOMIZER_HOME"


def state_root(env=None):
    """Absolute path of the live-state root."""
    env = os.environ if env is None else env
    root = env.get(HOME_ENV) or os.path.join(os.path.expanduser("~"), ".tokeneconomizer")
    return os.path.abspath(root)


def secure_root(env=None):
    """Keep the state root private to its owner, as the agents keep their own session logs.

    What the plugin stores -- repository paths, the commands sessions ran, costs, error
    tracebacks -- is derived from those logs, and `bin/te` in it is what the slash commands run.
    New files are created private (`te_bootstrap` sets umask 077); a root that an earlier
    version, or another entry point, created with looser permissions is tightened here."""
    root = state_root(env)
    try:
        st = os.stat(root)
        if st.st_mode & 0o077 and st.st_uid == os.getuid():
            os.chmod(root, 0o700)
    except (OSError, AttributeError):
        pass


def launcher_path(env=None):
    return os.path.join(state_root(env), "bin", "te")


def ensure_launcher(env=None):
    """Keep `<state root>/bin/te` pointing at this plugin's `te`. Returns its path, or None.

    A slash command has to name the CLI's path. Claude Code substitutes `${CLAUDE_PLUGIN_ROOT}`
    into a command's text; the Copilot CLI substitutes nothing but `$ARGUMENTS` (1.0.83:
    `${CLAUDE_PLUGIN_ROOT}`, `${PLUGIN_ROOT}`, `${COPILOT_PLUGIN_ROOT}`, `{{plugin_root}}` all
    arrive literally), and its shell exports none of them. So every hook session start writes
    this one-line launcher, the commands fall back to it, and a person on any host has a stable
    `te` to run. The plugin's location comes from the bootstrap (`TE_PLUGIN_ROOT`); the file is
    rewritten only when it changes, as an installed copy's path does across updates."""
    import shlex
    env = os.environ if env is None else env
    plugin = env.get("TE_PLUGIN_ROOT")
    target = os.path.join(plugin, "scripts", "te") if plugin else ""
    if not target or not os.access(target, os.X_OK):
        return None
    path = launcher_path(env)
    body = ("#!/bin/sh\n# Written by the TokenEconomizer plugin at session start: its `te` "
            "command line.\nexec %s \"$@\"\n" % shlex.quote(target))
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == body:
                return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return path


def find_repo_root(start, env=None):
    """The nearest directory at or above `start` that holds a `.git` entry, else None.

    `.git` may be a directory (a clone) or a file (a worktree or submodule), so existence is the
    test, not `isdir`. The walk honours `GIT_CEILING_DIRECTORIES` as git does: it never climbs
    into a listed directory, so a stray `.git` in a shared parent (`/tmp`, a versioned home
    directory) need not swallow every session beneath it."""
    env = os.environ if env is None else env
    ceilings = {os.path.abspath(c) for c in
                (env.get("GIT_CEILING_DIRECTORIES") or "").split(os.pathsep) if c}
    cur = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur or parent in ceilings:
            return None
        cur = parent


def _git_common_dir(root):
    """The git directory that holds `config` for a checkout, following worktree indirection."""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot
    try:
        with open(dot, encoding="utf-8") as fh:
            line = fh.read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(root, gitdir))
    # A worktree's gitdir is <main>/.git/worktrees/<name>; `commondir` points back at <main>/.git.
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as fh:
            common = fh.read().strip()
        return os.path.normpath(os.path.join(gitdir, common))
    except OSError:
        return gitdir


_REMOTE_RE = re.compile(r'^\s*\[remote\s+"([^"]+)"\]\s*$')
_URL_RE = re.compile(r"^\s*url\s*=\s*(\S+)\s*$")


def origin_url(root):
    """The `origin` remote URL (or the first remote's), read from the git config; None if none."""
    gitdir = _git_common_dir(root) if root else None
    if not gitdir:
        return None
    try:
        lines = open(os.path.join(gitdir, "config"), encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    remotes, current = {}, None
    for line in lines.splitlines():
        m = _REMOTE_RE.match(line)
        if m:
            current = m.group(1)
            continue
        if line.lstrip().startswith("["):
            current = None
            continue
        m = _URL_RE.match(line)
        if current and m and current not in remotes:
            remotes[current] = m.group(1)
    return remotes.get("origin") or (next(iter(remotes.values())) if remotes else None)


def normalize_remote(url):
    """`git@github.com:Owner/Repo.git` and `https://github.com/owner/repo` -> one spelling."""
    u = (url or "").strip()
    u = re.sub(r"^[a-z+]+://", "", u)            # scheme
    u = re.sub(r"^[^@/]+@", "", u)               # user@
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/\d]", u) else u   # scp-style host:path
    u = re.sub(r"\.git$", "", u.rstrip("/"))
    return u.lower()


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "repo").lower()).strip("-")[:40] or "repo"


@dataclass(frozen=True)
class RepoId:
    root: str        # the checkout this session runs in
    key: str         # stable directory name for its state
    label: str       # human name, used in rendered manuals ("owner/repo" or the basename)
    remote: str      # normalized remote, "" when the checkout has none


def repo_identity(cwd, env=None):
    """Identify the repository a session in `cwd` belongs to."""
    root = find_repo_root(cwd, env) or os.path.abspath(cwd or os.getcwd())
    remote = normalize_remote(origin_url(root)) if os.path.exists(os.path.join(root, ".git")) else ""
    canon = remote or os.path.realpath(root)
    label = "/".join(remote.split("/")[-2:]) if remote else os.path.basename(root.rstrip("/"))
    digest = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:10]
    return RepoId(root=root, key=f"{_slug(os.path.basename(root.rstrip('/')))}-{digest}",
                  label=label or "repo", remote=remote)


def repo_state_dir(repo, host, env=None):
    """`<root>/repos/<repo-key>/<host>` -- one host's state for one repository."""
    return os.path.join(state_root(env), "repos", repo.key, host)
