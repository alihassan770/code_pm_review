"""Request-scoped helpers: who is logged in, and is this POST legitimate.

CSRF matters here in a way it deliberately does not for the runner API. The
runner authenticates by custom header with `allow_credentials=False`, which
takes CSRF off the table entirely. This surface is the opposite: a cookie the
browser attaches automatically, so every mutating form must carry a token.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import crypto, sessions
from ..sessions import COOKIE_NAME, Session


class NotLoggedIn(Exception):
    """Raised by require_session; converted to a redirect by the route."""


def current_session(request: Request) -> Session | None:
    cached = getattr(request.state, "session", None)
    if cached is not None:
        return cached
    session = sessions.load(request.cookies.get(COOKIE_NAME, ""))
    request.state.session = session
    return session


def require_session(request: Request) -> Session:
    session = current_session(request)
    if session is None:
        raise NotLoggedIn()
    return session


def login_redirect(request: Request) -> RedirectResponse:
    """Send an anonymous visitor to the login page, remembering where they were.

    `next` is restricted to a path on this site; an absolute URL here would be
    an open redirect, which is a small bug with a large phishing surface.
    """
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    if not target.startswith("/") or target.startswith("//"):
        target = "/dashboard"
    return RedirectResponse(f"/login?next={target}", status_code=303)


def safe_next(raw: str | None, fallback: str = "/dashboard") -> str:
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return fallback
    return raw


def verify_csrf(session: Session, submitted: str | None) -> None:
    if not crypto.constant_time_equals(session.csrf_token, submitted or ""):
        raise HTTPException(
            status_code=400,
            detail="This form expired or came from somewhere unexpected. Reload and try again.",
        )


def render(request: Request, template: str, context: dict | None = None, **kwargs):
    """TemplateResponse with the things every page needs already present."""
    session = current_session(request)
    ctx = {
        "session": session,
        "user": session.user if session else None,
        "csrf_token": session.csrf_token if session else "",
    }
    ctx.update(context or {})
    return request.app.state.templates.TemplateResponse(request, template, ctx, **kwargs)
