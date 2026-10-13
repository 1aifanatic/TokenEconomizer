"""EconomizerConfig: three independent on/off switches, one per mechanism.

The three mechanisms act at three different lifecycle points and never contend for the
same lever, so the config is just their union. `from_args` maps CLI-style flags onto the
config; unknown attributes default off (via getattr), so a partial args object is fine.

  * injection  -- inject a firing-gated per-repo operating manual before the task.
  * tooling    -- drop inapplicable tool schemas (generic evidence untooling for
                  Claude/Copilot; Codex-native capability untooling for Codex).
  * spiral     -- kill/nudge a stuck rollout in flight (generic guard for Claude/Copilot;
                  Codex-native post-green finalize for Codex).
"""
from dataclasses import dataclass, field


@dataclass
class InjectionConfig:
    """Injection (learned per-repo operating manual) stage.

    `propose_every` is the paid proposer cadence (one LLM tick per this many tasks). The
    injection Stage is the single source of truth for it and rebinds the distiller module
    global at build time. Default 5 -- first injection a few tasks earlier and more
    revision/grounding ticks over a stream.
    """
    enabled: bool = False
    propose_every: int = 5
    # Which distiller(s) render the prefix:
    #   distiller           -- the generic paid-proposer manual (`injection.Distiller`);
    #   envfacts            -- LLM-free environment facts learned from recurring evidence
    #                          shapes (`envfacts.EnvFactsDistiller`). Makes NO paid call;
    #   envfacts+distiller  -- both, the free layer first and its claims handed to the
    #                          proposer as already-stated. Paid (the proposer).
    profile: str = "distiller"
    # Caps on the environment-fact block (`envfacts`): at most this many facts, and this many
    # tokens (~4 characters each) for the whole block. 0 keeps envfacts.MAX_FACTS / MAX_TOKENS.
    max_facts: int = 0
    max_tokens: int = 0

    PROFILES = ("distiller", "envfacts", "envfacts+distiller")

    def __post_init__(self):
        if self.profile not in self.PROFILES:
            raise ValueError(f"injection profile {self.profile!r} is not one of {self.PROFILES}")

    @property
    def paid(self):
        """True when this stage will make a paid between-task LLM call."""
        return bool(self.enabled and "distiller" in self.profile)


@dataclass
class ToolingConfig:
    """Untooling stage.

    Generic evidence untooling (Claude / Copilot):
      * evidence          -- drop a tool loaded-but-unused for >= this many tasks (>0 on).
      * evidence_reaudit  -- periodically reload the full catalog every this many tasks.
      * judge             -- add a per-task instruction-aware LLM tool judge.

    Codex-native capability untooling:
      * codex_untool      -- enable the evidence-gated, resolve-safe capability controller.
    """
    evidence: int = 0
    evidence_reaudit: int = 0
    judge: bool = False
    codex_untool: bool = False

    @property
    def enabled(self):
        return bool(self.evidence or self.codex_untool)


@dataclass
class SpiralConfig:
    """Spiral guard stage (generic for Claude/Copilot; Codex-native for Codex)."""
    guard: bool = False            # generic spiral guard (Claude / Copilot)
    judge: bool = False            # sampled LLM trace-judge for the abort decision
    nudge: bool = False            # rung-1 same-container nudge before abort
    nudge_llm: bool = False        # LLM-written nudge redirect (vs the count template)
    aggr: str = "default"          # aggressiveness: conservative | default | aggressive
    frozen_envelope: float = None  # pre-registered resolved-turn envelope for SHORT
                                   # streams where within-stream p90 never warms up;
                                   # None = learn within-stream only.
    codex_spiral: bool = False     # Codex-native controller (post-green finalize)
    # Which Codex policy: "post-green" (the shipped finalize rule alone) or "defensive"
    # (finalize plus every deterministic spiral signature, two distinct nudges per task,
    # never an abort).
    codex_spiral_profile: str = "post-green"
    CODEX_SPIRAL_PROFILES = ("post-green", "defensive")

    def __post_init__(self):
        if self.codex_spiral_profile not in self.CODEX_SPIRAL_PROFILES:
            raise ValueError(f"codex_spiral_profile must be one of "
                             f"{self.CODEX_SPIRAL_PROFILES}, got {self.codex_spiral_profile!r}")

    @property
    def enabled(self):
        return bool(self.guard or self.codex_spiral)


def needs_optimizer(config):
    """Whether ANY enabled mechanism will make a paid between-task LLM call.

    The three clauses mirror `facade.Economizer.__init__` exactly -- it resolves an
    optimizer runner for injection when `injection.paid` (enabled AND a profile that
    carries the paid `distiller`; the LLM-free `envfacts` profile never calls), for untooling when
    `tooling.judge`, and for spiral when the guard is on AND it has a judge or an LLM
    nudge writer. Kept as one predicate because callers OUTSIDE the facade need the
    aggregate answer: a harness whose mechanism transport must be preflighted should not
    pay for (or depend on) that probe when the arm will never call it. Arm `mode` is NOT
    the test -- `spiral-only` and `untool-only` are `standalone` and do make paid calls.

    tests/test_optimizer_basis.py asserts this agrees with the facade for every arm in the
    registry, so the two cannot drift.
    """
    return bool(
        config.injection.paid
        or config.tooling.judge
        or (config.spiral.guard and (config.spiral.judge or config.spiral.nudge_llm))
    )


@dataclass
class EconomizerConfig:
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    tooling: ToolingConfig = field(default_factory=ToolingConfig)
    spiral: SpiralConfig = field(default_factory=SpiralConfig)

    @classmethod
    def from_args(cls, args):
        injection = InjectionConfig(
            enabled=bool(getattr(args, "inject", False)),
            propose_every=int(getattr(args, "propose_every", 0)
                              or InjectionConfig.propose_every),
            profile=str(getattr(args, "inject_profile", "") or InjectionConfig.profile))
        tooling = ToolingConfig(
            evidence=int(getattr(args, "untool_evidence", 0)),
            evidence_reaudit=int(getattr(args, "untool_evidence_reaudit", 0)),
            judge=bool(getattr(args, "untool_judge", False)),
            codex_untool=bool(getattr(args, "codex_untool", False)))
        spiral = SpiralConfig(
            guard=bool(getattr(args, "spiral_guard", False)),
            judge=bool(getattr(args, "spiral_judge", False)),
            nudge=bool(getattr(args, "spiral_nudge", False)),
            nudge_llm=bool(getattr(args, "spiral_nudge_llm", False)),
            aggr=getattr(args, "spiral_aggr", "default"),
            frozen_envelope=getattr(args, "spiral_frozen_envelope", None),
            codex_spiral=bool(getattr(args, "codex_spiral", False)),
            codex_spiral_profile=getattr(args, "codex_spiral_profile", None) or "post-green")
        return cls(injection=injection, tooling=tooling, spiral=spiral)
