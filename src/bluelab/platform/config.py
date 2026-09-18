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
from urllib.parse import urlsplit

from pydantic import EmailStr, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bluelab.platform.queue.catalog import Lane


class Plane(StrEnum):
    """Which plane this process is. Selects the entrypoint, nothing else."""

    API = "api"
    WORKER = "worker"


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
    object_store_region: str = Field(default="eu-south-1", alias="OBJECT_STORE_REGION")
    object_store_access_key: SecretStr | None = Field(default=None, alias="OBJECT_STORE_ACCESS_KEY")
    object_store_secret_key: SecretStr | None = Field(default=None, alias="OBJECT_STORE_SECRET_KEY")
    object_presign_seconds: int = Field(
        default=300, ge=1, le=300, alias="OBJECT_PRESIGN_SECONDS"
    )
    erasure_ledger_bucket: str = Field(
        default="bluelab-erasure-ledger", alias="ERASURE_LEDGER_BUCKET"
    )
    lifecycle_history_bucket: str = Field(
        default="bluelab-lifecycle-history", alias="LIFECYCLE_HISTORY_BUCKET"
    )
    lifecycle_history_backup_bucket: str = Field(
        default="bluelab-lifecycle-history-backup",
        alias="LIFECYCLE_HISTORY_BACKUP_BUCKET",
    )
    lifecycle_history_kms_key_id: str | None = Field(
        default=None, alias="LIFECYCLE_HISTORY_KMS_KEY_ID"
    )
    erasure_backup_ledger_bucket: str = Field(
        default="bluelab-erasure-ledger-backup",
        alias="ERASURE_BACKUP_LEDGER_BUCKET",
    )
    erasure_backup_endpoint: str | None = Field(
        default=None, alias="ERASURE_BACKUP_ENDPOINT"
    )
    erasure_backup_region: str | None = Field(
        default=None, alias="ERASURE_BACKUP_REGION"
    )
    erasure_backup_access_key: SecretStr | None = Field(
        default=None, alias="ERASURE_BACKUP_ACCESS_KEY"
    )
    erasure_backup_secret_key: SecretStr | None = Field(
        default=None, alias="ERASURE_BACKUP_SECRET_KEY"
    )
    erasure_ledger_kms_key_id: str | None = Field(
        default=None, alias="ERASURE_LEDGER_KMS_KEY_ID"
    )

    # ── shared external-call bounds (architecture/04 §2) ─────────────────────
    dependency_timeout_seconds: float = Field(
        default=5.0, gt=0, le=30, alias="DEPENDENCY_TIMEOUT_SECONDS"
    )
    dependency_max_attempts: int = Field(
        default=3, ge=1, le=10, alias="DEPENDENCY_MAX_ATTEMPTS"
    )
    dependency_backoff_base_seconds: float = Field(
        default=0.1, ge=0, le=10, alias="DEPENDENCY_BACKOFF_BASE_SECONDS"
    )
    dependency_backoff_max_seconds: float = Field(
        default=1.0, ge=0, le=30, alias="DEPENDENCY_BACKOFF_MAX_SECONDS"
    )
    dependency_circuit_failure_threshold: int = Field(
        default=5, ge=1, le=100, alias="DEPENDENCY_CIRCUIT_FAILURE_THRESHOLD"
    )
    dependency_circuit_recovery_seconds: float = Field(
        default=30.0, gt=0, le=3600, alias="DEPENDENCY_CIRCUIT_RECOVERY_SECONDS"
    )

    # ── work plane (ADR-0023; infra/02 §4) ───────────────────────────────────
    # Deployments select lanes independently, which is how per-type concurrency
    # is configured without changing the common application image.
    worker_queues: str = Field(default="dispatch_email,maintenance", alias="WORKER_QUEUES")
    worker_concurrency: int = Field(default=10, ge=1, le=100, alias="WORKER_CONCURRENCY")
    worker_compatibility_delay_seconds: int = Field(
        default=5, ge=1, le=300, alias="WORKER_COMPATIBILITY_DELAY_SECONDS"
    )

    # ── C-14 email (ADR-0026) ─────────────────────────────────────────────────
    email_transport: EmailTransport = Field(default=EmailTransport.SMTP, alias="EMAIL_TRANSPORT")
    email_sender: EmailStr = Field(default="no-reply@example.com", alias="EMAIL_SENDER")
    email_region: str = Field(default="eu-south-1", alias="EMAIL_REGION")
    email_sns_topic_arn: str | None = Field(default=None, alias="EMAIL_SNS_TOPIC_ARN")
    smtp_url: SecretStr | None = Field(
        default=SecretStr("smtp://localhost:1025"), alias="SMTP_URL"
    )

    # The same-origin SPA URL used only to construct E-1 links. Paths and secret
    # fragments are supplied by the closed template, never by configuration.
    public_app_url: str = Field(
        default="https://app.example.com", alias="PUBLIC_APP_URL"
    )

    # E-1's initial credential/reset token exists only long enough for the worker
    # to render the message. Local uses one AES-256 key; deployed environments
    # name a KMS key whose IAM grants split encrypt (API) from decrypt (worker).
    email_delivery_local_key: SecretStr | None = Field(
        default=None, alias="EMAIL_DELIVERY_LOCAL_KEY"
    )
    email_delivery_kms_key_id: str | None = Field(
        default=None, alias="EMAIL_DELIVERY_KMS_KEY_ID"
    )

    # Operations TOTP seeds use a separate envelope key. The API has decrypt
    # capability only on the operations authentication path.
    ops_totp_local_key: SecretStr | None = Field(default=None, alias="OPS_TOTP_LOCAL_KEY")
    ops_totp_kms_key_id: str | None = Field(default=None, alias="OPS_TOTP_KMS_KEY_ID")

    # ── the call-plane seam (ADR-0071) ────────────────────────────────────────
    # The one shared secret with bluelab-agent-prod.
    agent_hmac_secret: SecretStr = Field(alias="AGENT_HMAC_SECRET")

    # ── C-1 media transport (ADR-0014) ────────────────────────────────────────
    livekit_url: str | None = Field(default=None, alias="LIVEKIT_URL")
    livekit_api_key: SecretStr | None = Field(default=None, alias="LIVEKIT_API_KEY")
    livekit_api_secret: SecretStr | None = Field(default=None, alias="LIVEKIT_API_SECRET")
    livekit_token_revocation_supported: bool = Field(
        default=False, alias="LIVEKIT_TOKEN_REVOCATION_SUPPORTED"
    )
    org_purge_enabled: bool = Field(default=False, alias="ORG_PURGE_ENABLED")
    call_capacity: int = Field(default=10, ge=1, alias="CALL_CAPACITY")
    call_capacity_retry_seconds: int = Field(default=15, ge=1, le=300, alias="CALL_CAPACITY_RETRY_SECONDS")
    call_lease_seconds: int = Field(default=960, ge=930, le=1800, alias="CALL_LEASE_SECONDS")

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
        default=5, ge=1, alias="AUTH_THROTTLE_IDENTIFIER_ATTEMPTS"
    )
    auth_backoff_base_seconds: int = Field(
        default=1, ge=1, alias="AUTH_BACKOFF_BASE_SECONDS"
    )
    auth_backoff_max_seconds: int = Field(
        default=300, ge=1, alias="AUTH_BACKOFF_MAX_SECONDS"
    )
    # The source window is independently fixed at the contract's 10/minute.
    auth_throttle_source_attempts: int = Field(
        default=10, ge=1, alias="AUTH_THROTTLE_SOURCE_ATTEMPTS"
    )
    auth_throttle_source_window_seconds: int = Field(
        default=60, ge=1, alias="AUTH_THROTTLE_SOURCE_WINDOW_SECONDS"
    )

    password_reset_request_window_seconds: int = Field(
        default=3600, ge=1, alias="PASSWORD_RESET_REQUEST_WINDOW_SECONDS"
    )
    password_reset_request_identifier_attempts: int = Field(
        default=3, ge=1, alias="PASSWORD_RESET_REQUEST_IDENTIFIER_ATTEMPTS"
    )
    password_reset_request_source_attempts: int = Field(
        default=10, ge=1, alias="PASSWORD_RESET_REQUEST_SOURCE_ATTEMPTS"
    )
    password_reset_complete_window_seconds: int = Field(
        default=3600, ge=1, alias="PASSWORD_RESET_COMPLETE_WINDOW_SECONDS"
    )
    password_reset_complete_source_attempts: int = Field(
        default=10, ge=1, alias="PASSWORD_RESET_COMPLETE_SOURCE_ATTEMPTS"
    )

    ops_throttle_attempts: int = Field(default=5, ge=1, alias="OPS_THROTTLE_ATTEMPTS")
    ops_throttle_window_seconds: int = Field(
        default=60, ge=1, alias="OPS_THROTTLE_WINDOW_SECONDS"
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
        if self.dependency_backoff_max_seconds < self.dependency_backoff_base_seconds:
            raise ValueError(
                "DEPENDENCY_BACKOFF_MAX_SECONDS must be at least "
                "DEPENDENCY_BACKOFF_BASE_SECONDS"
            )
        if (self.object_store_access_key is None) != (
            self.object_store_secret_key is None
        ):
            raise ValueError(
                "OBJECT_STORE_ACCESS_KEY and OBJECT_STORE_SECRET_KEY must be set together"
            )
        if (self.erasure_backup_access_key is None) != (
            self.erasure_backup_secret_key is None
        ):
            raise ValueError(
                "ERASURE_BACKUP_ACCESS_KEY and ERASURE_BACKUP_SECRET_KEY must be set together"
            )
        if self.org_purge_enabled and (
            not self.livekit_token_revocation_supported
            or self.lifecycle_history_kms_key_id is None
        ):
            raise ValueError(
                "ORG_PURGE_ENABLED requires provider token revocation and "
                "encrypted independent lifecycle evidence"
            )
        app_url = urlsplit(self.public_app_url)
        if (
            app_url.scheme not in {"http", "https"}
            or not app_url.hostname
            or app_url.username is not None
            or app_url.password is not None
            or app_url.query
            or app_url.fragment
            or app_url.path not in {"", "/"}
        ):
            raise ValueError("PUBLIC_APP_URL must be a bare http(s) origin")
        if self.environment is not Environment.LOCAL and app_url.scheme != "https":
            raise ValueError("PUBLIC_APP_URL must use https outside local development")
        if self.email_transport is EmailTransport.SMTP:
            if self.smtp_url is None:
                raise ValueError("SMTP_URL is required for the SMTP email transport")
            smtp_url = urlsplit(self.smtp_url.get_secret_value())
            if smtp_url.scheme not in {"smtp", "smtps"} or not smtp_url.hostname:
                raise ValueError("SMTP_URL must use smtp or smtps and include a host")
        # Parse here so an unknown lane fails process startup instead of becoming
        # a Procrastinate TaskNotFound failure after a job has been claimed.
        _ = self.worker_queue_names
        return self

    @property
    def worker_queue_names(self) -> tuple[str, ...]:
        """The configured, de-duplicated lanes in stable order."""
        raw_names = [item.strip() for item in self.worker_queues.split(",")]
        if not raw_names or any(not item for item in raw_names):
            raise ValueError("WORKER_QUEUES must name at least one queue")
        try:
            lanes = tuple(
                "maintenance" if item == "maintenance" else Lane(item).value
                for item in raw_names
            )
        except ValueError as exc:
            raise ValueError("WORKER_QUEUES contains an unknown queue") from exc
        return tuple(dict.fromkeys(lanes))

    @property
    def is_production(self) -> bool:
        """True in prod only. Used for fail-closed defaults, never for behaviour."""
        return self.environment is Environment.PROD

    @property
    def cookie_secure(self) -> bool:
        """Session cookies always satisfy the `__Host-` prefix contract (SEC-002).

        Loopback origins are potentially trustworthy browser contexts. Keeping
        `Secure` in local development preserves the deployed cookie shape and,
        critically, avoids constructing an invalid `__Host-` cookie.
        """
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Resolve settings once per process.

    Cached because `Settings` is frozen and reading the environment repeatedly
    would let a mutated environment change behaviour mid-process.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
