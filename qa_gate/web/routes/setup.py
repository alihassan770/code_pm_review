"""Configuring which Odoo is our identity provider.

Reachable without a login on a fresh install, because there is no way to log in
until it is done — authentication depends on this value. That makes the guard
below the important part of this module.

## Why it cannot simply stay open

Whoever sets this decides which Odoo issues identities. An attacker who could
repoint a configured instance at an Odoo *they* control would then log in as an
admin of their own server and inherit this app — including every client's stored
staging credentials. So "let anyone fix a wrong URL" is a privilege escalation
wearing a helpful hat.

## The rule

Configuring is allowed when **nobody has an account here yet**, and after that
only for a signed-in admin:

  * no users in the database -> nothing exists to take over, so it is open.
    This covers the real first-run case, including a first attempt that saved a
    wrong database name and left the operator unable to log in.
  * users exist              -> an admin may change it from inside the app.
  * locked out entirely      -> `qa-gate set-identity` on the server, which is
    the escape hatch that needs no session at all.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ... import config as config_mod
from ... import users
from ...odoo_client import OdooClient, OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()


def may_configure(request: Request) -> bool:
    cfg = request.app.state.config
    if not cfg.odoo.configured:
        return True
    if users.count() == 0:
        # Configured, but nobody has ever signed in — so the identity is
        # unproven and there is no account for an attacker to inherit.
        return True
    session = deps.current_session(request)
    return bool(session and session.user.is_admin)


def _context(request: Request, **overrides) -> dict:
    cfg = request.app.state.config
    ctx = {
        "error": None,
        "url_value": cfg.odoo.url,
        "db_value": cfg.odoo.db,
        "config_path": str(cfg.path),
        "reconfiguring": cfg.odoo.configured,
        "env_driven": config_mod.env_driven(),
    }
    ctx.update(overrides)
    return ctx


@router.get("/setup")
def setup_form(request: Request):
    if not may_configure(request):
        return RedirectResponse("/login", status_code=303)
    return deps.render(request, "setup.html", _context(request))


@router.post("/setup")
def setup_submit(request: Request, url: str = Form(""), db: str = Form("")):
    if not may_configure(request):
        return RedirectResponse("/login", status_code=303)

    cfg = request.app.state.config
    url, db = url.strip().rstrip("/"), db.strip()

    def fail(message: str):
        return deps.render(request, "setup.html",
                           _context(request, error=message, url_value=url, db_value=db))

    if not url or not db:
        return fail("Both the Odoo URL and the database name are required.")
    if not url.startswith(("http://", "https://")):
        return fail("The URL must start with http:// or https://")

    try:
        client = OdooClient(url, db)
        version = client.version()      # is the host an Odoo at all?
        client.check_database()         # does this database actually exist?
    except OdooError as exc:
        return fail(str(exc))

    new_cfg = replace(cfg, odoo=config_mod.OdooIdentity(url=url, db=db))
    try:
        config_mod.save(new_cfg)
    except OSError as exc:
        # A container with no writable config volume. Saying "saved" here and
        # reverting on the next deploy is the worst available outcome, so refuse
        # and name the two variables that actually persist.
        return fail(
            f"Verified the connection, but could not write {cfg.path} ({exc}). "
            f"This looks like a container deployment — set {config_mod.ENV_ODOO_URL} "
            f"and {config_mod.ENV_ODOO_DB} as environment variables instead."
        )
    request.app.state.config = new_cfg
    log.info("Identity Odoo set to %s (db %s, server %s)",
             url, db, version.get("server_version", "?"))
    return RedirectResponse("/login", status_code=303)
