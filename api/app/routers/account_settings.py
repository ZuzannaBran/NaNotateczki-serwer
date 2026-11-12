"""Versioned settings shared by every device belonging to an account."""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..account_protocol import require_legacy_protocol, require_v2_protocol
from ..database import database_session
from ..models import AccountSettings, User
from ..realtime_hub import hub
from ..security import current_user

Category = Literal[
    "appPreferences",
    "editorPreferences",
    "recentColors",
    "backupPreferences",
    "libraryState",
]

router = APIRouter(prefix="/v1/account-settings", tags=["account-settings"])
v2_router = APIRouter(prefix="/v2/account-settings", tags=["account-settings-v2"])
V2Category = Literal[
    "appPreferences", "editorPreferences", "recentColors", "backupPreferences",
]
MAX_SETTING_BYTES = 128 * 1024
BACKUP_IDLE_SECONDS = {15, 30, 60, 120, 300, 900, 1800}
APP_PREFERENCE_KEYS = {
    "deviceInputMode",
    "accentColor",
    "darkMode",
    "touchNavigationSensitivity",
    "uiScale",
}


class SettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expectedRevision: int = Field(ge=0)
    value: dict[str, Any] | list[int]


def validate_settings(category: Category, value: object) -> dict | list:
    if category == "recentColors":
        if not isinstance(value, list) or len(value) > 12:
            raise ValueError("Invalid recent colors.")
        if any(type(color) is not int or not 0 <= color <= 0xFFFFFFFF
               for color in value):
            raise ValueError("Invalid recent color value.")
    else:
        if not isinstance(value, dict):
            raise ValueError("Settings must be a JSON object.")
        if category == "libraryState" and (
            not isinstance(value.get("folders"), list)
            or not all(isinstance(folder, str) for folder in value["folders"])
            or not isinstance(value.get("covers"), dict)
            or not isinstance(value.get("plannerTasks"), dict)
            or not isinstance(value.get("recentCoverColors"), list)
        ):
            raise ValueError("Invalid account library state.")
        if category == "appPreferences" and (
            not set(value).issubset(APP_PREFERENCE_KEYS)
            or any(not isinstance(key, str) for key in value)
        ):
            raise ValueError("Unknown application preference.")
        if category == "backupPreferences" and value:
            if (set(value) != {"version", "idleSeconds"}
                or value["version"] != 1
                or type(value["idleSeconds"]) is not int
                or value["idleSeconds"] not in BACKUP_IDLE_SECONDS):
                raise ValueError("Invalid account backup preference.")
        if category == "editorPreferences":
            # Never accept authentication or other secret material.
            secret_names = {"password", "token", "secret", "credential"}
            if any(any(word in str(key).lower() for word in secret_names)
                   for key in value):
                raise ValueError("Secrets cannot be synchronized as preferences.")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("Settings are not valid JSON.") from error
    limit = 2 * 1024 * 1024 if category == "libraryState" else MAX_SETTING_BYTES
    if len(encoded) > limit:
        raise ValueError("Settings exceed the size limit.")
    return value


@router.get("")
async def get_account_settings(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    result = await session.execute(
        select(AccountSettings).where(AccountSettings.owner_id == user.id)
    )
    return {
        "settings": {
            entry.category: {"revision": entry.revision, "value": entry.content}
            for entry in result.scalars()
        }
    }


@router.put("/{category}")
async def put_account_settings(
    category: Category,
    body: SettingsWrite,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    return await _put_settings(
        category, body, user, session, require_legacy_protocol
    )


@v2_router.get("")
async def get_v2_account_settings(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    await require_v2_protocol(session, user.id)
    response = await get_account_settings(user, session)
    response["settings"].pop("libraryState", None)
    return response


@v2_router.put("/{category}")
async def put_v2_account_settings(
    category: V2Category,
    body: SettingsWrite,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    return await _put_settings(
        category, body, user, session, require_v2_protocol
    )


async def _put_settings(category, body, user, session, protocol_guard) -> dict:
    try:
        value = validate_settings(category, body.value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    # Lock the account row even when this category has not been created.
    # This serializes two first writes and makes the revision check atomic.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    await protocol_guard(session, user.id)
    record = await session.get(AccountSettings, (user.id, category))
    current_revision = record.revision if record is not None else 0
    if body.expectedRevision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "revision": current_revision,
                "value": record.content if record is not None else None,
            },
        )
    new_revision = current_revision + 1
    if record is None:
        session.add(AccountSettings(
            owner_id=user.id,
            category=category,
            revision=new_revision,
            content=value,
        ))
    else:
        record.content = value
        record.revision = new_revision
        record.updated_at = datetime.now(UTC)
    await session.commit()
    await hub.publish(
        user.id,
        {"type": "account_settings_updated", "category": category,
         "revision": new_revision},
    )
    return {"revision": new_revision, "value": value}
