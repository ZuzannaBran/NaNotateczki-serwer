import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.browser_origins import browser_origin_allowed, localhost_origin_regex
from app.main import app
from app.routers.auth import router as auth_router
from app.routers import operation_realtime, study_shares
from app.settings import Settings


def configured(enabled: bool) -> Settings:
    return Settings(ALLOW_LOCALHOST_ORIGINS=enabled)


@pytest.mark.parametrize("origin", [
    "http://localhost:5173", "http://127.0.0.1:7357", "http://[::1]:8080",
    "http://localhost",
])
def test_loopback_preflight_and_invalid_auth_body_reach_v2(origin):
    settings = configured(True)
    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    probe = FastAPI()
    probe.add_middleware(
        CORSMiddleware,
        **{**cors.kwargs, "allow_origin_regex": localhost_origin_regex(settings)},
    )
    probe.include_router(auth_router)
    with TestClient(probe) as client:
        for route in ("register", "login"):
            response = client.options(
                f"/v2/auth/{route}",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,x-request-id",
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
            # Validation only: this cannot create an account or a session.
            response = client.post(f"/v2/auth/{route}", json={}, headers={"Origin": origin})
            assert response.status_code == 422
            assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("origin", [
    "http://localhost.evil.test:5173", "http://evil.test", "null",
    "http://127.0.0.1.evil.test", "http://localhost:80@evil.test",
    "http://localhost:5173/path",
])
def test_other_origins_remain_rejected(origin):
    assert not browser_origin_allowed(origin, configured(True))


def test_loopback_is_opt_in_and_public_origin_always_works():
    settings = configured(False)
    assert localhost_origin_regex(settings) is None
    assert not browser_origin_allowed("http://localhost:5173", settings)
    assert browser_origin_allowed(str(settings.app_origin).rstrip("/"), settings)


@pytest.mark.parametrize("enabled,expected_code", [(False, 4403), (True, 4401)])
@pytest.mark.parametrize("module,path", [
    (operation_realtime, "/v2/realtime"),
    (study_shares, "/v2/study-shares/00000000-0000-4000-8000-000000000001/events"),
])
def test_websockets_use_same_loopback_policy_without_bypassing_auth(
    monkeypatch, enabled, expected_code, module, path,
):
    monkeypatch.setattr(module, "get_settings", lambda: configured(enabled))
    probe = FastAPI()
    probe.include_router(module.router)
    with TestClient(probe) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(path, headers={"Origin": "http://localhost:5173"}) as socket:
                # With permitted Origin, authentication is still required.
                socket.send_json({"type": "invalid"})
                socket.receive_json()
        assert error.value.code == expected_code
