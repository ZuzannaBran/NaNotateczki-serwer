import time

from fastapi.testclient import TestClient

from app.main import app


def test_server_time_returns_uncached_epoch_milliseconds():
    before = time.time_ns() // 1_000_000
    with TestClient(app) as client:
        response = client.get("/v2/time")
    after = time.time_ns() // 1_000_000

    assert response.status_code == 200
    timestamp = response.json()["serverTimeMs"]
    assert isinstance(timestamp, int)
    assert before <= timestamp <= after
    assert response.headers["cache-control"] == "no-store"
