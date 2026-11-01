import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..database import session_factory
from ..models import User
from ..realtime_hub import hub
from ..security import decode_access_token_claims
from ..settings import get_settings


router = APIRouter(tags=["realtime"])


@router.websocket("/v1/realtime")
async def realtime(websocket: WebSocket) -> None:
    settings = get_settings()
    # Browsers always send Origin, which must match the configured app.
    # Native Dart WebSocket clients may omit Origin; their bearer token
    # is still mandatory and validated before subscribing to account data.
    origin = websocket.headers.get("origin")
    if origin is not None and origin != str(settings.app_origin).rstrip("/"):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        message = json.loads(raw)
        if (
            not isinstance(message, dict)
            or message.get("type") != "authenticate"
            or not isinstance(message.get("token"), str)
        ):
            await websocket.close(code=4401)
            return
        user_id, expires_at, auth_version = decode_access_token_claims(
            message["token"]
        )
        async with session_factory() as session:
            user = await session.get(User, user_id)
            if user is None or user.auth_version != auth_version:
                await websocket.close(code=4401)
                return
        queue = await hub.subscribe(user_id)
        await websocket.send_json({"type": "authenticated"})
        try:
            while True:
                seconds_left = (expires_at - datetime.now(UTC)).total_seconds()
                if seconds_left <= 0:
                    await websocket.close(code=4401)
                    return
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=min(25, seconds_left)
                    )
                except TimeoutError:
                    event = {"type": "ping"}
                async with session_factory() as session:
                    user = await session.get(User, user_id)
                if user is None or user.auth_version != auth_version:
                    await websocket.close(code=4401)
                    return
                await websocket.send_json(event)
        finally:
            await hub.unsubscribe(user_id, queue)
    except (
        WebSocketDisconnect,
        asyncio.TimeoutError,
        json.JSONDecodeError,
        HTTPException,
    ):
        await websocket.close(code=4401)
