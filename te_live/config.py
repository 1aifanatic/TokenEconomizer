"""Live configuration: which mechanisms act in real sessions, and how far each may go.

The defaults are the conservative end of every switch, chosen from what the local replay of real
sessions showed (docs/PLUGIN.md) rather than from the benchmark arms:

  * inject   = "envfacts"  -- the LLM-free layer only. The paid proposer (`envfacts+distiller`,
                              `distiller`) is opt-in, and refused unless `paid` is also true;
  * untool   = "propose"   -- evidence is gathered and a deny list is proposed, but nothing is
                              written to the user's settings until they approve it. "auto"
                              applies proposals as they form;
  * spiral   = "nudge"     -- at most one mid-turn redirect per prompt-turn. "guard" also stops a
                              turn that keeps spinning after the nudge, and only in headless mode;
                              interactive sessions get a notice instead, because there a person is
                              already the abort;
  * paid     = False       -- no mechanism makes an LLM call of its own. Every paid call that is
                              allowed is charged to the session ledger, as the experiments charge
                              optimizer_tokens to an arm;
  * shadow   = False       -- with True, every decision is computed and logged but none reaches
                              the agent. The way to measure a mechanism before trusting it.

Each of the three mechanisms also takes a plain `on` / `off` (`on` = the value above), and each
has hyperparameters a person can set by hand:

  * inject_max_facts, inject_max_tokens -- size caps on the injected fact block;
  * propose_every                       -- sessions between paid proposer runs;
  * untool_evidence                     -- unused sessions before a tool is proposed;
  * spiral_aggr                         -- the guard's threshold preset;
  * spiral_nudges                       -- redirects per prompt-turn before it stops/notifies;
  * spiral.<name>                       -- one guard threshold, pinned over the preset and over
                                           what the repository's history learned (SPIRAL_KEYS).

Sources, lowest precedence first: built-in defaults, `<root>/config.json`, the repository's
`config.json`, then environment variables (`TE_INJECT`, `TE_UNTOOL`, `TE_SPIRAL`, `TE_MODE`,
`TE_PAID`, `TE_SHADOW`, `TE_DISABLE`, and one per hyperparameter; `TE_SPIRAL_THRESHOLDS` takes
JSON). An invalid value is ignored with a warning recorded in the result rather than raised: a
typo in a config file must not break the agent's hooks.
"""
import json
import os
from dataclasses import asdict, dataclass, field, fields

INJECT = ("off", "envfacts", "envfacts+distiller", "distiller")
UNTOOL = ("off", "propose", "auto")
SPIRAL = ("off", "nudge", "guard")
MODES = ("auto", "interactive", "headless")
AGGR = ("conservative", "default", "aggressive")

# What `on` turns each mechanism to, and the words read as on / off.
ON_VALUE = {"inject": "envfacts", "untool": "propose", "spiral": "nudge"}
_ON = ("on", "true", "yes", "1", "enable", "enabled")
_OFF = ("off", "false", "no", "0", "disable", "disabled")

# The spiral guard's thresholds a person may pin (`spiral.<name>=<value>`), with their type and
# range. They are the keys of `tokeneconomizer.vendor.spiral.DEFAULT_THRESHOLDS`; a turn's
# "turns" are its tool calls here.
SPIRAL_KEYS = {
    "min_turns": (int, 1, None, "tool calls in a turn before it can count as stuck"),
    "edit_churn": (int, 1, None, "edits to one file that count as churn"),
    "distinct_edit_files": (int, 1, None, "files edited that count as scattered"),
    "error_outputs": (int, 1, None, "failing tool results that count as sustained errors"),
    "stagnation": (float, 0.0, 1.0, "share of failures that repeat an earlier one"),
    "envelope_pct": (float, 1.0, 100.0, "percentile of this repository's ended-green turns"),
    "envelope_margin": (float, 0.1, None, "multiplier on that percentile: the envelope"),
    "envelope_min_samples": (int, 1, None, "ended-green turns needed before the envelope is used"),
    "backstop_mult": (float, 0.1, None, "stuck past this multiple of the envelope: redirect, "
                                        "then stop"),
    "judge_confidence": (float, 0.0, 1.0, "paid judge confidence needed to act (paid=true)"),
    "gate_min_turns": (int, 1, None, "tool calls before the paid judge is consulted"),
    "gate_error_outputs": (int, 1, None, "failing results that wake the paid judge early"),
}


@dataclass
class LiveConfig:
    enabled: bool = True
    mode: str = "auto"
    inject: str = "envfacts"
    inject_max_facts: int = 4
    inject_max_tokens: int = 150
    propose_every: int = 5
    untool: str = "propose"
    untool_evidence: int = 5
    spiral: str = "nudge"
    spiral_aggr: str = "default"
    spiral_nudges: int = 1
    spiral_thresholds: dict = field(default_factory=dict)
    paid: bool = False
    paid_model: str = ""
    shadow: bool = False
    vocabulary: str = "auto"
    warnings: list = field(default_factory=list)

    _CHOICES = {"mode": MODES, "inject": INJECT, "untool": UNTOOL, "spiral": SPIRAL,
                "spiral_aggr": AGGR, "vocabulary": ("auto", "python-git", "node-web")}

    def as_dict(self):
        d = asdict(self)
        d.pop("warnings", None)
        return d

    def set(self, key, value, source="?"):
        """Apply one setting with type coercion and validation; False if rejected."""
        if key.startswith("spiral."):
            return self._set_threshold(key.split(".", 1)[1], value, source)
        names = {f.name: f for f in fields(self) if f.name != "warnings"}
        if key not in names:
            self.warnings.append(f"{source}: unknown key {key!r}")
            return False
        if key == "spiral_thresholds":
            if not isinstance(value, dict):
                self.warnings.append(f"{source}: spiral_thresholds must be an object")
                return False
            return all([self._set_threshold(k, v, source) for k, v in value.items()])
        if key in ON_VALUE and isinstance(value, (str, bool, int)):
            word = str(value).strip().lower()
            if word in _ON:
                value = ON_VALUE[key]
            elif word in _OFF:
                value = "off"
        cur = getattr(self, key)
        try:
            if isinstance(cur, bool):
                v = value if isinstance(value, bool) else str(value).strip().lower() in (
                    "1", "true", "yes", "on")
            elif isinstance(cur, int):
                v = int(value)
                if v < 1:
                    raise ValueError("must be >= 1")
            else:
                v = str(value).strip()
        except (TypeError, ValueError) as e:
            self.warnings.append(f"{source}: {key}={value!r} rejected ({e})")
            return False
        choices = self._CHOICES.get(key)
        if choices and v not in choices:
            self.warnings.append(f"{source}: {key}={v!r} not one of {choices}")
            return False
        setattr(self, key, v)
        return True

    def _set_threshold(self, name, value, source):
        spec = SPIRAL_KEYS.get(name)
        if spec is None:
            self.warnings.append(f"{source}: unknown spiral threshold {name!r} "
                                 f"(one of {', '.join(SPIRAL_KEYS)})")
            return False
        kind, lo, hi, _ = spec
        try:
            if isinstance(value, bool):
                raise ValueError("not a number")
            v = kind(float(value)) if kind is int and float(value).is_integer() else kind(value)
        except (TypeError, ValueError) as e:
            self.warnings.append(f"{source}: spiral.{name}={value!r} rejected ({e})")
            return False
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            rng = f">= {lo}" if hi is None else f"between {lo} and {hi}"
            self.warnings.append(f"{source}: spiral.{name}={v!r} rejected (must be {rng})")
            return False
        self.spiral_thresholds[name] = v
        return True

    @property
    def inject_effective(self):
        """The injection profile that will actually run: a paid profile without `paid` falls
        back to the free layer, and says so in `warnings` when the config is loaded."""
        if self.inject in ("envfacts+distiller", "distiller") and not self.paid:
            return "envfacts"
        return self.inject


_ENV = {"TE_MODE": "mode", "TE_INJECT": "inject", "TE_UNTOOL": "untool", "TE_SPIRAL": "spiral",
        "TE_PAID": "paid", "TE_SHADOW": "shadow", "TE_PROPOSE_EVERY": "propose_every",
        "TE_UNTOOL_EVIDENCE": "untool_evidence", "TE_SPIRAL_AGGR": "spiral_aggr",
        "TE_PAID_MODEL": "paid_model", "TE_VOCABULARY": "vocabulary",
        "TE_INJECT_MAX_FACTS": "inject_max_facts", "TE_INJECT_MAX_TOKENS": "inject_max_tokens",
        "TE_SPIRAL_NUDGES": "spiral_nudges"}


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(root, repo_dir=None, env=None):
    """Merge defaults, the global file, the repository file and the environment."""
    env = os.environ if env is None else env
    cfg = LiveConfig()
    for label, path in (("global", os.path.join(root, "config.json")),
                        ("repo", os.path.join(repo_dir, "config.json") if repo_dir else None)):
        if path:
            for k, v in _read(path).items():
                cfg.set(k, v, source=f"{label} {path}")
    for var, key in _ENV.items():
        if env.get(var) not in (None, ""):
            cfg.set(key, env[var], source=var)
    if env.get("TE_SPIRAL_THRESHOLDS"):
        try:
            cfg.set("spiral_thresholds", json.loads(env["TE_SPIRAL_THRESHOLDS"]),
                    source="TE_SPIRAL_THRESHOLDS")
        except ValueError:
            cfg.warnings.append("TE_SPIRAL_THRESHOLDS: not valid JSON")
    if str(env.get("TE_DISABLE", "")).strip().lower() in ("1", "true", "yes", "on"):
        cfg.enabled = False
    if cfg.inject != cfg.inject_effective:
        cfg.warnings.append(f"inject={cfg.inject!r} makes paid calls but paid=false; "
                            f"running {cfg.inject_effective!r}")
    return cfg


def write_config(path, updates):
    """Merge `updates` into the JSON config at `path` (validating each key first).

    A `spiral.<name>` key lands in the file's `spiral_thresholds` object."""
    probe = LiveConfig()
    bad = [k for k, v in updates.items() if not probe.set(k, v, source="te config")]
    if bad:
        raise ValueError("; ".join(probe.warnings))
    current = _read(path)
    for k in updates:
        if k.startswith("spiral."):
            name = k.split(".", 1)[1]
            current.setdefault("spiral_thresholds", {})[name] = probe.spiral_thresholds[name]
        elif k == "spiral_thresholds":
            current.setdefault("spiral_thresholds", {}).update(probe.spiral_thresholds)
        else:
            current[k] = getattr(probe, k)
    _write(path, current)
    return current


def unset_config(path, keys):
    """Remove keys from the JSON config at `path`, so the next source down applies again.

    `spiral.<name>` removes one threshold; `spiral_thresholds` removes them all."""
    known = {f.name for f in fields(LiveConfig) if f.name != "warnings"}
    bad = [k for k in keys if k not in known and not (
        k.startswith("spiral.") and k.split(".", 1)[1] in SPIRAL_KEYS)]
    if bad:
        raise ValueError(f"unknown key(s): {', '.join(bad)}")
    current = _read(path)
    for k in keys:
        if k.startswith("spiral."):
            (current.get("spiral_thresholds") or {}).pop(k.split(".", 1)[1], None)
            if not current.get("spiral_thresholds"):
                current.pop("spiral_thresholds", None)
        else:
            current.pop(k, None)
    _write(path, current)
    return current


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
