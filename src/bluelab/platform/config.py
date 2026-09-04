"""Typed settings (pydantic-settings).

Read from the environment only; secret *values* arrive at runtime through the
application's own role, never through the pipeline (pipeline/02 §4, ADR-0028).

Carries no branch keyed on the rollout tier. SEC-026 / infra C-6 make a
tier-conditional application branch a build failure
(`tools/scan_tier_branches.py`); the tier lives in composition and configuration
values, never in a code path.

The enforcement here is structural rather than documentary: **there is no `tier`
field on `Settings`.** Code cannot branch on a value it cannot read.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Plane(StrEnum):
    """Which plane this process is. Selects the entrypoint, nothing else."""

    API = "api"
    WORKER = "worker"
    SWEEPER = "sweeper"


class Environment(StrEnum):
    """local · staging · prod — the three environments, no more (ADR-0047)."""

    LOCAL = "local"
    STAGING = "staging"
    PROD = "prod"


class EmailTransport(StrEnum):
    """SMTP locally (Mailpit), SES in staging and prod (ADR-0026)."""

    SMTP = "smtp"
    SES = "ses"


class Settings(BaseSettings):
    """Runtime configuration, resolved once at startup.

    Every secret is a `SecretStr` so it cannot be logged by accident: the repr
    of a `SecretStr` is `**********`, which means a stray `log.info(settings)`
    is inert rather than a SEC-019 incident.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    plane: Plane = Field(default=Plane.API, alias="BLUELAB_PLANE")
    environment: Environment = Field(default=Environment.LOCAL, alias="BLUELAB_ENV")

    # ── C-10 transactional store (ADR-0022) ───────────────────────────────────
    # The application role is non-superuser and does NOT hold BYPASSRLS; only the
    # migration role does (ADR-0031 decision 3, data/04 §1).
    database_url: SecretStr = Field(alias="DATABASE_URL")
    migration_database_url: SecretStr | None = Field(default=None, alias="MIGRATION_DATABASE_URL")

    # A transaction-mode pooler sits in front of Postgres, so the driver must not
    # keep its own prepared-statement cache or server-side state between
    # transactions (data/04 §8).
    db_pool_size: int = Field(default=10, ge=1, le=100, alias="DB_POOL_SIZE")
    db_pool_max_overflow: int = Field(default=5, ge=0, le=100, alias="DB_POOL_MAX_OVERFLOW")
    db_statement_timeout_ms: int = Field(default=10_000, ge=100, alias="DB_STATEMENT_TIMEOUT_MS")
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    # ── C-13 coordination store (ADR-0024) ────────────────────────────────────
    valkey_url: SecretStr = Field(alias="VALKEY_URL")

    # ── C-11 object store (ADR-0025) ──────────────────────────────────────────
    object_store_endpoint: str | None = Field(default=None, alias="OBJECT_STORE_ENDPOINT")
    object_store_bucket: str = Field(default="bluelab-local", alias="OBJECT_STORE_BUCKET")
    object_store_access_key: SecretStr | None = Field(default=None, alias="OBJECT_STORE_ACCESS_KEY")
    object_store_secret_key: SecretStr | None = Field(default=None, alias="OBJECT_STORE_SECRET_KEY")

    # ── C-14 email (ADR-0026) ─────────────────────────────────────────────────
    email_transport: EmailTransport = Field(default=EmailTransport.SMTP, alias="EMAIL_TRANSPORT")
    smtp_url: str | None = Field(default=None, alias="SMTP_URL")

    # ── the call-plane seam (ADR-0071) ────────────────────────────────────────
    # The one shared secret with bluelab-agent-prod.
    agent_hmac_secret: SecretStr = Field(alias="AGENT_HMAC_SECRET")

    # ── C-1 media transport (ADR-0014) ────────────────────────────────────────
    livekit_url: str | None = Field(default=None, alias="LIVEKIT_URL")
    livekit_api_key: SecretStr | None = Field(default=None, alias="LIVEKIT_API_KEY")
    livekit_api_secret: SecretStr | None = Field(default=None, alias="LIVEKIT_API_SECRET")

    # ── C-6 / C-7 model capabilities (ADR-0018) ───────────────────────────────
    # Turn-path keys (C-2/C-3/C-4) are deliberately absent: they belong to the
    # call plane, which is a separate deployment unit.
    evaluator_api_key: SecretStr | None = Field(default=None, alias="EVALUATOR_API_KEY")
    generation_api_key: SecretStr | None = Field(default=None, alias="GENERATION_API_KEY")

    # ── C-8 document extraction (ADR-0020) ────────────────────────────────────
    document_intelligence_endpoint: str | None = Field(
        default=None, alias="DOCUMENT_INTELLIGENCE_ENDPOINT"
    )
    document_intelligence_key: SecretStr | None = Field(
        default=None, alias="DOCUMENT_INTELLIGENCE_KEY"
    )

    # ── C-16 observability (ADR-0027) ─────────────────────────────────────────
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_service_name: str = Field(default="bluelab-backend", alias="OTEL_SERVICE_NAME")

    # ── session policy (security/04 §3; api/00 §3 fixes only the behaviour) ───
    session_idle_seconds: int = Field(default=12 * 3600, ge=60, alias="SESSION_IDLE_SECONDS")
    session_absolute_seconds: int = Field(
        default=7 * 24 * 3600, ge=300, alias="SESSION_ABSOLUTE_SECONDS"
    )

    # ── sign-in throttling (SEC-004/005, OWASP A07) ───────────────────────────
    #
    # Two counters over one window — see `platform.security.throttle`. The
    # identifier limit is what stops guessing at one account; the source limit is
    # what stops spraying across many, which is also the shape the argon2
    # memory-exhaustion attack takes.
    #
    # The identifier limit is deliberately not tiny. A counter small enough to
    # frustrate an attacker in one window is also small enough to let that
    # attacker lock a named user out on purpose, and a legitimate person retyping
    # a password they half-remember should not be collateral. Six failures in a
    # row is well past honest fumbling and far short of useful guessing against a
    # 12-character floor.
    auth_throttle_window_seconds: int = Field(
        default=900, ge=30, alias="AUTH_THROTTLE_WINDOW_SECONDS"
    )
    auth_throttle_identifier_attempts: int = Field(
        default=6, ge=1, alias="AUTH_THROTTLE_IDENTIFIER_ATTEMPTS"
    )
    # Higher, because one address legitimately carries a whole office behind NAT.
    auth_throttle_source_attempts: int = Field(
        default=60, ge=1, alias="AUTH_THROTTLE_SOURCE_ATTEMPTS"
    )

    # ── surface-wide request limit (api/05, SEC-004) ──────────────────────────
    #
    # Every contracted operation declares 429; this is what makes that true for
    # routes that have not thought about it. Generous on purpose — it is an abuse
    # ceiling, not a quota. A rep loading a dashboard fires several requests at
    # once, and a limit tight enough to shape normal use would be reported as a
    # bug long before it stopped anybody.
    #
    # The auth throttles are separate and much tighter: they ration argon2id, not
    # request volume.
    request_limit_per_window: int = Field(
        default=600, ge=1, alias="REQUEST_LIMIT_PER_WINDOW"
    )
    request_limit_window_seconds: int = Field(
        default=60, ge=1, alias="REQUEST_LIMIT_WINDOW_SECONDS"
    )

    # ── vendor fixture mode (infra/00 §3) ─────────────────────────────────────
    vendor_fixture_mode: bool = Field(default=False, alias="VENDOR_FIXTURE_MODE")

    @model_validator(mode="after")
    def _absolute_outlives_idle(self) -> Settings:
        """An absolute lifetime shorter than the idle window is a dead setting.

        The session store takes the *nearer* of the two clocks, so an absolute
        shorter than idle would make the idle policy unreachable — the sliding
        window would never get a chance to slide. Fail at startup rather than
        run with a policy nobody intended.
        """
        if self.session_absolute_seconds <= self.session_idle_seconds:
            raise ValueError(
                "SESSION_ABSOLUTE_SECONDS must exceed SESSION_IDLE_SECONDS "
                f"({self.session_absolute_seconds} <= {self.session_idle_seconds})"
            )
        return self

    @property
    def is_production(self) -> bool:
        """True in prod only. Used for fail-closed defaults, never for behaviour."""
        return self.environment is Environment.PROD

    @property
    def cookie_secure(self) -> bool:
        """`Secure` is mandatory everywhere the scheme allows it (SEC-002).

        Local development runs over http://localhost, which browsers treat as a
        secure context but which rejects the `Secure` attribute on some clients.
        This is the *only* environment-conditional value in the settings, and it
        is about the transport, not about product behaviour.
        """
        return self.environment is not Environment.LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Resolve settings once per process.

    Cached because `Settings` is frozen and reading the environment repeatedly
    would let a mutated environment change behaviour mid-process.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
