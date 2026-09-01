"""Phase B pages: the hygiene audit, the census, and client team membership.

Routes here are `def` rather than `async def` on purpose. An audit is a series
of blocking RPC calls taking a few seconds, and a sync route runs in FastAPI's
threadpool where a slow instance delays one worker instead of the event loop.
Making them async without an async Odoo client would be the worst of both.

Nothing in this module writes to a client instance. It cannot: `census.take` and
`audit.run` only read, and the RPC credential is an API key that could not open
a web session even if something here tried.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from ... import audit as audit_mod
from ... import census as census_mod
from ... import clients as clients_mod
from ... import fingerprint as fp_mod
from ... import instance, users as users_mod
from ...odoo_client import OdooAuthError, OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()


def _guard(request: Request):
    """Session or redirect. Repeated per route rather than pushed into a
    dependency because `Depends` cannot return a redirect without raising, and
    an exception-driven redirect is harder to read than four extra lines."""
    try:
        return deps.require_session(request), None
    except deps.NotLoggedIn:
        return None, deps.login_redirect(request)


def _client_or_404(request: Request, client_id: int):
    client = clients_mod.get(client_id)
    if client:
        return client, None
    return None, deps.render(request, "error.html",
                             {"code": 404, "message": "No such client."},
                             status_code=404)


# ---- the fleet report (UC-16) ----------------------------------------------

@router.get("/audit")
def fleet(request: Request):
    """One table, every instance, most alarming first.

    The sort is deliberate: refusals above unknowns above passes. A hygiene
    report sorted by client name buries the instance that can email customers
    somewhere in the middle of the alphabet.
    """
    session, redirect = _guard(request)
    if redirect:
        return redirect

    all_clients = clients_mod.list_all(include_inactive=True)
    latest = audit_mod.latest_by_client()
    rows = []
    for c in all_clients:
        row = latest.get(c.id)
        checks = audit_mod.checks_of(row) if row else []
        rows.append({
            "client": c,
            "audit": row,
            "checks": checks,
            "failures": [x for x in checks if x.status == audit_mod.FAIL],
            "warnings": [x for x in checks if x.status == audit_mod.WARN],
        })
    order = {audit_mod.VERDICT_REFUSE: 0, audit_mod.VERDICT_ERROR: 1,
             audit_mod.VERDICT_PASS: 2}
    rows.sort(key=lambda r: (order.get((r["audit"] or {}).get("verdict"), 1.5),
                             r["client"].name.lower()))

    return deps.render(request, "audit.html", {
        "rows": rows,
        "never_audited": [r for r in rows if not r["audit"]],
        "auditable": [c for c in all_clients if c.has_credentials],
    })


@router.post("/audit/run")
async def run_fleet(request: Request):
    """Audit every instance that has a stored credential.

    Async only so the form can be awaited; the sweep itself is handed to the
    threadpool, because a fleet of forty instances is a minute of blocking RPC
    and doing that on the event loop would stall every other request.
    """
    session, redirect = _guard(request)
    if redirect:
        return redirect
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    targets = [c for c in clients_mod.list_all(include_inactive=True) if c.has_credentials]
    results = await run_in_threadpool(
        audit_mod.run_fleet, targets, request.app.state.config.secret_key,
        run_by=session.user.id,
    )
    log.info("Fleet audit by %s: %s instance(s), %s refused",
             session.user.login, len(results),
             sum(1 for r in results if r.verdict == audit_mod.VERDICT_REFUSE))
    return RedirectResponse("/audit", status_code=303)


# ---- per client ------------------------------------------------------------

@router.get("/clients/{client_id}/audit")
def client_audit(request: Request, client_id: int):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing

    latest = audit_mod.latest_for(client_id)
    return deps.render(request, "client_audit.html", {
        "client": client,
        "audit": latest,
        "checks": audit_mod.checks_of(latest) if latest else [],
        "history": audit_mod.history_for(client_id),
        "statuses": audit_mod,
    })


@router.post("/clients/{client_id}/audit")
async def run_client_audit(request: Request, client_id: int):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    result = await run_in_threadpool(
        audit_mod.run, client, request.app.state.config.secret_key,
        run_by=session.user.id,
    )
    log.info("Audit of %s by %s: %s", client.slug, session.user.login, result.verdict)
    return RedirectResponse(f"/clients/{client_id}/audit", status_code=303)


@router.get("/clients/{client_id}/census")
def client_census(request: Request, client_id: int):
    """The census, computed live and thrown away when the response is sent.

    There is no "refresh" button because there is nothing cached to refresh —
    loading the page *is* taking the census (plan §9).
    """
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing

    ctx: dict = {"client": client, "census": None, "error": None, "drift": []}
    try:
        conn = instance.connect(client, request.app.state.config.secret_key)
        census = census_mod.take(conn)
    except instance.MissingCredentials as exc:
        ctx["error"] = str(exc)
    except OdooAuthError:
        ctx["error"] = ("The staging instance rejected the stored credentials. "
                        "The API key was probably revoked in Odoo.")
    except OdooError as exc:
        ctx["error"] = str(exc)
    else:
        ctx["census"] = census
        ctx["conflicts"] = census.conflicts()
        # Compared against the last stored fingerprint, not recorded as a new
        # one: viewing a page is not a run, and a fingerprint written by a page
        # load would make the next real drift report compare against a moment
        # nobody chose.
        ctx["drift"] = fp_mod.diff(fp_mod.latest(client.id), fp_mod.compute(census))
    return deps.render(request, "client_census.html", ctx)


# ---- team membership (the phase A gap) -------------------------------------

@router.get("/clients/{client_id}/team")
def client_team(request: Request, client_id: int):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing
    attached = clients_mod.team_of(client_id)
    attached_ids = {r["id"] for r in attached}
    return deps.render(request, "client_team.html", {
        "client": client,
        "team": attached,
        "candidates": [u for u in users_mod.list_all() if u.id not in attached_ids],
    })


@router.post("/clients/{client_id}/team")
async def update_team(request: Request, client_id: int):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    action = form.get("action")
    try:
        user_id = int(form.get("user_id") or 0)
    except ValueError:
        user_id = 0
    if user_id:
        if action == "remove":
            clients_mod.detach_user(user_id, client_id)
        else:
            access = "owner" if form.get("access") == "owner" else "member"
            clients_mod.attach_user(user_id, client_id, access=access)
    return RedirectResponse(f"/clients/{client_id}/team", status_code=303)
