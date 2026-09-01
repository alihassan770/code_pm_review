"""Phase C pages: the knowledge overlay and the coverage map.

Both read the cached parse of the client's repo rather than GitHub, so a page
load is never a rate-limited network call. Refreshing is an explicit POST — the
same reason the prior art never auto-pulls: a knowledge base that quietly
updated itself between somebody reading it and a run using it would make the two
disagree with no way to tell which was which.

The coverage map is the one place that needs both halves at once, so it takes a
live census (which is never stored, by design) and joins it against the cache.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from ... import census as census_mod
from ... import clients as clients_mod
from ... import coverage as coverage_mod
from ... import github, instance
from ... import knowledge as knowledge_mod
from ... import repo_sync
from ...odoo_client import OdooAuthError, OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()


def _guard(request: Request):
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


def _token(request: Request) -> str:
    return github.resolve_token(request.app.state.config.github_token)


@router.get("/clients/{client_id}/knowledge")
def client_knowledge(request: Request, client_id: int):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing

    snap = repo_sync.load(client_id)
    ctx: dict = {
        "client": client, "snap": snap, "knowledge_path": knowledge_mod.PATH,
        "stale_entries": [], "contradictions": [], "drift_note": None,
    }
    if snap:
        ctx["stale_entries"] = snap.knowledge.stale()
        # Contradiction detection needs the instance, and an unreachable one
        # must not take the page down — the knowledge is still worth reading.
        try:
            conn = instance.connect(client, request.app.state.config.secret_key)
            ctx["contradictions"] = knowledge_mod.contradictions(
                snap.knowledge, census_mod.take(conn))
        except (instance.MissingCredentials, OdooAuthError, OdooError) as exc:
            ctx["census_error"] = (
                f"Contradictions could not be checked against the instance: {exc}")
        ctx["drift_note"] = repo_sync.stale(client, snap, token=_token(request))
    return deps.render(request, "client_knowledge.html", ctx)


@router.post("/clients/{client_id}/knowledge/refresh")
async def refresh_knowledge(request: Request, client_id: int):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    try:
        snap = await run_in_threadpool(
            repo_sync.sync, client, token=_token(request), fetched_by=session.user.id)
    except repo_sync.NoRepository as exc:
        return deps.render(request, "client_knowledge.html", {
            "client": client, "snap": None, "knowledge_path": knowledge_mod.PATH,
            "error": str(exc), "stale_entries": [], "contradictions": [],
        })
    log.info("Repo sync for %s by %s: %s @ %s (%s scenarios)", client.slug,
             session.user.login, client.github, snap.short_sha, len(snap.scenarios))
    back = form.get("back") or f"/clients/{client_id}/knowledge"
    return RedirectResponse(deps.safe_next(back, f"/clients/{client_id}/knowledge"),
                            status_code=303)


@router.get("/clients/{client_id}/coverage")
def client_coverage(request: Request, client_id: int):
    """Which of this client's modules is most exposed right now.

    Sorted worst first, and the sort is the feature: a module we wrote, changed
    recently, with no scenario covering it belongs at the top of the page rather
    than in alphabetical order somewhere in the middle.
    """
    session, redirect = _guard(request)
    if redirect:
        return redirect
    client, missing = _client_or_404(request, client_id)
    if missing:
        return missing

    snap = repo_sync.load(client_id)
    ctx: dict = {"client": client, "snap": snap, "coverage": None, "error": None}
    try:
        conn = instance.connect(client, request.app.state.config.secret_key)
        census = census_mod.take(conn)
    except instance.MissingCredentials as exc:
        ctx["error"] = str(exc)
    except OdooAuthError:
        ctx["error"] = ("The staging instance rejected the stored credentials, so "
                        "there is nothing to join the repo against.")
    except OdooError as exc:
        ctx["error"] = str(exc)
    else:
        ctx["coverage"] = coverage_mod.build(
            census,
            repo_modules=snap.modules if snap else {},
            scenario_list=snap.scenarios if snap else [],
            knowledge=snap.knowledge if snap else None,
            last_changed=snap.last_changed if snap else {},
        )
    return deps.render(request, "client_coverage.html", ctx)
