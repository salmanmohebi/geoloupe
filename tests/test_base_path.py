"""Serving under a path prefix (RenkuLab sessions, other proxies)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from geoloupe.api.app import create_app
from geoloupe.catalog import Catalog
from geoloupe.server import BasePath, base_path


@pytest.fixture
def client(composite_catalog: Catalog) -> TestClient:
    app = BasePath(create_app(composite_catalog, warm=False), "/sessions/abc-123")
    return TestClient(app)


def test_prefixed_requests_reach_the_app(client: TestClient) -> None:
    assert client.get("/sessions/abc-123/api/health").json()["status"] == "ok"
    page = client.get("/sessions/abc-123/")
    assert page.status_code == 200 and "<html" in page.text.lower()
    tile = client.get("/sessions/abc-123/api/layers/tas/tiles/3/4/2.bin?time=2021-01-02")
    assert tile.status_code in (200, 204)


def test_prefix_alone_redirects_to_the_folder(client: TestClient) -> None:
    r = client.get("/sessions/abc-123?x=1", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/sessions/abc-123/?x=1"


def test_requests_without_the_prefix_still_work(client: TestClient) -> None:
    # proxies that remove the prefix themselves
    assert client.get("/api/health").json()["status"] == "ok"


def test_base_path_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEOLOUPE_BASE_PATH", raising=False)
    monkeypatch.delenv("RENKU_BASE_URL_PATH", raising=False)
    assert base_path() == ""
    monkeypatch.setenv("RENKU_BASE_URL_PATH", "/sessions/x")
    assert base_path() == "/sessions/x"
    monkeypatch.setenv("GEOLOUPE_BASE_PATH", "/geo")
    assert base_path() == "/geo"
    assert base_path("") == ""  # --base-path "" turns it off


def test_starting_answers_at_once_then_serves(composite_catalog: Catalog) -> None:
    import threading
    import time

    from geoloupe.server import Starting

    gate = threading.Event()

    def build():
        gate.wait(10)  # a slow start: opening the datasets
        return create_app(composite_catalog, warm=False)

    with TestClient(BasePath(Starting(build), "/sessions/x")) as c:
        page = c.get("/sessions/x/")
        assert page.status_code == 200 and "Starting GeoLoupe" in page.text
        assert 'http-equiv="refresh"' in page.text
        api = c.get("/sessions/x/api/catalog")
        assert api.status_code == 503 and api.json() == {"detail": "starting"}
        gate.set()
        for _ in range(100):
            if c.get("/sessions/x/api/health").status_code == 200:
                break
            time.sleep(0.05)
        assert c.get("/sessions/x/api/catalog").json()["layers"]


def test_starting_shows_why_it_failed() -> None:
    import time

    from geoloupe.server import Starting

    def build():
        raise RuntimeError("nothing to serve: no dataset could be opened")

    app = Starting(build)
    for _ in range(100):
        if app.error:
            break
        time.sleep(0.02)
    with TestClient(app) as c:
        page = c.get("/")
        assert page.status_code == 500 and "nothing to serve" in page.text
        assert "refresh" not in page.text
        assert c.get("/api/health").json()["detail"].startswith("nothing to serve")
