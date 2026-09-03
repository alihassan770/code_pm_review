"""Login and logout.

There is no signup, no password reset, and no local password. A person can log
in because they exist in our Odoo; removing them there removes their access
here, which means offboarding is one step instead of two.
"""
from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ... import app_secrets, clients, config as config_mod, sessions, users
from ...odoo_client import OdooAuthError, OdooClient, OdooError
from ...sessions import COOKIE_NAME
from .. import deps
from . import setup as setup_routes

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    if deps.current_session(request):
        return RedirectResponse(deps.safe_next(next), status_code=303)
    cfg = request.app.state.config
    if not cfg.odoo.configured:
        return RedirectResponse("/setup", status_code=303)
    return deps.render(request, "login.html", {
        "next": deps.safe_next(next),
        "odoo_url": cfg.odoo.url,
        "odoo_db": cfg.odoo.db,
        "can_reconfigure": setup_routes.may_configure(request),
        # Offering this here rather than only on the settings page is the whole
        # point: this request is the one moment the app legitimately holds a
        # working credential, because login uses it and then discards it.
        "offer_service_credential": not app_secrets.is_configured(app_secrets.IDENTITY_RPC),
        "error": None,
    })


@router.post("/login")
def login_submit(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
    next: str = Form("/dashboard"),
    use_for_service: str = Form(""),
):
    form_wants_service = use_for_service == "on"
    cfg = request.app.state.config
    if not cfg.odoo.configured:
        return RedirectResponse("/setup", status_code=303)

    def fail(message: str, *, connection: bool = False):
        # 200 rather than 401: this is a rendered form, and a 401 makes some
        # browsers surface their own basic-auth dialog on top of it.
        #
        # `connection` separates "your password is wrong" from "the server or
        # database is wrong". Only the second is fixable from /setup, and
        # conflating them leaves people retrying a password that was never the
        # problem — which is exactly what a bad database name looked like.
        return deps.render(request, "login.html", {
            "next": deps.safe_next(next), "odoo_url": cfg.odoo.url,
            "odoo_db": cfg.odoo.db, "error": message, "login_value": login,
            "connection_error": connection,
            "offer_service_credential": not app_secrets.is_configured(app_secrets.IDENTITY_RPC),
            "can_reconfigure": setup_routes.may_configure(request),
        }, status_code=200)

    client = OdooClient(cfg.odoo.url, cfg.odoo.db)
    try:
        odoo_user = client.login(login.strip(), password)
    except OdooAuthError as exc:
        log.info("Failed login for %r from %s", login, _client_ip(request))
        return fail(str(exc))
    except OdooError as exc:
        # A reachability problem is not the user's fault and must not read like
        # bad credentials, or they will sit there retrying a correct password.
        log.warning("Odoo unreachable during login: %s", exc)
        return fail(str(exc), connection=True)

    user = users.upsert_from_odoo(odoo_user)

    # Adopt the credential as the background service account, but only when the
    # person explicitly asked, only if they are an admin, and only when none is
    # set. It is stored encrypted like any other; revoke the API key in Odoo to
    # revoke it here.
    if (form_wants_service and user.is_admin
            and not app_secrets.is_configured(app_secrets.IDENTITY_RPC)):
        app_secrets.set_(app_secrets.IDENTITY_RPC, login=login.strip(), secret=password,
                         secret_key=cfg.secret_key, updated_by=user.id)
        log.info("Service credential adopted from %s's login", user.login)
    token, _ = sessions.create(
        user, cfg.session_hours,
        ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    response = RedirectResponse(deps.safe_next(next), status_code=303)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=cfg.session_hours * 3600,
        httponly=True,            # no reason for JS to read it
        samesite="lax",           # blocks cross-site POSTs while keeping normal links working
        secure=cfg.secure_cookies,
        path="/",
    )
    log.info("Login: %s (odoo uid %s)", user.login, user.odoo_uid)
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    session = deps.current_session(request)
    if session:
        deps.verify_csrf(session, csrf_token)
        sessions.destroy(request.cookies.get(COOKIE_NAME, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


def _client_ip(request: Request) -> str | None:
    """Best-effort client address, or None.

    Returns None rather than a raw string when the value will not parse. The
    column is `inet`, and `X-Forwarded-For` is attacker-controlled input that
    can hold anything at all — letting it through would turn a malformed header
    into a failed login, which is a denial of service with extra steps.
    """
    candidate = ""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Left-most entry is the original client when the proxy chain is honest.
        candidate = forwarded.split(",")[0].strip()
    elif request.client:
        candidate = request.client.host

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
