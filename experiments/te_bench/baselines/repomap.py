"""Repo map — aider's token-budgeted ranked map of the repository, injected as context.

Generated on the HOST and written into the container as project instructions. Host-side
because aider needs a modern Python and tree-sitter, and the SWE-bench images are pinned to
the issue's era; installing aider inside would both fail on old images and change the
environment the agent is being measured in.

Getting the source to the host is the part that differs from the original implementation.
That one read a pre-staged worktree of every instance, which this repo does not have -- the
repo exists only inside the container, checked out at `base_commit` by the image. So the
source is streamed out through the context's `read_bytes` (this module never touches Docker
itself): `git ls-files '*.py'` piped through `tar`, which is
  * exact -- it is the container's own tree at the graded commit, not a host checkout that
    could drift from it;
  * cheap -- only tracked `.py` files, which is all the generator walks anyway, so a
    matplotlib-sized repo moves a few MB rather than a `.git` directory;
  * self-cleaning -- it lands in the task's run dir and is deleted after generation.

The map is a FIXED prefix cost charged on every turn of every task, which is exactly why it
is a baseline worth measuring rather than an obvious win: it shortens long-navigation tasks
and taxes ones that were already localized. `map_chars` is recorded per row so that tax is
visible in the results rather than inferred.
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile

import tokeneconomizer.plugin

from .base import Scaffold, ScaffoldError

DEFAULT_MAP_TOKENS = 2048
# The generator lives in the core package: the plugin's opt-in repository map runs it too.
GEN = os.path.join(os.path.dirname(os.path.abspath(tokeneconomizer.plugin.__file__)),
                   "repomap_gen.py")

HEADER = ("# Repository map\n"
          "Ranked key files and symbols (signatures only) for this repository. Consult "
          "this to locate relevant code before searching.\n\n")


def map_tokens():
    return int(os.environ.get("TE_REPOMAP_TOKENS", DEFAULT_MAP_TOKENS))


def extract_idents(text):
    """Heuristic identifiers from the issue body, used to personalize the ranking.

    Reads ONLY the issue text the agent is given, so there is no gold-patch leakage: the
    map is ranked toward names the agent could have extracted itself.
    """
    import re
    ids = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)`", text or ""))
    ids |= set(re.findall(r"\b([a-z_][a-z0-9_]*_[a-z0-9_]+)\b", text or ""))
    ids |= set(re.findall(r"\b([A-Z][a-zA-Z0-9]+[A-Z][a-zA-Z0-9]*)\b", text or ""))
    return sorted({i.split(".")[-1] for i in ids if len(i) > 2})[:60]


class RepoMapScaffold(Scaffold):
    name = "repomap"
    patch_excludes = ()   # writes only the agent's own instructions file, excluded already

    def preflight(self, profile):
        """aider must be importable by THIS interpreter -- the generator is a subprocess of
        it. Checked once at construction: a missing dependency that surfaces per-task would
        produce a whole tree of `map_ok: false` rows that look like a scaffold result.

        The message names the interpreter, and puts the WRONG-INTERPRETER cause first.
        "Install aider" is the wrong advice for the likelier case: the launchers run
        `${PYTHON:-python3}`, so a lane started from an unactivated shell -- a fresh
        terminal, a cron job, a detached script -- picks up the system Python even though
        `pip install -e '.[baselines]'` put aider in the venv exactly as the guide says. A
        message that only says "install it" sends that reader to reinstall a package they
        already have.
        """
        try:
            import aider.repomap                                   # noqa: F401
        except ImportError as e:
            raise ScaffoldError(
                "the repomap scaffold needs aider importable by the interpreter running "
                f"this lane, which is {sys.executable}. It generates the map on the HOST "
                "because the container's Python is era-pinned. Two causes, likeliest "
                "first:\n"
                "  1. WRONG INTERPRETER -- aider is installed, but not here. The launchers "
                "run `${PYTHON:-python3}`, so an unactivated shell picks up the system "
                "Python. Fix: `source .venv/bin/activate`, or pass "
                "`PYTHON=/path/to/.venv/bin/python` to the launcher.\n"
                "  2. NOT INSTALLED -- `pip install -e '.[baselines]'` (or 'aider-chat') "
                "into THAT interpreter.\n"
                f"({e})") from None

    # `git ls-files -z` + `tar --null -T -` so a path containing a space or a newline
    # cannot split into two arguments and silently truncate the map's view of the repo.
    TAR_SCRIPT = ("cd /testbed && git ls-files -z -- '*.py' | "
                  "tar -cf - --null -T - 2>/dev/null")

    def fetch_sources(self, ctx):
        """Stream the container's tracked `.py` files into a host directory. Returns it."""
        dest = os.path.join(ctx.run_dir, "_repomap_src")
        os.makedirs(dest, exist_ok=True)
        blob = ctx.read_bytes(self.TAR_SCRIPT)
        if not blob:
            raise ScaffoldError("the repository stream out of the container was empty")
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            # No absolute or parent-relative members: this archive is built inside a task
            # container, and extracting one blindly would let it write outside run_dir.
            safe = [m for m in tf.getmembers()
                    if m.isfile() and not m.name.startswith(("/", ".."))
                    and "/../" not in m.name]
            tf.extractall(dest, members=safe)
        return dest

    def generate(self, src_dir, idents):
        """Run the host-side generator. Returns the map text ('' on failure)."""
        res = subprocess.run(
            [sys.executable, GEN, src_dir, str(map_tokens()), ",".join(idents)],
            capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            return "", (res.stderr or "")[-300:]
        return res.stdout, ""

    def setup(self, ctx):
        idents = extract_idents(ctx.task.get("problem_statement", ""))
        notes = {"arm_setup": "repomap", "n_idents": len(idents),
                 "map_tokens_budget": map_tokens()}
        src = None
        try:
            src = self.fetch_sources(ctx)
            text, err = self.generate(src, idents)
        except (ScaffoldError, subprocess.TimeoutExpired) as e:
            text, err = "", str(e)[-300:]
        finally:
            if src and os.path.isdir(src):
                shutil.rmtree(src, ignore_errors=True)

        if not text.strip():
            # No map means this task ran native. Flag it -- averaged into a cell as a
            # repomap row it would dilute exactly the effect being measured.
            ctx.log(f"repomap: generation produced no map ({err})")
            notes.update({"map_ok": False, "map_chars": 0, "map_err": err})
            return notes

        notes["map_ok"] = ctx.write_instructions(HEADER + text)
        notes["map_chars"] = len(text)
        notes["instructions_file"] = ctx.profile.instructions_file
        return notes
