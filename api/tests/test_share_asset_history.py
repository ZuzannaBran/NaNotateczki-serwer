"""Guest replays can fetch removed historical media without global access."""

import uuid

from sqlalchemy.dialects import postgresql

from app.routers.assets import historical_share_asset_query


def test_historical_asset_scope_is_owner_document_and_digest_bound():
    owner = uuid.UUID("11111111-1111-4111-8111-111111111111")
    digest = "a" * 64
    query = historical_share_asset_query(owner, "shared-document", digest)
    sql = str(query.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert "document_operations" in sql
    assert "owner_id" in sql
    assert str(owner) in sql
    assert "document_uid" in sql
    assert "shared-document" in sql
    assert "asset.attach" in sql
    assert "payload" in sql
    assert digest in sql
    assert "LIMIT 1" in sql.upper()
