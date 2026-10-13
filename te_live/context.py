"""`LiveContext`: everything one hook call or CLI command needs about where it is running."""
import os
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
