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

from ... import (ai, app_secrets, app_settings, config as config_mod,
                 github, projects, users)
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
        # The AI provider an administrator chose, and the state of each
        # provider's key. Keys are stored per provider, so the page can show
        # which ones are already set rather than only the active one, and
        # switching back to a provider you used before needs no retyping.
        "provider": ai.selected(),
        "providers": list(ai.PROVIDERS.values()),
        "provider_keys": {
            name: {"configured": app_secrets.is_configured(ai.secret_key_name(name)),
                   "hint": app_secrets.login_for(ai.secret_key_name(name))}
            for name in ai.PROVIDERS},
        "ai_configured": ai.is_configured(),
        # Roles. Two of them, and this is the whole list: an administrator sets
        # the Odoo connection and the AI provider, everybody else uses what was
        # set. Rendered here rather than on a page of its own because "who may
        # change these" belongs next to the things they change.
        "staff": users.list_all(),
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


@router.post("/settings/ai-provider")
async def save_ai_provider(request: Request):
    """Choose a provider and store its API key, after proving the key works.

    One provider is active at a time and it applies to everybody: the reviews
    are run by us, so asking each user to bring their own account would bill our
    costs to people who never see the tool. Admin-only for the same reason the
    Odoo connection is, it decides what every other user's runs cost and where
    their client source is sent.

    Only the last four characters of a key are kept in the clear, as a
    fingerprint: enough to tell two keys apart when rotating one, useless to
    anybody who reads the row. The key itself is encrypted with the same Fernet
    key as every other stored credential and is never rendered back into a page.
    """
    try:
        session, denied = _admin(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    if denied:
        return denied

    form = await request.form()
    deps.verify_csrf(session, form.get("csrf_token"))
    cfg = request.app.state.config
    name = (form.get("provider") or "").strip().lower()
    provider = ai.PROVIDERS.get(name)
    if not provider:
        return deps.render(request, "settings.html",
                           _ctx(request, error="Choose one of the listed AI providers."))

    key = (form.get("api_key") or "").strip()
    stored = app_secrets.is_configured(ai.secret_key_name(provider.key))
    if not key and stored:
        # Switching back to a provider whose key is already held. Asking for it
        # again would mean an administrator has to keep every key to hand just
        # to change their mind, so selecting is enough.
        app_settings.set_(app_settings.AI_PROVIDER, provider.key,
                          updated_by=session.user.id)
        log.info("AI provider set to %s by %s", provider.key, session.user.login)
        return deps.render(request, "settings.html", _ctx(
            request, ok=f"Now using {provider.label} with the key already stored."))
    if not key:
        return deps.render(request, "settings.html", _ctx(
            request, error=f"An API key is required to use {provider.label}."))

    try:
        models = ai.verify(key, provider)
    except ai.AIError as exc:
        return deps.render(request, "settings.html", _ctx(request, error=str(exc)))

    app_secrets.set_(ai.secret_key_name(provider.key), login=key[-4:], secret=key,
                     secret_key=cfg.secret_key, updated_by=session.user.id)
    app_settings.set_(app_settings.AI_PROVIDER, provider.key,
                      updated_by=session.user.id)
    log.info("%s key stored (....%s) and selected by %s",
             provider.label, key[-4:], session.user.login)

    note = f"{provider.label} key verified, and it is now the active provider."
    if models and provider.reasoning not in models:
        # Not fatal, the key is valid and the account may simply be scoped
        # differently, but silently falling back to a model the reviewer did not
        # choose is exactly the kind of thing that should be said out loud.
        log.warning("%s key stored but %s is not in the visible model list: %s",
                    provider.label, provider.reasoning, ", ".join(models))
        note += (f" Note that {provider.reasoning} is not in this account's model "
                 "list, so reviews will fail until it can reach that model.")
    return deps.render(request, "settings.html", _ctx(request, ok=note))


@router.post("/settings/role")
async def set_role(request: Request):
    """Promote or demote one person between the two roles.

    Two roles, deliberately: `user` runs reviews, `admin` also sets the Odoo
    connection and the AI provider. Anything finer would be a permission model
    for a tool used by one team.

    **An administrator cannot demote themselves, and the last one cannot be
    demoted at all.** Both guards exist because the failure they prevent is
    unrecoverable from inside the app: a system with no administrator has nobody
    who can appoint one, and the only way back is `qa-gate grant-admin` on the
    server. Odoo's own `base.group_system` still grants admin on next sign-in,
    so a demotion here is not permanent for a genuine Odoo sysadmin, and the
    page says so.
    """
    try:
        session, denied = _admin(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    if denied:
        return denied

    form = await request.form()
    deps.verify_csrf(session, form.get("csrf_token"))
    try:
        target_id = int(form.get("user_id") or 0)
    except ValueError:
        target_id = 0
    role = (form.get("role") or "").strip().lower()
    target = users.get(target_id)

    if not target or role not in ("user", "admin"):
        return deps.render(request, "settings.html",
                           _ctx(request, error="That is not a person and a role."))
    if role == "user":
        if target.id == session.user.id:
            return deps.render(request, "settings.html", _ctx(
                request, error="You cannot remove your own administrator rights. "
                               "Ask another administrator to do it."))
        if users.admin_count() <= 1:
            return deps.render(request, "settings.html", _ctx(
                request, error="This is the only administrator. Promote somebody "
                               "else first, or nobody will be able to change "
                               "these settings."))

    users.set_admin(target.id, role == "admin")
    log.info("%s set %s to %s", session.user.login, target.login, role)
    return deps.render(request, "settings.html", _ctx(
        request, ok=f"{target.name or target.login} is now "
                    f"{'an administrator' if role == 'admin' else 'a user'}."))


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
