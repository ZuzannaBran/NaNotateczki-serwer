from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-z0-9_]+$")
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class RegistrationCredentials(Credentials):
    password: str = Field(min_length=8, max_length=128)


class RefreshRequest(BaseModel):
    refreshToken: str = Field(min_length=40, max_length=256)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currentPassword: str = Field(min_length=1, max_length=256)
    newPassword: str = Field(min_length=8, max_length=128)


class SyncWrite(BaseModel):
    expectedRevision: int = Field(ge=0)
    notebook: dict[str, Any]

    @field_validator("notebook")
    @classmethod
    def validate_notebook_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        required = {"uid", "title", "kind", "folder", "createdAt", "updatedAt", "pages"}
        if not required.issubset(value) or not isinstance(value.get("pages"), list):
            raise ValueError("notebook payload is incomplete")
        return value


class DeleteWrite(BaseModel):
    expectedRevision: int = Field(ge=1)


class BackupCreate(BaseModel):
    deviceId: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sizeBytes: int = Field(gt=0)
    formatVersion: Literal[2]
