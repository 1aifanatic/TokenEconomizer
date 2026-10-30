"""Load the repo-root `.env` into `os.environ` (stdlib only, no python-dotenv).

The core package is stdlib-only and credential-free by design; this loader lives in
`scripts/` because credentials belong to the *harness* layer (Layer 2 — Docker + agent CLIs
+ API keys), not to the economizer. `run_experiment.py` calls `load_env()` at startup so a
real run picks up ANTHROPIC_API_KEY / OPENAI_API_KEY without every launcher having to
re-export them.

Contract (matches scripts/launch/_env.sh so both loaders agree):
  * plain ``KEY=value`` lines, optional leading ``export``; ``#`` starts a comment;
  * surrounding single/double quotes are stripped; values are otherwise LITERAL — no
    ``$VAR`` interpolation, no command substitution;
  * an existing ``os.environ`` entry always WINS (``override=False``), so a one-off
    ``FOO=bar python scripts/run_experiment.py ...`` still overrides the file;
  * repeated keys use the last nonempty assignment in the file;
  * an empty value (``KEY=``) is treated as unset and skipped, so the committed
    ``.env.example`` placeholders never mask a real shell export.

`.env` is git-ignored; `.env.example` is the tracked template.
"""
import os

__all__ = ["env_path", "load_env", "require_keys"]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_path(path=None):
    """The env file to read: explicit arg, else $TOKENECONOMIZER_ENV_FILE, else
    experiments/.env, else the repository root's .env (where it lived before experiments/
    moved under the plugin repository; an existing one keeps working)."""
    explicit = path or os.environ.get("TOKENECONOMIZER_ENV_FILE")
    if explicit:
        return explicit
    local = os.path.join(_REPO_ROOT, ".env")
    parent = os.path.join(os.path.dirname(_REPO_ROOT), ".env")
    return local if os.path.exists(local) or not os.path.exists(parent) else parent


def _parse(text):
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if val:                       # KEY= means "not set here"; don't mask a real export
            out[key] = val
    return out


def load_env(path=None, override=False):
    """Read the env file into os.environ. Returns the dict of keys actually applied.

    A missing file is not an error -- the offline/stub paths need no credentials at all."""
    p = env_path(path)
    try:
        with open(p, encoding="utf-8") as fh:
            parsed = _parse(fh.read())
    except FileNotFoundError:
        return {}
    applied = {}
    for key, val in parsed.items():
        if override or not os.environ.get(key):
            os.environ[key] = val
            applied[key] = val
    return applied


def require_keys(*names):
    """Exit with a pointed message if any of `names` is missing/empty in os.environ."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(
            "missing credential(s): " + ", ".join(missing)
            + f"\nset them in {env_path()} (copy .env.example) or export them in your shell")
