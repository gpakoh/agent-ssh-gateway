"""Application configuration."""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    uvicorn_host: str = Field(default="0.0.0.0", alias="UVICORN_HOST")
    uvicorn_port: int = Field(default=8085, alias="UVICORN_PORT")

    # CORS
    cors_origins: list[str] = Field(
        default_factory=lambda: ["https://ssh.xloud.ru"], alias="CORS_ORIGINS"
    )

    # Session management
    session_timeout: int = Field(default=3600, alias="SESSION_TIMEOUT")  # 1 hour
    cleanup_interval: int = Field(default=300, alias="CLEANUP_INTERVAL")  # 5 min

    # SSH defaults
    ssh_default_port: int = 22
    ssh_default_timeout: int = 120  # seconds

    # Security
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")
    api_key: str = Field(default="", alias="API_KEY")
    api_auth_enabled: bool = Field(default=False, alias="API_AUTH_ENABLED")
    allowed_client_cidrs: str = Field(
        default="127.0.0.1/32,::1/128,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12",
        alias="ALLOWED_CLIENT_CIDRS",
    )
    trusted_proxy_cidrs: str = Field(
        default="127.0.0.1/32,172.16.0.0/12",
        alias="TRUSTED_PROXY_CIDRS",
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

    class Config:
        env_file = ".env"
        populate_by_name = True


settings = Settings()
