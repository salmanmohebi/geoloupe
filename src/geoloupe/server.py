"""Run the web application: blocking (CLI) or in a background thread (notebooks)."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from html import escape
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


STARTING_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}<title>GeoLoupe</title><style>
body{{font-family:system-ui,sans-serif;background:#e9edf1;color:#0b0b0b;display:grid;
place-items:center;min-height:100vh;margin:0}} div{{max-width:34em;padding:16px;text-align:center}}
p{{color:#52514e}} @media (prefers-color-scheme:dark){{body{{background:#14161a;color:#eee}}
p{{color:#aaa}}}}</style></head><body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


class Starting:
    """An app that answers at once while the real one is built in a background thread.

    Opening the datasets of a large configuration (or the public gallery on a slow machine) can
    take a minute or more; a server that listens only afterwards looks dead to browsers and to
    platforms that check the port (RenkuLab stops such sessions). Until the real app is ready,
    pages show "Starting GeoLoupe…" (and reload themselves) and the API answers 503.
    """

    def __init__(self, build: Callable[[], Any]):
        self.app: Any = None
        self.error: str | None = None
        threading.Thread(target=self._build, args=(build,), daemon=True, name="start").start()

    def _build(self, build: Callable[[], Any]) -> None:
        try:
            app = build()
            start = getattr(app.state, "start_background", None)
            if start:
                start()  # warm-up and vector overlays, as the app's own start-up would
            self.app = app
        except Exception as e:  # shown on the page; the server keeps answering
            log.error("GeoLoupe could not start: %s", e)
            self.error = str(e) or type(e).__name__

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":  # the real app's start-up runs in _build
            while True:
                msg = await receive()
                kind = msg["type"].rsplit(".", 1)[-1]  # startup / shutdown
                await send({"type": f"lifespan.{kind}.complete"})
                if kind == "shutdown":
                    return
        if self.app is not None:
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        api = "/api/" in scope["path"]
        if self.error:
            status, title, text = 500, "GeoLoupe could not start", escape(self.error)
        else:
            status, title, text = (
                503 if api else 200,
                "Starting GeoLoupe…",
                ("Opening the datasets. This page reloads by itself."),
            )
        if api:
            body = json.dumps({"detail": self.error or "starting"}).encode()
            ctype = b"application/json"
        else:
            refresh = "" if self.error else '<meta http-equiv="refresh" content="2">'
            body = STARTING_PAGE.format(refresh=refresh, title=title, text=text).encode()
            ctype = b"text/html; charset=utf-8"
        headers = [(b"content-type", ctype), (b"cache-control", b"no-store")]
        if status == 503:
            headers.append((b"retry-after", b"2"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def base_path(explicit: str | None = None) -> str:
    """The path prefix to serve under: given, or from GEOLOUPE_BASE_PATH or RenkuLab's
    RENKU_BASE_URL_PATH."""
    if explicit is not None:
        return explicit
    return os.environ.get("GEOLOUPE_BASE_PATH") or os.environ.get("RENKU_BASE_URL_PATH") or ""


def serve(
    catalog: Catalog | Config | Callable[[], Catalog],
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "info",
    prefix: str | None = None,
) -> None:
    """Serve until interrupted (used by `geoloupe serve`). `catalog` may be a function that
    builds it: the server then answers at once ("Starting GeoLoupe…") while the catalog is built.
    `prefix`: a path prefix to serve under (default: from the environment, see base_path)."""
    from geoloupe.api.app import create_app

    app: Any
    if callable(catalog) and not isinstance(catalog, Catalog | Config):
        build = catalog
        app = Starting(lambda: create_app(build(), warm=False))
    else:
        app = create_app(_catalog(catalog))
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
