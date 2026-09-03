"""Application settings: the Odoo connection and its service credential.

Admin-only. Changing which Odoo issues identities is the same privilege as
handing out accounts, and the service credential can read every task in the
system, so neither belongs behind an ordinary session.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ... import ai, app_secrets, config as config_mod, github, projects
from ...odoo_client import OdooAuthError, OdooClient, OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()


def _ctx(request: Request, **over) -> dict:
    cfg = request.app.state.config
    ctx = {
        "cfg": cfg,
        # Prefill with whoever is signed in when nothing is stored — they are
        # almost always the account being offered, and retyping a login you just
        # used is pure friction.
        "service_login": (app_secrets.login_for(app_secrets.IDENTITY_RPC)
                          or (deps.current_session(request).user.login
                              if deps.current_session(request) else "")),
        "service_configured": app_secrets.is_configured(app_secrets.IDENTITY_RPC),
        "error": None, "ok": None, "projects_seen": None,
        "env_driven": config_mod.env_driven(),
        "github_configured": app_secrets.is_configured(app_secrets.GITHUB_TOKEN),
        "github_login": app_secrets.login_for(app_secrets.GITHUB_TOKEN),
        # Only ever the fingerprint, never the key. `login_for` reads a column
        # that holds the last four characters; the secret itself is not decrypted
        # to render this page.
        "deepseek_configured": app_secrets.is_configured(app_secrets.DEEPSEEK_KEY),
        "deepseek_hint": app_secrets.login_for(app_secrets.DEEPSEEK_KEY),
        "deepseek_model": ai.MODEL_REASONING,
    }
    ctx.update(over)
    return ctx


def _admin(request: Request):
    session = deps.require_session(request)
    if not session.user.is_admin:
        return None, deps.render(
            request, "error.html",
            {"code": 403, "message": "Only an administrator can change these settings."},
            status_code=403)
    return session, None


@router.get("/settings")
def settings_page(request: Request):
    try:
        session, denied = _admin(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    if denied:
        return denied
    return deps.render(request, "settings.html", _ctx(request))


@router.post("/settings/github-token")
async def save_github_token(request: Request):
    """Store a GitHub token, after proving it works.

    Verified by asking GitHub who the token belongs to. Storing an unverified
    token means the first sign of a bad one is an empty knowledge base that
    looks like the client has no addons.
    """
    try:
        session, denied = _admin(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    if denied:
        return denied

    form = await request.form()
    deps.verify_csrf(session, form.get("csrf_token"))
    token = (form.get("token") or "").strip()
    cfg = request.app.state.config
    if not token:
        return deps.render(request, "settings.html",
                           _ctx(request, error="A GitHub token is required."))

    import httpx
    try:
        resp = httpx.get(f"{github.API_ROOT}/user", timeout=20.0,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        return deps.render(request, "settings.html",
                           _ctx(request, error=f"Could not reach GitHub. {exc}"))
    if resp.status_code != 200:
        return deps.render(request, "settings.html", _ctx(
            request, error="GitHub rejected that token "
                           f"({resp.status_code}). Check it has not expired and "
                           "that it carries the `repo` scope."))
    who = (resp.json() or {}).get("login") or "?"

    app_secrets.set_(app_secrets.GITHUB_TOKEN, login=who, secret=token,
                     secret_key=cfg.secret_key, updated_by=session.user.id)
    log.info("GitHub token stored for %s by %s", who, session.user.login)
    return deps.render(request, "settings.html",
                       _ctx(request, ok=f"GitHub token verified as {who}."))


@router.post("/settings/deepseek-key")
async def save_deepseek_key(request: Request):
    """Store a DeepSeek API key, after proving it works.

    Only the last four characters are kept in the clear, as a fingerprint: it is
    enough to tell two keys apart when rotating one, and useless to anybody who
    reads the row. The key itself is encrypted with the same Fernet key as every
    other stored credential and is never rendered back into a page.
    """
    try:
        session, denied = _admin(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    if denied:
        return denied

    form = await request.form()
    deps.verify_csrf(session, form.get("csrf_token"))
    key = (form.get("api_key") or "").strip()
    cfg = request.app.state.config
    if not key:
        return deps.render(request, "settings.html",
                           _ctx(request, error="A DeepSeek API key is required."))
    try:
        models = ai.verify(key)
    except ai.AIError as exc:
        return deps.render(request, "settings.html", _ctx(request, error=str(exc)))

    if ai.MODEL_REASONING not in models:
        # Not fatal — the key is valid and the account may simply be scoped
        # differently — but silently falling back to a model the reviewer did not
        # choose is exactly the kind of thing that should be said out loud.
        log.warning("DeepSeek key stored but %s is not in the visible model list: %s",
                    ai.MODEL_REASONING, ", ".join(models))

    app_secrets.set_(app_secrets.DEEPSEEK_KEY, login=key[-4:], secret=key,
                     secret_key=cfg.secret_key, updated_by=session.user.id)
    log.info("DeepSeek key stored (••••%s) by %s", key[-4:], session.user.login)
    note = f"DeepSeek key verified. Models available: {', '.join(models)}."
    if ai.MODEL_REASONING not in models:
        note += (f" Note that {ai.MODEL_REASONING} is not among them, so digests "
                 "will fail until this account can reach it.")
    return deps.render(request, "settings.html", _ctx(request, ok=note))


@router.post("/settings/service-credential")
async def save_service_credential(request: Request):
    try:
        session, denied = _admin(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    if denied:
        return denied

    form = await request.form()
    deps.verify_csrf(session, form.get("csrf_token"))
    login = (form.get("login") or "").strip()
    secret = form.get("secret") or ""
    cfg = request.app.state.config

    if not login or not secret:
        return deps.render(request, "settings.html",
                           _ctx(request, error="Both a login and an API key are required."))
    # Prove it works before storing it. A credential that only fails at 2am in a
    # nightly run is the worst possible time to find out it was mistyped.
    try:
        OdooClient(cfg.odoo.url, cfg.odoo.db).authenticate(login, secret)
    except OdooAuthError as exc:
        return deps.render(request, "settings.html", _ctx(request, error=str(exc)))
    except OdooError as exc:
        return deps.render(request, "settings.html",
                           _ctx(request, error=f"Could not reach Odoo. {exc}"))

    app_secrets.set_(app_secrets.IDENTITY_RPC, login=login, secret=secret,
                     secret_key=cfg.secret_key, updated_by=session.user.id)
    log.info("Odoo service credential set to %s by %s", login, session.user.login)

    seen = None
    try:
        seen = len(projects.connect(cfg).search_projects(""))
    except Exception as exc:  # noqa: BLE001 - informational only
        log.info("service credential stored but project probe failed: %s", exc)
    return deps.render(request, "settings.html",
                       _ctx(request, ok="Credential verified and stored.",
                            projects_seen=seen))
