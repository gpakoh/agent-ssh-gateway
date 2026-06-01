"""Application configuration."""

from datetime import UTC, datetime, timedelta

from pydantic import Field
from pydantic_settings import BaseSettings


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
    ssh_strict_host_key_checking: bool = Field(
        default=True, alias="SSH_STRICT_HOST_KEY_CHECKING"
    )
    known_hosts_store: str = Field(default="", alias="KNOWN_HOSTS_STORE")
    known_hosts_file: str = Field(default="/app/known_hosts", alias="KNOWN_HOSTS_FILE")

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
    command_policy_mode: str = Field(default="audit", alias="COMMAND_POLICY_MODE")
    command_policy_profile: str = Field(default="default", alias="COMMAND_POLICY_PROFILE")
    agent_token: str = Field(default="", alias="AGENT_TOKEN")
    agent_token_ttl: int = Field(default=3600, alias="AGENT_TOKEN_TTL")
    agent_token_expires_at: datetime | None = None
    agent_token_scopes: list[str] = Field(default=[], alias="AGENT_TOKEN_SCOPES")
    allowed_client_cidrs: str = Field(
        default="127.0.0.1/32,::1/128,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12",
        alias="ALLOWED_CLIENT_CIDRS",
    )
    trusted_proxy_cidrs: str = Field(
        default="127.0.0.1/32,172.16.0.0/12",
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
    max_sessions_per_ip: int = Field(default=10, alias="MAX_SESSIONS_PER_IP")
    rate_limit_requests: int = Field(default=100, alias="RATE_LIMIT_REQUESTS")
    rate_limit_window: int = Field(default=60, alias="RATE_LIMIT_WINDOW")

    # Redis
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    redis_job_queue_enabled: bool = Field(
        default=False, alias="REDIS_JOB_QUEUE_ENABLED"
    )

    # PostgreSQL
    database_url: str = Field(default="", alias="DATABASE_URL")
    persistent_sessions_enabled: bool = Field(
        default=False, alias="PERSISTENT_SESSIONS_ENABLED"
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
    event_hooks_retention_dead_days: int = Field(default=30, alias="EVENT_HOOKS_RETENTION_DEAD_DAYS")

    class Config:
        env_file = ".env"
        populate_by_name = True

    def model_post_init(self, __context) -> None:
        if self.agent_token and self.agent_token_expires_at is None:
            self.agent_token_expires_at = datetime.now(UTC) + timedelta(
                seconds=self.agent_token_ttl
            )


settings = Settings()
