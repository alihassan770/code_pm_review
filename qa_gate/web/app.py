"""FastAPI application factory.

The control plane only. It never touches a client repo worktree and never drives
a browser — those belong to the runner, which is a separate process for the same
reason odoo-dev-loop split its runner out: the half that holds network
credentials and the half that executes things on a machine have different blast
radii and should be deployable separately.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config as config_mod
from .. import db, paths, sessions
from .routes import audit as audit_routes
from .routes import tasks as task_routes
from .routes import auth as auth_routes
from .routes import clients as client_routes
from .routes import dashboard as dashboard_routes
from .routes import knowledge as knowledge_routes
from .routes import runs as run_routes
from .routes import settings as settings_routes
from .routes import setup as setup_routes

log = logging.getLogger(__name__)

TEMPLATE_DIR = __import__("pathlib").Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config_mod.load()
    paths.ensure_dirs()
    app.state.config = cfg
    app.state.templates = templates

    db.init_pool(cfg.database_url)
    applied = db.migrate()
    if applied:
        log.info("Applied migrations: %s", ", ".join(applied))
    sessions.purge_expired()

    if not cfg.odoo.configured:
        log.warning(
            "No identity Odoo configured yet. The first request will redirect "
            "to /setup. Config file: %s", cfg.path,
        )
    yield
    db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="Odoo PM Agent", lifespan=lifespan, docs_url=None, redoc_url=None)

    app.include_router(setup_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(client_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(task_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(knowledge_routes.router)
    app.include_router(run_routes.router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        return templates.TemplateResponse(
            request, "error.html",
            {"code": 404, "message": "That page does not exist."},
            status_code=404,
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        # Touches Postgres deliberately: a process that is up but cannot reach
        # its database is not healthy, and reporting otherwise hides the outage.
        db.query_one("SELECT 1 AS ok")
        return {"ok": True}

    # No return annotation: FastAPI would try to build a Pydantic response model
    # from a union of Response subclasses and fail at import time.
    @app.get("/", include_in_schema=False)
    def root(request: Request):
        if not request.app.state.config.odoo.configured:
            return RedirectResponse("/setup", status_code=303)
        return RedirectResponse("/dashboard", status_code=303)

    return app
