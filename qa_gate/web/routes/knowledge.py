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

from ... import app_secrets, census as census_mod
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
    """Stored token first, then the config file, then the environment or `gh`.

    The stored one wins because it is the only source a container deployment
    has: there is no config file to edit and no `gh` on the box.
    """
    cfg = request.app.state.config
    stored = app_secrets.get(app_secrets.GITHUB_TOKEN, cfg.secret_key)
    return github.resolve_token(stored.secret or cfg.github_token)


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
        "drift_note": None,
        # "created" or "updated", set by the refresh redirect. Anything else is
        # ignored, so a hand-typed ?synced=whatever cannot forge a success box.
        "synced": _synced_flag(request),
    }
    if snap:
        ctx["drift_note"] = repo_sync.stale(client, snap, token=_token(request))
    return deps.render(request, "client_knowledge.html", ctx)


def _synced_flag(request: Request) -> str:
    value = request.query_params.get("synced") or ""
    return value if value in ("created", "updated") else ""


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

    # Whether this is the first successful read decides which word the toast
    # uses. Taken before the sync, because after it there is always a row.
    existing = repo_sync.load(client_id)
    first_time = not (existing and existing.commit_sha)

    try:
        snap = await run_in_threadpool(
            repo_sync.sync, client, token=_token(request), fetched_by=session.user.id)
    except repo_sync.NoRepository as exc:
        return deps.render(request, "client_knowledge.html", {
            "client": client, "snap": None, "knowledge_path": knowledge_mod.PATH,
            "error": str(exc), "synced": "", "drift_note": None,
        })
    log.info("Repo sync for %s by %s: %s @ %s (%s module(s))", client.slug,
             session.user.login, client.github, snap.short_sha, len(snap.modules))

    back = deps.safe_next(form.get("back") or f"/clients/{client_id}/knowledge",
                          f"/clients/{client_id}/knowledge")
    # Only a read that actually produced a commit is worth announcing. A failed
    # one already renders its own error, and a green box above a red one would
    # be the page contradicting itself.
    if snap.ok:
        flag = "created" if first_time else "updated"
        back += ("&" if "?" in back else "?") + f"synced={flag}"
    return RedirectResponse(back, status_code=303)


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
