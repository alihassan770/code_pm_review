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


def _no_em_dash(value):
    """Render every em and en dash as a plain hyphen, everywhere.

    The house style bans them. Enforcing that in the prompts alone is not
    enough for two reasons: rows written before the ban still hold them, and a
    model instructed not to use a character will occasionally use it anyway.
    Neither is fixable at the point of writing, so the rule is applied at the
    point of reading, which is the only place that sees all the text.

    It runs as `finalize`, so it covers stored summaries, screenshot captions
    and template literals alike without a filter having to be remembered at
    each site. Non-strings pass through untouched, and a hyphen is harmless in
    the URLs and class names that also flow through here.
    """
    if isinstance(value, str) and ("\u2014" in value or "\u2013" in value):
        return value.replace("\u2014", "-").replace("\u2013", "-")
    return value


templates.env.finalize = _no_em_dash


def _plain_number(value) -> str:
    """Show an identifier like "R1" as plain "1".

    Display only. The model is still asked for `R1`-style ids and the plan still
    references them in `covers`, because a prefixed id is much harder to confuse
    with a scenario id or an assertion id inside a prompt. That reasoning is
    about the prompt, though, and none of it is the reader's problem: on the page
    a numbered list should be numbered.

    Anything that is not a single letter followed by digits is passed through
    untouched, so an id in a shape this does not recognise is shown as it is
    rather than mangled into something misleading.
    """
    text = str(value or "").strip()
    if len(text) > 1 and text[0].isalpha() and text[1:].isdigit():
        return text[1:]
    return text


templates.env.filters["plain_number"] = _plain_number


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
