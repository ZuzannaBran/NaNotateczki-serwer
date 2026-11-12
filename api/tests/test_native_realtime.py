"""WebSocket Origin policy for browsers and native account clients."""

import asyncio
import uuid
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

from app.routers import operation_realtime
from app.routers.realtime import realtime
from app.settings import get_settings


class StubWebSocket:
    def __init__(self, origin=None):
        self.headers = {} if origin is None else {"origin": origin}
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        return '{"type":"invalid-authentication"}'

    async def close(self, code):
        self.closed = code


def test_originless_native_client_reaches_token_auth():
    socket = StubWebSocket()
    asyncio.run(realtime(socket))
    assert socket.accepted
    assert socket.closed == 4401  # Bearer JWT remains required.


def test_unknown_browser_origin_is_denied_before_auth():
    socket = StubWebSocket("https://attacker.invalid")
    asyncio.run(realtime(socket))
    assert not socket.accepted
    assert socket.closed == 4403


def test_configured_browser_origin_still_works():
    socket = StubWebSocket(str(get_settings().app_origin).rstrip("/"))
    asyncio.run(realtime(socket))
    assert socket.accepted
    assert socket.closed == 4401

class DisconnectV2WebSocket:
    def __init__(self):
        self.headers = {}
        self.accepted = False
        self.messages = []
        self.closed_codes = []
        self.receives = 0

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        self.receives += 1
        if self.receives == 1:
            return '{"type": "authenticate", "token": "test-token"}'
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, code):
        self.closed_codes.append(code)


def test_v2_normal_disconnect_releases_subscription_without_auth_close(
    monkeypatch,
):
    owner = uuid.uuid4()
    socket = DisconnectV2WebSocket()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def authenticate(*_):
        return SimpleNamespace(owner_id=owner, share_id=None)

    monkeypatch.setattr(
        operation_realtime, "session_factory", Session,
    )
    monkeypatch.setattr(
        operation_realtime, "authorize", authenticate,
    )

    asyncio.run(operation_realtime.watch_operations(socket))

    assert socket.accepted
    assert socket.messages == [{"type": "authenticated"}]
    assert socket.receives == 2
    assert socket.closed_codes == []
    assert not operation_realtime.hub._queues.get(owner)


class GracefulV2WebSocket(DisconnectV2WebSocket):
    def __init__(self):
        super().__init__()
        self.incoming = [
            '{"type": "authenticate", "token": "test-token"}',
            '{"type": "unsubscribe", "unexpected": true}',
            '{"type": "unsubscribe"}',
        ]

    async def receive_text(self):
        self.receives += 1
        return self.incoming.pop(0)


def test_v2_graceful_unsubscribe_acknowledges_and_cleans_subscription(
    monkeypatch,
):
    """Logout must not depend on TestClient transport-close timing."""
    owner = uuid.uuid4()
    socket = GracefulV2WebSocket()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def authenticate(*_):
        return SimpleNamespace(owner_id=owner, share_id=None)

    monkeypatch.setattr(operation_realtime, "session_factory", Session)
    monkeypatch.setattr(operation_realtime, "authorize", authenticate)

    asyncio.run(operation_realtime.watch_operations(socket))

    assert socket.accepted
    assert socket.messages == [
        {"type": "authenticated"}, {"type": "unsubscribed"},
    ]
    assert socket.receives == 3
    assert socket.closed_codes == []
    assert not operation_realtime.hub._queues.get(owner)
