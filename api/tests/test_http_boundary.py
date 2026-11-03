from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas import PasswordChangeRequest


def test_health_exposes_no_data_and_disables_caching() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_auth_request_is_rejected_before_large_body_is_parsed() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/auth/login",
            content=b"{}",
            headers={"Content-Length": str(1024 * 1024 + 1)},
        )
    assert response.status_code == 413


def test_sync_write_limit_is_enforced_at_http_boundary() -> None:
    with TestClient(app) as client:
        response = client.put(
            "/v1/sync/notebooks/item",
            content=b"{}",
            headers={"Content-Length": str(16 * 1024 * 1024 + 1)},
        )
    assert response.status_code == 413


def test_password_change_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/auth/change-password",
            json={
                "currentPassword": "old long password",
                "newPassword": "a different long password",
            },
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication is required."}


def test_password_change_rejects_short_new_password() -> None:
    try:
        PasswordChangeRequest.model_validate(
            {"currentPassword": "old long password", "newPassword": "short"}
        )
    except ValidationError as error:
        assert error.errors()[0]["loc"] == ("newPassword",)
    else:
        raise AssertionError("A short new password must be rejected")



def test_v2_share_preflight_allows_guest_ticket_header() -> None:
    from app.settings import get_settings

    origin = str(get_settings().app_origin).rstrip("/")
    with TestClient(app) as client:
        response = client.options(
            "/v2/operations/batch",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers":
                    "authorization,content-type,x-share-id",
            },
        )
    assert response.status_code == 200
    assert "x-share-id" in response.headers[
        "access-control-allow-headers"
    ].lower()
