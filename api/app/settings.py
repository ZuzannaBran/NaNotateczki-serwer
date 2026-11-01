from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True)

    database_url: str = Field(alias="DATABASE_URL")
    app_origin: HttpUrl = Field(alias="APP_ORIGIN")
    jwt_secret: str = Field(alias="JWT_SECRET", min_length=64)
    allow_registration: bool = Field(default=False, alias="ALLOW_REGISTRATION")
    data_path: Path = Field(default=Path("/data"), alias="DATA_PATH")
    access_token_minutes: int = 10
    refresh_token_days: int = 30
    issuer: str = "nanotateczki-api"
    maximum_backup_bytes: int = Field(
        default=64 * 1024 * 1024, alias="MAXIMUM_BACKUP_BYTES"
    )
    maximum_sync_bytes: int = 16 * 1024 * 1024

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL with asyncpg")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
