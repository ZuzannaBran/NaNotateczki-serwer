"""Account-scoped rollout gate shared by all v2 mutation endpoints."""
import uuid

from fastapi import HTTPException

from .settings import get_settings


def require_operations_v2_write(owner_id: uuid.UUID) -> None:
    settings = get_settings()
    mode = settings.operations_v2_write_mode
    if mode == "all":
        return
    if mode == "canary" and str(owner_id) in (
        settings.operations_v2_canary_owner_ids.split(",")
    ):
        return
    raise HTTPException(
        status_code=403,
        detail="Operation sync v2 is not enabled for this account.",
    )
