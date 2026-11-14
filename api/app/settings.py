import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True)

    database_url: str = Field(alias="DATABASE_URL")
    app_origin: HttpUrl = Field(alias="APP_ORIGIN")
    jwt_secret: str = Field(alias="JWT_SECRET", min_length=64)
    allow_registration: bool = Field(default=False, alias="ALLOW_REGISTRATION")
    allow_localhost_origins: bool = Field(default=False, alias="ALLOW_LOCALHOST_ORIGINS")
    data_path: Path = Field(default=Path("/data"), alias="DATA_PATH")
    access_token_minutes: int = 10
    # Remember this installation across long breaks; explicit sign-out,
    # password changes and server-side revocation still invalidate tokens.
    refresh_token_days: int = Field(default=3650, ge=1)
    issuer: str = "nanotateczki-api"
    maximum_backup_bytes: int = Field(
        default=64 * 1024 * 1024, alias="MAXIMUM_BACKUP_BYTES"
    )
    maximum_sync_bytes: int = 16 * 1024 * 1024
    maximum_asset_bytes: int = Field(
        default=64 * 1024 * 1024, alias="MAXIMUM_ASSET_BYTES", ge=1
    )
    operations_v2_write_mode: Literal["disabled", "canary", "all"] = Field(
        default="disabled", alias="OPERATIONS_V2_WRITE_MODE"
    )
    operations_v2_canary_owner_ids: str = Field(
        default="", alias="OPERATIONS_V2_CANARY_OWNER_IDS"
    )

    @field_validator("operations_v2_canary_owner_ids")
    @classmethod
    def validate_canary_owner_ids(cls, value: str) -> str:
        ids = [part.strip() for part in value.split(",") if part.strip()]
        try:
            if any(str(uuid.UUID(item)) != item.lower() for item in ids):
                raise ValueError("Non-canonical UUID")
        except ValueError as exc:
            raise ValueError("Canary owner IDs must be UUIDs") from exc
        return ",".join(ids)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL with asyncpg")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
