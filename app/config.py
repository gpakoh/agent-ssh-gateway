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
    session_timeout: int = Field(default=300, alias="SESSION_TIMEOUT")  # seconds
    cleanup_interval: int = Field(default=60, alias="CLEANUP_INTERVAL")  # seconds

    # SSH defaults
    ssh_default_port: int = 22
    ssh_default_timeout: int = 30  # seconds

    class Config:
        env_file = ".env"
        populate_by_name = True


settings = Settings()
