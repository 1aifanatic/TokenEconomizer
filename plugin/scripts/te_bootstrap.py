"""Locate TokenEconomizer (`tokeneconomizer` + `te_live`) and run the hook dispatcher (`hook`)
or the command line (`cli`).

The core package is found, in order:

  1. `$TE_CORE_PATH`                  -- a checkout you point at explicitly;
  2. `<plugin root>/lib`              -- in the repository, symlinks to the two packages; Claude
                                         Code's install copies the plugin directory into its
                                         cache (even from a local marketplace) and follows them,
                                         so the installed copy is self-contained. In the bundle
                                         `scripts/build_plugin.py` writes, real copies;
  3. `<plugin root>/..`               -- the repository this plugin directory lives in, for a
                                         checkout whose symlinks were not created (`--plugin-dir`);
  4. an installed package            -- `pip install` of the repository.

This file must stay importable by any Python 3 so that an old interpreter can still answer a
hook with `{}` instead of a syntax error: no f-strings, no annotations, nothing newer than 3.0.
"""
import os
import sys

MIN_VERSION = (3, 10)


def core_path():
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.environ.get("TE_CORE_PATH"), os.path.join(plugin_root, "lib"),
                 os.path.dirname(plugin_root)):
        if cand and os.path.isfile(os.path.join(cand, "te_live", "hook.py")) and \
                os.path.isfile(os.path.join(cand, "tokeneconomizer", "__init__.py")):
            return cand
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hook"
    rest = sys.argv[2:]
    if sys.version_info < MIN_VERSION:
        if mode == "hook":
            sys.stdin.read()
            sys.stdout.write("{}\n")
            return 0
        sys.stderr.write("TokenEconomizer needs Python >= %d.%d (this is %s); set TE_PYTHON\n"
                         % (MIN_VERSION[0], MIN_VERSION[1], sys.version.split()[0]))
        return 1
    # Everything the plugin writes under its state root is derived from the agents' session
    # logs, which the agents keep private; so are these files.
    os.umask(0o077)
    # Where this plugin is, for the stable launcher the hooks keep (`paths.ensure_launcher`).
    os.environ["TE_PLUGIN_ROOT"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    core = core_path()
    if core:
        sys.path.insert(0, core)
    try:
        if mode == "hook":
            from te_live import hook
            return hook.main(rest)
        from te_live import cli
        return cli.main(rest)
    except ImportError:
        if mode == "hook":
            sys.stdout.write("{}\n")
            return 0
        raise


if __name__ == "__main__":
    sys.exit(main())
