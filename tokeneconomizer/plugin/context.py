"""`LiveContext`: everything one hook call or CLI command needs about where it is running."""
import os
import subprocess
import sys
from dataclasses import dataclass

from .config import LiveConfig, load_config
from .paths import RepoId, repo_identity, repo_state_dir, state_root
from .store import Store


@dataclass
class LiveContext:
    host: object            # hosts.Host
    repo: RepoId
    root: str               # the state root
    store: Store            # this host's state for this repository
    config: LiveConfig
    env: dict

    @property
    def mech_dir(self):
        """Where the mechanism classes keep their own state (their `results_root`)."""
        return self.store.ensure("mechanisms")

    @property
    def headless(self):
        if self.config.mode == "headless":
            return True
        if self.config.mode == "interactive":
            return False
        return self.host.headless(self.env)

    @classmethod
    def build(cls, host, cwd, env=None):
        env = dict(os.environ if env is None else env)
        repo = repo_identity(cwd, env)
        root = state_root(env)
        base = repo_state_dir(repo, host.name, env)
        store = Store(base)
        config = load_config(root, repo_dir=os.path.dirname(base), env=env)
        return cls(host=host, repo=repo, root=root, store=store, config=config, env=env)

    def spawn(self, argv, log_name):
        """Run `te <argv>` for this host and repository in a detached process, its output
        appended to `<store>/<log_name>`. A hook uses it for anything slower than a hook may
        take (folding a session, the paid judge, the repository map)."""
        # The directory holding the package: the checkout, or the bundle's lib/.
        core = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = dict(self.env)
        env["PYTHONPATH"] = core + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH")
                                    else "")
        cmd = [sys.executable, "-m", "tokeneconomizer.plugin.cli", *argv,
               "--host", self.host.name, "--cwd", self.repo.root]
        self.store.ensure()
        log = open(self.store.path(log_name), "a")
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             cwd=self.repo.root, env=env, start_new_session=True)
        finally:
            log.close()
        return True
