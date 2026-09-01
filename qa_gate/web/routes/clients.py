"""Client registry CRUD, plus storing the RPC credential.

The credential form is write-only by design: an API key that has been saved is
never rendered back, not even masked, because a masked field that round-trips
still means the plaintext reached the browser.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ... import clients as clients_mod
from ...clients import BRANCH_MODES, HOSTING_PLATFORMS, ODOO_VERSIONS, ClientError
from ...odoo_client import OdooAuthError, OdooClient, OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,48}$")
GITHUB_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _form_context(**overrides) -> dict:
    ctx = {
        "platforms": HOSTING_PLATFORMS,
        "versions": ODOO_VERSIONS,
        "branch_modes": BRANCH_MODES,
        "error": None,
        "client": None,
        "values": {},
    }
    ctx.update(overrides)
    return ctx


@router.get("/clients")
def list_clients(request: Request):
    try:
        deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    return deps.render(request, "clients.html", {
        "clients": clients_mod.list_all(include_inactive=True),
    })


@router.get("/clients/new")
def new_client(request: Request):
    try:
        deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    return deps.render(request, "client_form.html", _form_context())


@router.post("/clients/new")
async def create_client(request: Request):
    try:
        session = deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    slug = (form.get("slug") or "").strip().lower()
    name = (form.get("name") or "").strip()
    error = _validate(slug, name, form)
    if error:
        return deps.render(request, "client_form.html",
                           _form_context(error=error, values=form))
    try:
        client = clients_mod.create(
            slug=slug, name=name, created_by=session.user.id, **_fields(form))
    except ClientError as exc:
        return deps.render(request, "client_form.html",
                           _form_context(error=str(exc), values=form))
    log.info("Client created: %s by %s", client.slug, session.user.login)
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


@router.get("/clients/{client_id}")
def client_detail(request: Request, client_id: int):
    try:
        deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    client = clients_mod.get(client_id)
    if not client:
        return deps.render(request, "error.html",
                           {"code": 404, "message": "No such client."}, status_code=404)
    return deps.render(request, "client_detail.html", {"client": client})


@router.get("/clients/{client_id}/edit")
def edit_client(request: Request, client_id: int):
    try:
        deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    client = clients_mod.get(client_id)
    if not client:
        return deps.render(request, "error.html",
                           {"code": 404, "message": "No such client."}, status_code=404)
    return deps.render(request, "client_form.html",
                       _form_context(client=client, values=client.__dict__))


@router.post("/clients/{client_id}/edit")
async def update_client(request: Request, client_id: int):
    try:
        session = deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    client = clients_mod.get(client_id)
    if not client:
        return deps.render(request, "error.html",
                           {"code": 404, "message": "No such client."}, status_code=404)
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    name = (form.get("name") or "").strip()
    error = _validate(client.slug, name, form, slug_required=False)
    if error:
        return deps.render(request, "client_form.html",
                           _form_context(client=client, error=error, values=form))
    clients_mod.update(client_id, name=name,
                       active=form.get("active") == "on", **_fields(form))
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/credentials")
async def save_credentials(request: Request, client_id: int):
    """Verify the credential against the live instance before storing it.

    Storing an unverified key means the first time anyone finds out it is wrong
    is halfway through a run, reported as an infrastructure failure against
    somebody's task. Better to fail here, where the person who typed it is
    still looking at the screen.
    """
    try:
        session = deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    client = clients_mod.get(client_id)
    if not client:
        return deps.render(request, "error.html",
                           {"code": 404, "message": "No such client."}, status_code=404)
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    login = (form.get("rpc_login") or "").strip()
    api_key = (form.get("rpc_api_key") or "").strip()

    def fail(message: str):
        return deps.render(request, "client_detail.html",
                           {"client": client, "cred_error": message}, status_code=200)

    if not client.staging_url or not client.staging_db:
        return fail("Set the staging URL and database name before adding credentials.")
    if not login or not api_key:
        return fail("Both the login and the API key are required.")

    try:
        odoo_user = OdooClient(client.staging_url, client.staging_db).login(login, api_key)
    except OdooAuthError:
        return fail(
            "The staging instance rejected those credentials. Note this must be "
            "an API key, not a password, if the account has 2FA enabled."
        )
    except OdooError as exc:
        return fail(f"Could not reach the staging instance. {exc}")

    cfg = request.app.state.config
    clients_mod.set_rpc_credentials(
        client_id, login=login, api_key=api_key,
        secret_key=cfg.secret_key, updated_by=session.user.id,
    )
    log.info("RPC credentials stored for %s (uid %s) by %s",
             client.slug, odoo_user.uid, session.user.login)
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


# ---- validation ------------------------------------------------------------

def _validate(slug: str, name: str, form: dict, *, slug_required: bool = True) -> str | None:
    if slug_required and not SLUG_RE.match(slug or ""):
        return ("Slug must be lowercase letters, numbers, hyphens or underscores, "
                "2 to 49 characters.")
    if not name:
        return "Name is required."
    github = (form.get("github") or "").strip()
    if github and not GITHUB_RE.match(github):
        return "GitHub must be in owner/name form, e.g. hsxtech/legacymakermeats."
    url = (form.get("staging_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return "Staging URL must start with http:// or https://"
    return None


def _fields(form: dict) -> dict:
    return {
        "github": form.get("github", ""),
        "odoo_version": form.get("odoo_version", "17.0"),
        "hosting_platform": form.get("hosting_platform", "other"),
        "staging_url": form.get("staging_url", ""),
        "staging_db": form.get("staging_db", ""),
        "db_name_pattern": form.get("db_name_pattern", "%_staging"),
        "branch_mode": form.get("branch_mode", "per_task"),
        "base_branch": form.get("base_branch", "main"),
    }
