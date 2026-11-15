"""Control-plane web app: the single UI + API for every project at once.

FastAPI + native WebSockets. The browser
gets a live aggregate view of ALL workers; the API is the command surface (enable/disable a
project, set the budget, graceful stop). ``main()`` is the ``carousel`` entrypoint.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import ConfigStore
from .core import ControlPlane

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _auth_ok(auth_header: str | None, username: str, password: str) -> bool:
    """Validate an HTTP Basic ``Authorization`` header against the configured credentials.

    When no username+password is configured, access is open (returns True).
    """
    if not (username and password):
        return True
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    user, _, pw = decoded.partition(":")
    # constant-time compare to avoid leaking length/contents via timing
    return (secrets.compare_digest(user, username)
            and secrets.compare_digest(pw, password))


def create_app(cp: ControlPlane, *, heartbeat_interval: float = 600.0,
               monitor_interval: float = 10.0, run_background: bool = True) -> FastAPI:

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        if run_background:
            tasks = [asyncio.create_task(_heartbeat_loop()),
                     asyncio.create_task(_monitor_loop())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await cp.stop_all()

    async def _heartbeat_loop():
        """Slow loop: HQ catalog refresh + project-update detection."""
        while True:
            try:
                await cp.heartbeat_once()
            except Exception as exc:  # keep the loop alive through transient HQ errors
                print(f"[heartbeat] {exc!r}")
            await asyncio.sleep(heartbeat_interval)

    async def _monitor_loop():
        """Fast loop: restart crashed workers, re-apply budget. No network."""
        while True:
            await asyncio.sleep(monitor_interval)
            try:
                await cp.monitor_once()
            except Exception as exc:
                print(f"[monitor] {exc!r}")

    app = FastAPI(title="Carousel", lifespan=lifespan)

    @app.middleware("http")
    async def _basic_auth(request, call_next):
        # /healthz stays open so the container health check works even with auth on.
        if request.url.path != "/healthz":
            gc = cp.config.global_config
            if not _auth_ok(request.headers.get("authorization"),
                            gc.http_username, gc.http_password):
                return Response(status_code=401, headers={
                    "WWW-Authenticate": 'Basic realm="Archive Carousel"'})
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        index_html = WEB_DIR / "index.html"
        return index_html.read_text() if index_html.exists() else "<h1>Archive Carousel</h1>"

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(cp.status())

    @app.get("/api/projects")
    async def projects() -> JSONResponse:
        return JSONResponse(cp.projects())

    @app.get("/api/stats")
    async def stats(hours: float = 24.0, buckets: int = 48) -> JSONResponse:
        hours = min(max(hours, 0.1), 24 * 30)      # clamp: 6 min .. 30 days
        buckets = min(max(int(buckets), 1), 500)
        return JSONResponse(cp.stats_series(hours=hours, buckets=buckets))

    @app.post("/api/projects/{name}/enable")
    async def enable(name: str) -> JSONResponse:
        await cp.set_enabled(name, True)
        return JSONResponse({"ok": True, "enabled": cp.config.enabled_projects()})

    @app.post("/api/projects/{name}/disable")
    async def disable(name: str) -> JSONResponse:
        await cp.set_enabled(name, False)
        return JSONResponse({"ok": True, "enabled": cp.config.enabled_projects()})

    @app.post("/api/projects/{name}/restart")
    async def restart(name: str) -> JSONResponse:
        await cp.restart_project(name)
        return JSONResponse({"ok": True})

    @app.post("/api/projects/{name}/concurrency")
    async def concurrency(name: str, body: dict) -> JSONResponse:
        await cp.set_project_concurrency(name, body.get("concurrent_items"))
        return JSONResponse({"ok": True, "budget": cp.supervisor.budget()})

    @app.post("/api/settings")
    async def settings(body: dict) -> JSONResponse:
        await cp.update_settings(body)
        return JSONResponse(cp.status())

    @app.post("/api/refresh")
    async def refresh() -> JSONResponse:
        await cp.heartbeat_once()
        return JSONResponse(cp.status())

    @app.post("/api/stop")
    async def stop() -> JSONResponse:
        await cp.stop_all()
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        gc = cp.config.global_config
        if not _auth_ok(socket.headers.get("authorization"),
                        gc.http_username, gc.http_password):
            await socket.close(code=1008)  # policy violation; UI falls back to polling
            return
        await socket.accept()
        try:
            while True:
                await socket.send_json({"status": cp.status(), "projects": cp.projects()})
                await asyncio.sleep(2.0)
        except WebSocketDisconnect:
            pass

    return app


def main() -> None:
    import uvicorn

    from .hq import HQClient
    from .projects import ProjectInstaller
    from .stats import StatsStore
    from .supervisor import Supervisor, SubprocessBackend

    home = Path(os.environ.get("WARRIOR_HOME", "/home/warrior"))
    config = ConfigStore(Path(os.environ.get("CONFIG_FILE", home / "projects" / "config.json")))
    config.load()

    stats = StatsStore(Path(os.environ.get("STATS_DB", home / "projects" / "stats.db")))

    def on_event(project: str, event: dict) -> None:
        if event.get("event") == "item_end":
            stats.record_item(project, bool(event.get("ok")), int(event.get("bytes", 0) or 0))

    installer = ProjectInstaller(projects_dir=home / "projects", versions_dir=home / "versions")
    worker_root = Path(__file__).resolve().parent.parent  # makes `python -m worker...` import
    supervisor = Supervisor(config, installer,
                            SubprocessBackend(worker_root, on_event=on_event),
                            data_dir=str(home / "data"))
    hq = HQClient(os.environ.get("WARRIOR_HQ", "https://warriorhq.archiveteam.org"))
    cp = ControlPlane(config, supervisor, hq, stats=stats)

    app = create_app(cp)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8001")))


if __name__ == "__main__":
    main()
