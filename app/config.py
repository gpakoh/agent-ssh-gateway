"""Application configuration."""

from datetime import UTC, datetime, timedelta

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    uvicorn_host: str = Field(default="0.0.0.0", alias="UVICORN_HOST")
    uvicorn_port: int = Field(default=8085, alias="UVICORN_PORT")

    # CORS
    cors_origins: list[str] = Field(
        default_factory=lambda: ["https://gateway.example.com"], alias="CORS_ORIGINS"
    )

    # Session management
    session_timeout: int = Field(default=3600, alias="SESSION_TIMEOUT")  # 1 hour
    cleanup_interval: int = Field(default=300, alias="CLEANUP_INTERVAL")  # 5 min

    # SSH defaults
    ssh_default_port: int = 22
    ssh_default_timeout: int = 120  # seconds
    ssh_strict_host_key_checking: bool = Field(default=True, alias="SSH_STRICT_HOST_KEY_CHECKING")
    known_hosts_store: str = Field(default="", alias="KNOWN_HOSTS_STORE")
    known_hosts_file: str = Field(default="known_hosts", alias="KNOWN_HOSTS_FILE")

    # SSH connection pooling (optional; 0 = disabled)
    ssh_connection_pool_size: int = Field(default=0, alias="SSH_CONNECTION_POOL_SIZE")
    ssh_connection_pool_ttl_seconds: int = Field(
        default=60, alias="SSH_CONNECTION_POOL_TTL_SECONDS"
    )

    server_default_configs: str = Field(
        default="{}",
        alias="SERVER_DEFAULT_CONFIGS",
        description="JSON object with default server connection presets",
    )

    # Security
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")
    api_key: str = Field(default="", alias="API_KEY")
    api_auth_enabled: bool = Field(default=True, alias="API_AUTH_ENABLED")
    ssh_key_upload_enabled: bool = Field(default=False, alias="SSH_KEY_UPLOAD_ENABLED")
    ssh_key_upload_dir: str = Field(default="./ssh_keys", alias="SSH_KEY_UPLOAD_DIR")
    workspace_readonly: bool = Field(default=True, alias="WORKSPACE_READONLY")
    command_policy_mode: str = Field(default="enforce", alias="COMMAND_POLICY_MODE")
    command_policy_profile: str = Field(default="default", alias="COMMAND_POLICY_PROFILE")
    command_policy_key_profiles: str = Field(
        default="{}",
        alias="COMMAND_POLICY_KEY_PROFILES",
        description="JSON mapping API key fingerprint → profile name (e.g. {\"abc123\": \"testlint\"})",
    )
    command_policy_agent_modes: str = Field(
        default="{}",
        alias="COMMAND_POLICY_AGENT_MODES",
        description="JSON mapping agent name → policy mode (e.g. {\"chatgpt\":\"enforce\",\"claude-code\":\"audit\"})",
    )
    command_output_redaction_enabled: bool = Field(
        default=True, alias="COMMAND_OUTPUT_REDACTION_ENABLED"
    )
    # Web UI Auth
    jwt_secret: str = Field(default="", alias="JWT_SECRET")
    jwt_expires_minutes: int = Field(default=1440, alias="JWT_EXPIRES_MINUTES")
    jwt_cookie_secure: bool = Field(default=False, alias="JWT_COOKIE_SECURE")
    auth_db_path: str = Field(default="/app/data/auth.sqlite3", alias="AUTH_DB_PATH")
    setup_token: str = Field(
        default="",
        alias="SETUP_TOKEN",
        description=(
            "One-time token required to create the first admin account. "
            "Empty = first-admin registration is disabled (fail-closed)."
        ),
    )

    # OAuth2/SSO (optional; "" = disabled)
    oauth_provider: str = Field(
        default="",
        alias="OAUTH_PROVIDER",
        description="OAuth provider: github, gitlab, google, or oidc (generic OIDC). Empty disables SSO.",
    )
    oauth_client_id: str = Field(default="", alias="OAUTH_CLIENT_ID")
    oauth_client_secret: str = Field(default="", alias="OAUTH_CLIENT_SECRET")
    oauth_issuer_url: str = Field(
        default="",
        alias="OAUTH_ISSUER_URL",
        description="OIDC issuer URL for provider=oidc (auto-discovery of /.well-known/openid-configuration)",
    )
    oauth_redirect_uri: str = Field(
        default="",
        alias="OAUTH_REDIRECT_URI",
        description="Public callback URL (e.g. https://gateway.example.com/api/auth/oauth/callback). Empty = auto-detect from request.",
    )
    oauth_allowed_emails: str = Field(
        default="",
        alias="OAUTH_ALLOWED_EMAILS",
        description=(
            "Comma-separated allowlist of SSO emails. "
            "Empty = deny all SSO sign-ins (fail-closed; must be set to enable SSO)."
        ),
    )

    @property
    def jwt_secret_required(self) -> str:
        if not self.jwt_secret:
            raise RuntimeError(
                "JWT_SECRET must be configured when Web UI auth is enabled. "
                'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
            )
        return self.jwt_secret

    agent_token: str = Field(default="", alias="AGENT_TOKEN")
    agent_token_ttl: int = Field(default=3600, alias="AGENT_TOKEN_TTL")
    agent_token_expires_at: datetime | None = None
    agent_token_scopes: list[str] = Field(default=[], alias="AGENT_TOKEN_SCOPES")
    allowed_client_cidrs: str = Field(
        default="127.0.0.1/32,::1/128,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12",
        alias="ALLOWED_CLIENT_CIDRS",
    )
    trusted_proxy_cidrs: str = Field(
        default="127.0.0.1/32,::1/128",
        alias="TRUSTED_PROXY_CIDRS",
    )
    allowed_target_cidrs: str = Field(
        default="10.0.0.0/8,192.168.0.0/16,172.16.0.0/12",
        alias="ALLOWED_TARGET_CIDRS",
    )
    denied_target_cidrs: str = Field(
        default="127.0.0.0/8,::1/128,169.254.0.0/16,0.0.0.0/8,224.0.0.0/4",
        alias="DENIED_TARGET_CIDRS",
    )
    ssh_health_user: str = Field(default="", alias="SSH_HEALTH_USER")
    ssh_health_password: str = Field(default="", alias="SSH_HEALTH_PASSWORD")

    max_sessions_per_ip: int = Field(default=10, alias="MAX_SESSIONS_PER_IP")
    rate_limit_requests: int = Field(default=100, alias="RATE_LIMIT_REQUESTS")
    rate_limit_window: int = Field(default=60, alias="RATE_LIMIT_WINDOW")

    # Redis
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    redis_job_queue_enabled: bool = Field(default=False, alias="REDIS_JOB_QUEUE_ENABLED")

    # PostgreSQL
    database_url: str = Field(default="", alias="DATABASE_URL")
    persistent_sessions_enabled: bool = Field(default=False, alias="PERSISTENT_SESSIONS_ENABLED")

    # Audit trail
    audit_log_path: str = Field(default="./data/audit/events.jsonl", alias="AUDIT_LOG_PATH")
    audit_recent_limit: int = Field(default=500, alias="AUDIT_RECENT_LIMIT")
    audit_log_persist_enabled: bool = Field(
        default=False, alias="AUDIT_LOG_PERSIST_ENABLED"
    )
    audit_log_retention_days: int = Field(
        default=90, alias="AUDIT_LOG_RETENTION_DAYS"
    )
    audit_log_cleanup_interval_seconds: int = Field(
        default=3600, alias="AUDIT_LOG_CLEANUP_INTERVAL_SECONDS"
    )

    event_hooks_enabled: bool = Field(default=False, alias="EVENT_HOOKS_ENABLED")
    event_hooks_max: int = Field(default=50, alias="EVENT_HOOKS_MAX")
    event_hooks_timeout_connect: float = Field(default=5.0, alias="EVENT_HOOKS_TIMEOUT_CONNECT")
    event_hooks_timeout_read: float = Field(default=10.0, alias="EVENT_HOOKS_TIMEOUT_READ")
    event_hooks_max_attempts: int = Field(default=5, alias="EVENT_HOOKS_MAX_ATTEMPTS")
    event_hooks_retry_base_sec: float = Field(default=2.0, alias="EVENT_HOOKS_RETRY_BASE_SEC")
    event_hooks_retry_max_sec: float = Field(default=300.0, alias="EVENT_HOOKS_RETRY_MAX_SEC")
    event_hooks_max_output_bytes: int = Field(default=65536, alias="EVENT_HOOKS_MAX_OUTPUT_BYTES")
    event_hooks_allow_http: bool = Field(default=False, alias="EVENT_HOOKS_ALLOW_HTTP")
    event_hooks_poll_interval: float = Field(default=5.0, alias="EVENT_HOOKS_POLL_INTERVAL")
    event_hooks_lease_ttl: float = Field(default=30.0, alias="EVENT_HOOKS_LEASE_TTL")
    event_hooks_retention_sent_days: int = Field(default=7, alias="EVENT_HOOKS_RETENTION_SENT_DAYS")
    event_hooks_retention_dead_days: int = Field(
        default=30, alias="EVENT_HOOKS_RETENTION_DEAD_DAYS"
    )

    # Access Control Gate (Phase 12B)
    access_control_enabled: bool = Field(default=True, alias="ACCESS_CONTROL_ENABLED")
    access_control_enforce_master: bool = Field(
        default=False, alias="ACCESS_CONTROL_ENFORCE_MASTER"
    )
    access_control_pending_ttl: int = Field(default=900, alias="ACCESS_CONTROL_PENDING_TTL")
    access_control_allow_ttl: int = Field(default=86400, alias="ACCESS_CONTROL_ALLOW_TTL")
    access_control_deny_ttl: int = Field(default=86400, alias="ACCESS_CONTROL_DENY_TTL")
    access_control_redis_url: str | None = Field(
        default=None, alias="ACCESS_CONTROL_REDIS_URL"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        populate_by_name=True,
        extra="ignore",
    )

    def model_post_init(self, __context) -> None:
        if self.agent_token and self.agent_token_expires_at is None and self.agent_token_ttl > 0:
            self.agent_token_expires_at = datetime.now(UTC) + timedelta(
                seconds=self.agent_token_ttl
            )

    @model_validator(mode="after")
    def _reject_placeholder_secrets(self) -> "Settings":
        """T79.13: refuse to boot with .env.example placeholder secrets."""
        placeholders = {
            "API_KEY": self.api_key,
            "AGENT_TOKEN": self.agent_token,
            "ENCRYPTION_KEY": self.encryption_key,
            "JWT_SECRET": self.jwt_secret,
            "SETUP_TOKEN": self.setup_token,
        }
        offenders = [name for name, val in placeholders.items() if "change-me" in val]
        if offenders:
            raise ValueError(
                f"Placeholder secret values in use for: {', '.join(sorted(offenders))}. "
                "Set real secrets in your environment before starting the gateway."
            )
        return self


settings = Settings()
