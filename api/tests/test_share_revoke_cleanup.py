"""A failed private frame cleanup cannot undo committed sharing revocation."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.routers import study_shares


def test_end_share_notifies_guest_even_if_frame_unlink_fails(monkeypatch):
    share = SimpleNamespace(
        revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    published = []

    async def owner_share(*args, **kwargs):
        return share

    async def publish(channel, event):
        published.append(event)

    class FailingPath:
        def unlink(self, *, missing_ok=False):
            raise PermissionError("private frame directory unavailable")

    class Session:
        committed = False

        async def commit(self):
            self.committed = True

    monkeypatch.setattr(study_shares, "owner_share", owner_share)
    monkeypatch.setattr(study_shares.hub, "publish", publish)
    monkeypatch.setattr(
        study_shares, "frame_path", lambda share_id: FailingPath()
    )
    session = Session()
    response = asyncio.run(
        study_shares.end_share(
            uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), session
        )
    )
    assert response.status_code == 204
    assert session.committed
    assert share.revoked_at is not None
    assert published == [{"type": "ended"}]
