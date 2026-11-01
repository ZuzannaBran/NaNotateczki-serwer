import base64
import binascii
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


def validate_notebook_payload(value: dict[str, Any]) -> None:
    required = {"uid", "title", "kind", "folder", "createdAt", "updatedAt", "pages"}
    if not required.issubset(value):
        raise ValueError("notebook payload is incomplete")
    if (
        not isinstance(value["uid"], str)
        or not 1 <= len(value["uid"]) <= 128
        or not isinstance(value["title"], str)
        or not isinstance(value["folder"], str)
        or type(value["kind"]) is not int
        or not 0 <= value["kind"] <= 2
        or not isinstance(value["createdAt"], str)
        or not isinstance(value["updatedAt"], str)
        or not isinstance(value["pages"], list)
    ):
        raise ValueError("notebook metadata is invalid")
    page_ids: set[str] = set()
    for page in value["pages"]:
        if not isinstance(page, dict):
            raise ValueError("notebook page is invalid")
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id or page_id in page_ids:
            raise ValueError("missing or duplicate page ID")
        page_ids.add(page_id)
        for key in ("textBlocks", "imageBlocks"):
            items = page.get(key)
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise ValueError(f"invalid {key}")
        strokes = page.get("inkStrokes", [])
        if not isinstance(strokes, list) or any(
            not isinstance(stroke, dict) for stroke in strokes
        ):
            raise ValueError("invalid inkStrokes")
        for image in page["imageBlocks"]:
            encoded = image.get("bytes")
            if encoded is not None:
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError("invalid image bytes")
                try:
                    base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as error:
                    raise ValueError("invalid image base64") from error


class SyncWrite(BaseModel):
    expectedRevision: int = Field(ge=0)
    notebook: dict[str, Any]

    @field_validator("notebook")
    @classmethod
    def validate_notebook_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_notebook_payload(value)
        return value


class DeleteWrite(BaseModel):
    expectedRevision: int = Field(ge=1)


class BackupCreate(BaseModel):
    deviceId: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sizeBytes: int = Field(gt=0)
    formatVersion: Literal[2]
