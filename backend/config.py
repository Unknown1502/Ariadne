"""Runtime configuration.

Ariadne is local-first by construction. Every Google Cloud dependency is behind a feature
flag that defaults to off, and every one has an offline adapter. That is not a convenience
for development: the scientific core has to be verifiable by someone with no cloud account,
or its results are not independently checkable.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VAR_DIR = REPO_ROOT / "var"


class Settings(BaseSettings):
    """Environment-driven settings. See .env.example for the full surface."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- storage -----------------------------------------------------------------------
    database_url: str = ""
    """Empty means 'use the local SQLite ledger under var/'. A postgresql+psycopg URL
    switches to Cloud SQL without any other change."""

    var_dir: Path = DEFAULT_VAR_DIR
    runtime_store: Literal["local", "firestore"] = "local"

    # --- messaging ---------------------------------------------------------------------
    event_bus: Literal["local", "pubsub"] = "local"
    pubsub_model_topic: str = "ariadne.model-events"
    """One topic carries every event type. A separate investigation topic was declared and
    never used, and Terraform only ever created this one - a second name in config that no
    subscriber reads is a promise nothing keeps."""

    pubsub_dead_letter_topic: str = "ariadne.dead-letter"
    max_delivery_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_delay_seconds: float = Field(default=0.05, ge=0.0, le=60.0)
    retry_max_delay_seconds: float = Field(default=5.0, ge=0.0, le=600.0)

    # --- reasoning ---------------------------------------------------------------------
    llm_provider: Literal["stub", "gemini"] = "stub"
    """`stub` is a deterministic offline reasoner. It is the default so the test suite and
    the scientific core never depend on a network call or an API key."""

    gemini_model: str = "gemini-2.5-flash"
    gemini_max_output_tokens: int = Field(default=2048, ge=256, le=32768)
    google_api_key: str = ""

    # There is deliberately no `gemini_temperature`. Every agent pins temperature to 0.0
    # because claim compilation has to be reproducible - the same explanation must compile
    # to the same claim, or provenance cannot distinguish "the model changed" from "the
    # sampler did". Exposing a knob whose only effect is to break that guarantee would be
    # worse than not having one.

    agent_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    agent_loop_budget: int = Field(default=3, ge=1, le=10)
    """Global ceilings, applied to every agent manifest at construction.

    They tighten, never loosen: an agent whose manifest allows less keeps its lower value.
    Per-agent budgets are deliberate (the Verifier gets one attempt, the Experimenter gets
    longer for execution), and a global override that raised them would erase that tuning.
    """

    # --- google cloud ------------------------------------------------------------------
    enable_google_cloud: bool = False
    gcp_project_id: str = ""
    gcp_region: str = "asia-south1"
    use_vertex_ai: bool = False
    firestore_database: str = "(default)"

    # Settings for capabilities that do not exist are rejected below rather than accepted
    # and ignored. `cloud_storage_bucket`, Memory Bank, Agent Gateway, and Model Armor are
    # all documented as unintegrated; a flag that silently does nothing would turn that
    # honest gap into a quiet false claim.
    cloud_storage_bucket: str = ""
    enable_memory_bank: bool = False
    enable_agent_gateway: bool = False
    enable_model_armor: bool = False

    # --- science -----------------------------------------------------------------------
    default_repetitions: int = Field(default=24, ge=3, le=100)
    default_seed: int = Field(default=20260101, ge=0, le=2**31 - 1)
    evidence_validity_days: int = Field(default=90, ge=1, le=3650)
    """How long evidence stays current for *operational* purposes - audit priority and
    staleness.

    Distinct from `Policy.thresholds.stale_days`, which scores debt, on purpose. Debt has to
    be comparable across time, so its threshold is versioned with the policy rather than
    settable per deployment. This one can be tuned freely because nothing compares its
    outputs across deployments.
    """

    @model_validator(mode="after")
    def _cloud_flags_are_coherent(self) -> Settings:
        if self.enable_google_cloud and not self.gcp_project_id:
            raise ValueError("enable_google_cloud=true requires GCP_PROJECT_ID")
        if self.event_bus == "pubsub" and not self.enable_google_cloud:
            raise ValueError("event_bus=pubsub requires enable_google_cloud=true")
        if self.runtime_store == "firestore" and not self.enable_google_cloud:
            raise ValueError("runtime_store=firestore requires enable_google_cloud=true")
        if self.llm_provider == "gemini" and not (self.google_api_key or self.use_vertex_ai):
            raise ValueError(
                "llm_provider=gemini requires GOOGLE_API_KEY, or USE_VERTEX_AI=true with "
                "application default credentials"
            )
        return self

    @model_validator(mode="after")
    def _unimplemented_features_fail_loudly(self) -> Settings:
        """Refuse to start when asked for something that does not exist.

        Three settings were previously accepted, validated, and read by nothing at all.
        Silently ignoring them is the same defect that made RUNTIME_STORE=firestore report a
        cloud store while writing local files: the operator believes a capability is on, and
        nothing anywhere says otherwise.
        """
        unimplemented = {
            "ENABLE_MEMORY_BANK": self.enable_memory_bank,
            "ENABLE_AGENT_GATEWAY": self.enable_agent_gateway,
            "ENABLE_MODEL_ARMOR": self.enable_model_armor,
            "CLOUD_STORAGE_BUCKET": bool(self.cloud_storage_bucket),
        }
        requested = [name for name, enabled in unimplemented.items() if enabled]
        if requested:
            raise ValueError(
                f"{', '.join(requested)} requested, but these are not implemented in this "
                f"build - see docs/limitations.md. Leaving them unset is the honest "
                f"configuration; accepting them silently would not be."
            )
        return self

    @property
    def resolved_database_url(self) -> str:
        """The SQLAlchemy URL to use, defaulting to a file-backed SQLite ledger."""
        if self.database_url:
            return self.database_url
        self.var_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.var_dir / 'ariadne.sqlite3').as_posix()}"

    @property
    def runtime_dir(self) -> Path:
        path = self.var_dir / "runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def artifact_dir(self) -> Path:
        path = self.var_dir / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so config is read once and cannot drift mid-run."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cache. Tests use this after changing the environment."""
    get_settings.cache_clear()
