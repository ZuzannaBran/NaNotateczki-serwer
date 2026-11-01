"""WebSocket Origin policy for browsers and native account clients."""

import asyncio

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
