"""Application configuration."""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    uvicorn_host: str = Field(default="0.0.0.0", alias="UVICORN_HOST")
    uvicorn_port: int = Field(default=8085, alias="UVICORN_PORT")

    # CORS
    cors_origins: list[str] = Field(
        default_factory=lambda: ["https://ssh.xloud.ru"],
        alias="CORS_ORIGINS"
    )

    # Session management
    session_timeout: int = Field(default=3600, alias="SESSION_TIMEOUT")  # 1 hour
    cleanup_interval: int = Field(default=300, alias="CLEANUP_INTERVAL")  # 5 min

    # SSH defaults
    ssh_default_port: int = 22
    ssh_default_timeout: int = 120  # seconds

    # Security
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")
    max_sessions_per_ip: int = Field(default=10, alias="MAX_SESSIONS_PER_IP")
    rate_limit_requests: int = Field(default=100, alias="RATE_LIMIT_REQUESTS")
    rate_limit_window: int = Field(default=60, alias="RATE_LIMIT_WINDOW")

    class Config:
        env_file = ".env"
        populate_by_name = True


settings = Settings()
