"""Run the web application: blocking (CLI) or in a background thread (notebooks)."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any
from urllib.parse import quote

import uvicorn
import xarray as xr

from geoloupe.catalog import Catalog
from geoloupe.config import Config

log = logging.getLogger(__name__)


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _catalog(obj: Catalog | Config | dict[str, xr.Dataset] | xr.Dataset, **kw: Any) -> Catalog:
    if isinstance(obj, Catalog):
        return obj
    if isinstance(obj, Config):
        return Catalog(obj)
    if isinstance(obj, xr.Dataset):
        obj = {str(obj.attrs.get("name") or "data"): obj}
    return Catalog.from_xarray(obj, **kw)


class BasePath:
    """Serve an app under a path prefix (e.g. ``/sessions/abc`` on RenkuLab).

    Requests for ``<prefix>/…`` reach the app as ``/…``; ``<prefix>`` alone redirects to
    ``<prefix>/`` (the web client's URLs are relative to it). Requests without the prefix pass
    unchanged, so the app also works behind proxies that remove the prefix themselves.
    """

    def __init__(self, app: Any, prefix: str):
        self.app = app
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        p = self.prefix
        if p and scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == p and scope["type"] == "http":
                query = scope.get("query_string", b"").decode()
                location = quote(p + "/") + (f"?{query}" if query else "")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 307,
                        "headers": [(b"location", location.encode())],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            if path.startswith(p + "/"):
                scope = {
                    **scope,
                    "path": path[len(p) :],
                    "raw_path": path[len(p) :].encode(),
                    "root_path": scope.get("root_path", "") + p,
                }
        await self.app(scope, receive, send)


def base_path(explicit: str | None = None) -> str:
    """The path prefix to serve under: given, or from GEOLOUPE_BASE_PATH or RenkuLab's
    RENKU_BASE_URL_PATH."""
    if explicit is not None:
        return explicit
    return os.environ.get("GEOLOUPE_BASE_PATH") or os.environ.get("RENKU_BASE_URL_PATH") or ""


def serve(
    catalog: Catalog | Config,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "info",
    prefix: str | None = None,
) -> None:
    """Serve until interrupted (used by `geoloupe serve`). `prefix`: a path prefix to serve under
    (default: from the environment, see base_path)."""
    from geoloupe.api.app import create_app

    app: Any = create_app(_catalog(catalog))
    p = base_path(prefix)
    if p.strip("/"):
        log.info("serving under the path prefix %s", p)
        app = BasePath(app, p)
    uvicorn.run(app, host=host, port=port, log_level=log_level, timeout_keep_alive=30)


class Viewer:
    """A GeoLoupe server running in a background thread, e.g. inside Jupyter."""

    def __init__(self, catalog: Catalog, host: str = "127.0.0.1", port: int | None = None):
        from geoloupe.api.app import create_app

        self.catalog = catalog
        self.host = host
        self.port = port or free_port(host)
        config = uvicorn.Config(create_app(catalog), host=host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="geoloupe", daemon=True)

    def start(self, timeout: float = 30.0) -> Viewer:
        self._thread.start()
        t0 = time.monotonic()
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError("the GeoLoupe server stopped during start-up (see the log)")
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("the GeoLoupe server did not start in time")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        """URL of the web client. Behind JupyterHub, the jupyter-server-proxy path."""
        prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
        if prefix:
            return f"{prefix.rstrip('/')}/proxy/{self.port}/"
        return f"http://{self.host}:{self.port}/"

    def link(self, layer: str | None = None, time: str | None = None) -> str:
        """URL that opens the client on a given layer and time."""
        parts = []
        if layer:
            parts.append(f"layer={quote(layer, safe=':')}")
        if time:
            parts.append(f"t={quote(time)}")
        return self.url + (("#" + "&".join(parts)) if parts else "")

    def show(self, layer: str | None = None, time: str | None = None, height: int = 640) -> Any:
        """Display the client in the notebook (an IFrame)."""
        from IPython.display import IFrame

        return IFrame(self.link(layer, time), width="100%", height=height)

    def _repr_html_(self) -> str:
        return (
            f'<iframe src="{self.url}" width="100%" height="640" style="border:0"></iframe>'
            f'<div style="font-size:12px">GeoLoupe at <a href="{self.url}" target="_blank">{self.url}</a></div>'
        )

    def __repr__(self) -> str:
        return f"Viewer({self.url!r}, layers={len(self.catalog.layers)})"


def show(
    data: Catalog | Config | dict[str, xr.Dataset] | xr.Dataset,
    layers: dict[str, dict[str, Any]] | None = None,
    port: int | None = None,
    **kw: Any,
) -> Viewer:
    """Start a background server for datasets (or a catalog) and return the viewer.

    In a notebook, the returned object renders as the interactive map::

        geoloupe.show({"era5": ds_era5, "regional": ds_regional},
                        layers={"tas": {"sources": [{"dataset": "regional", "variable": "tas"},
                                                    {"dataset": "era5", "variable": "t2m"}]}})
    """
    cat = (
        _catalog(data, layers=layers, **kw)
        if not isinstance(data, Catalog | Config)
        else _catalog(data)
    )
    return Viewer(cat, port=port).start()
