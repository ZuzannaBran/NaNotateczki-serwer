"""Verified backup listing must prioritize completed, durable snapshots."""

import asyncio
import uuid

from app.models import User
from app.routers.backups import list_backups


def test_latest_verified_backup_uses_verification_order():
    class Session:
        async def execute(self, statement):
            sql = str(statement).lower()
            assert "verified_at desc" in sql
            assert "created_at desc" in sql
            assert sql.index("verified_at desc") < sql.index("created_at desc")

            class Rows:
                def scalars(self):
                    return iter(())

            return Rows()

    user = User(id=uuid.uuid4(), username="tester", password_hash="hash")
    assert asyncio.run(list_backups(user=user, session=Session())) == {
        "backups": [],
    }
