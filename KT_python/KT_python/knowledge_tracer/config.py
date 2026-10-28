"""Feature flags and model hyper-parameters.

Port of {@code KnowledgeTracerProperties}. Every intelligence component can be toggled
for ablation studies (docs/evaluation.md). Defaults are development values, not tuned
constants. Environment variables use the same names as the Spring Boot service.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None else raw


@dataclass
class IrtSettings:
    """Rasch (1PL) baseline."""
    enabled: bool = True
    ability_prior_mean: float = 5.0   # weak prior over learner ability on the 0-10 scale
    ability_prior_sd: float = 2.0
    # TEMPORARY calibration constants — priors only, re-estimated from evidence.
    easy_prior_difficulty: float = 3.0
    medium_prior_difficulty: float = 5.0
    hard_prior_difficulty: float = 7.0
    difficulty_prior_sd: float = 1.5
    # Acceptance-adjusted prior: prior = label prior - shift * z (z = within-label
    # z-score of logit(acceptanceRate), clamped to +-max_shift). Per-label logit
    # mean/sd come from all free LeetCode algorithm problems (docs/irt.md).
    acceptance_shift: float = 0.6
    acceptance_max_shift: float = 1.2
    easy_acceptance_logit_mean: float = 0.8929
    easy_acceptance_logit_sd: float = 0.6924
    medium_acceptance_logit_mean: float = 0.2570
    medium_acceptance_logit_sd: float = 0.6718
    hard_acceptance_logit_mean: float = -0.1888
    hard_acceptance_logit_sd: float = 0.6556
    # Rasch assumes one independent response per learner and item; retries after
    # feedback are not independent, so only first attempts feed ability/difficulty.
    first_attempts_only: bool = True
    window_size: int = 100        # most recent first-attempt responses for ability
    item_window_size: int = 50    # most recent responses for item re-calibration


@dataclass
class DktSettings:
    """Off by default: the shipped model is trained on SYNTHETIC data (docs/dkt.md)."""
    enabled: bool = False
    model_path: str = "../model/artifacts/dkt/dkt-model.json"  # relative to KT_python/
    blend_weight: float = 0.5   # candidate P(success) = (1-w) * IRT + w * DKT
    min_history: int = 3        # events before DKT is blended in


@dataclass
class GraphSettings:
    enabled: bool = True


@dataclass
class LocalizationSettings:
    enabled: bool = True
    calibration_uncertainty_threshold: float = 0.9
    calibration_min_events: int = 8   # counted as first-attempt IRT responses


@dataclass
class BanditSettings:
    alpha: float = 0.5      # UCB exploration width
    lambda_: float = 1.0    # ridge prior strength / warm-start weight
    guarded: bool = True    # deterministic rules still remediate detected gaps


@dataclass
class HttpSettings:
    url: str = "http://localhost:8090/decide"   # kt-jev-v1 protocol endpoint
    timeout_ms: int = 2000
    api_key: str = ""                           # sent as X-API-Key when non-blank


@dataclass
class RewardSettings:
    attribution_window_hours: int = 336   # outcomes later than this are not attributed
    quality_weight: float = 0.0           # reward = w*quality + (1-w)*growth
    growth_scale: float = 0.5             # ability change mapped to growth = 1


@dataclass
class JevSettings:
    """Jev is the bounded decision layer (docs/jev.md)."""
    enabled: bool = True
    provider: str = "deterministic"       # deterministic | bandit | http
    confidence_threshold: float = 0.35    # below this the validator triggers fallback
    bandit: BanditSettings = field(default_factory=BanditSettings)
    http: HttpSettings = field(default_factory=HttpSettings)
    reward: RewardSettings = field(default_factory=RewardSettings)


@dataclass
class MasterySettings:
    prior_alpha: float = 1.0        # uninformative Beta-Bernoulli prior
    prior_beta: float = 1.0
    weak_threshold: float = 0.45    # mastery below this flags a weak concept
    min_evidence: int = 2           # minimum evidence before trusting the estimate


@dataclass
class RecommendationSettings:
    target_success_probability: float = 0.65   # "productive struggle" heuristic
    recent_window: int = 20                    # events used by gap/streak analysis


@dataclass
class SecuritySettings:
    api_key: str = ""               # blank = API-key check disabled (local dev)
    allowed_origins: List[str] = field(default_factory=lambda: [
        "chrome-extension://*",
        "http://localhost:*",
        "http://127.0.0.1:*",
        "https://leetcode.com",
        "https://leetcode.cn",
    ])

    @property
    def api_key_enabled(self) -> bool:
        return bool(self.api_key and self.api_key.strip())


@dataclass
class ResearchSettings:
    export_enabled: bool = False            # exports contain behavioural data
    pseudonym_salt: str = "change-me"       # HMAC key for learner pseudonyms


@dataclass
class Settings:
    """Root settings object, equivalent to the Spring Environment + properties."""
    irt: IrtSettings = field(default_factory=IrtSettings)
    dkt: DktSettings = field(default_factory=DktSettings)
    graph: GraphSettings = field(default_factory=GraphSettings)
    localization: LocalizationSettings = field(default_factory=LocalizationSettings)
    jev: JevSettings = field(default_factory=JevSettings)
    mastery: MasterySettings = field(default_factory=MasterySettings)
    recommendation: RecommendationSettings = field(default_factory=RecommendationSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    research: ResearchSettings = field(default_factory=ResearchSettings)
    # Python-only infrastructure settings (the Java side gets these from Spring):
    database_url: str = "sqlite:///./kt.db"
    catalog_path: str = "../data/seed/catalog_1000.json"   # relative to KT_python/

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.irt.enabled = _env_bool("KT_IRT_ENABLED", s.irt.enabled)
        s.dkt.enabled = _env_bool("KT_DKT_ENABLED", s.dkt.enabled)
        s.dkt.model_path = _env_str("KT_DKT_MODEL_PATH", s.dkt.model_path)
        s.dkt.blend_weight = _env_float("KT_DKT_BLEND_WEIGHT", s.dkt.blend_weight)
        s.dkt.min_history = _env_int("KT_DKT_MIN_HISTORY", s.dkt.min_history)
        s.graph.enabled = _env_bool("KT_GRAPH_ENABLED", s.graph.enabled)
        s.localization.enabled = _env_bool("KT_LOCALIZATION_ENABLED", s.localization.enabled)
        s.jev.enabled = _env_bool("KT_JEV_ENABLED", s.jev.enabled)
        s.jev.provider = _env_str("KT_JEV_PROVIDER", s.jev.provider)
        s.jev.http.url = _env_str("KT_JEV_HTTP_URL", s.jev.http.url)
        s.jev.http.api_key = _env_str("KT_JEV_HTTP_API_KEY", s.jev.http.api_key)
        s.security.api_key = _env_str("KT_API_KEY", s.security.api_key)
        origins = _env_str("KT_ALLOWED_ORIGINS", "")
        if origins.strip():
            s.security.allowed_origins = [o.strip() for o in origins.split(",") if o.strip()]
        s.research.export_enabled = _env_bool("KT_RESEARCH_EXPORT", s.research.export_enabled)
        s.research.pseudonym_salt = _env_str("KT_PSEUDONYM_SALT", s.research.pseudonym_salt)
        s.database_url = _env_str("KT_DATABASE_URL", s.database_url)
        s.catalog_path = _env_str("KT_CATALOG_PATH", s.catalog_path)
        return s
