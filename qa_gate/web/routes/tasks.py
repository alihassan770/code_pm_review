"""Tasks for a client, read live from our Odoo.

Live rather than mirrored: a cached task list is wrong the moment a project
manager drags a card, and the premise of the gate is that the Odoo task is the
source of truth (plan §1). One RPC round trip per page view against a few dozen
rows is the right trade.

The page is also where a review is started, so it is the join between "what the
PM wants looked at" and "what the gate does".
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ... import app_secrets, clients as clients_mod, html_clean, projects
from ... import repo_sync, review
from ...odoo_client import OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()


def _client_or_404(request: Request, client_id: int):
    client = clients_mod.get(client_id)
    if not client:
        return None, deps.render(request, "error.html",
                                 {"code": 404, "message": "No such client."}, status_code=404)
    return client, None


@router.get("/clients/{client_id}/tasks")
def client_tasks(request: Request, client_id: int, stage: int | None = None):
    try:
        deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    client, err = _client_or_404(request, client_id)
    if err:
        return err

    client = clients_mod.with_projects(client)
    wanted_stage = (request.query_params.get("stage") or client.task_stage_name or "").strip()

    ctx = {
        "client": client, "tasks": [], "stage_names": [], "counts": {},
        "active_stage": wanted_stage, "error": None, "partial": [],
        "service_configured": app_secrets.is_configured(app_secrets.IDENTITY_RPC),
        "runs": {},
    }

    if not client.has_project:
        ctx["error"] = ("This client is not linked to any Odoo project yet. "
                        "Add one or more project ids on the client's settings page.")
        return deps.render(request, "client_tasks.html", ctx)

    try:
        identity = projects.connect(request.app.state.config)
    except projects.NotConfigured as exc:
        ctx["error"] = str(exc)
        return deps.render(request, "client_tasks.html", ctx)

    # Every project is queried, and the results are merged. One project failing
    # does not blank the page — it is listed instead, because a partial answer
    # that says what is missing beats an error that hides the rest.
    seen_stages: dict[str, int] = {}
    rows: list = []
    for cp in client.projects:
        try:
            stages = identity.stages(cp.odoo_project_id)
            counts = identity.task_counts_by_stage(cp.odoo_project_id)
            by_id = {st.id: st for st in stages}
            for st in stages:
                seen_stages[st.name] = seen_stages.get(st.name, 0) + counts.get(st.id, 0)

            # Stage ids differ per project, so the shared stage is resolved by
            # name inside each one. That is what makes "everything in PM Review"
            # work across projects at all.
            stage_id = None
            if wanted_stage:
                stage_id = next((st.id for st in stages if st.name == wanted_stage), None)
                if stage_id is None:
                    continue    # this project simply has no such stage
            for t in identity.tasks(cp.odoo_project_id, stage_id=stage_id):
                rows.append((cp, t))
        except OdooError as exc:
            log.warning("tasks for project %s of %s failed: %s",
                        cp.odoo_project_id, client.slug, exc)
            ctx["partial"].append((cp, str(exc)))

    rows.sort(key=lambda r: (r[1].priority == "0", -(r[1].write_date.timestamp()
                                                     if r[1].write_date else 0)))
    ctx["tasks"] = rows
    ctx["counts"] = seen_stages
    ctx["stage_names"] = sorted(seen_stages, key=lambda n: (-seen_stages[n], n))
    # What each task's last review concluded, so the list can show a verdict
    # instead of offering to start a review that has already been run.
    ctx["runs"] = review.latest_by_task(client.id, [t.id for _cp, t in rows])
    return deps.render(request, "client_tasks.html", ctx)


@router.get("/clients/{client_id}/tasks/{task_id}/detail")
def task_detail(request: Request, client_id: int, task_id: int):
    """The expanded body of one task: description and attachments.

    Loaded on demand rather than with the list. Descriptions are HTML of
    arbitrary length and most rows are never opened, so fetching every one would
    make the common case pay for the rare one.
    """
    try:
        deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    client, err = _client_or_404(request, client_id)
    if err:
        return err

    ctx = {"client": client, "task_id": task_id, "detail": None,
           "description": "", "dropped_images": 0, "error": None}
    try:
        identity = projects.connect(request.app.state.config)
        detail = identity.task_detail(task_id)
        if not detail:
            ctx["error"] = "That task no longer exists in Odoo."
        else:
            ctx["detail"] = detail
            # Odoo images need an authenticated session, so they are re-pointed
            # at our proxy; everything else is reduced to an allowlist.
            # Images stay off: the service account cannot read ir.attachment
            # unless it is an Internal User, so every one would render broken.
            ctx["description"], ctx["dropped_images"] = html_clean.clean(
                detail.description_html,
                image_url=lambda aid: f"/clients/{client_id}/tasks/{task_id}/file/{aid}",
                allow_images=False)
    except (projects.NotConfigured, OdooError) as exc:
        ctx["error"] = str(exc)
    return deps.render(request, "_task_detail.html", ctx)


@router.get("/clients/{client_id}/tasks/{task_id}/file/{attachment_id}")
def task_attachment(request: Request, client_id: int, task_id: int, attachment_id: int):
    """Stream an attachment through this app.

    Proxied rather than linked because `/web/image/...` and `/web/content/...`
    only resolve for someone holding an Odoo session, and the point of showing
    the description here is that the reader does not need one.

    The attachment is checked to belong to the task being viewed. Without that
    this route would be an authenticated read of *any* attachment in the Odoo
    database by id, which is a much larger permission than it looks.
    """
    try:
        deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)

    try:
        identity = projects.connect(request.app.state.config)
        files, _err = identity.attachments_of(task_id)
        allowed = {a.id for a in files}
        if attachment_id not in allowed:
            return Response("Not found", status_code=404)
        found = identity.attachment_bytes(attachment_id)
    except (projects.NotConfigured, OdooError) as exc:
        log.warning("attachment %s for task %s failed: %s", attachment_id, task_id, exc)
        return Response("Unavailable", status_code=502)

    if not found:
        return Response("Not found", status_code=404)
    content, mimetype, filename = found
    return Response(
        content, media_type=mimetype,
        headers={
            # inline so images and PDFs render in place; the filename still
            # applies if the reader chooses to save it.
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
            # Belt and braces against a crafted SVG or HTML attachment being
            # treated as same-origin script by the browser.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        },
    )


@router.post("/clients/{client_id}/tasks/{task_id}/review")
async def start_review(request: Request, client_id: int, task_id: int):
    """Open a run for one task and hand off to the run page.

    The phases themselves are not run here. A full review is minutes of model
    calls and browser work, and holding an HTTP request open for that would tie
    the run's life to a socket — a closed tab would abandon a run that is still
    holding the client's staging instance. So this creates the run and returns;
    the run page drives it forward.
    """
    try:
        session = deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    client = clients_mod.get(client_id)
    if not client:
        return deps.render(request, "error.html",
                           {"code": 404, "message": "No such client."}, status_code=404)

    cfg = request.app.state.config
    snap = repo_sync.load(client_id)
    name = ""
    try:
        detail = projects.connect(cfg).task_detail(task_id)
        name = detail.name if detail else ""
    except (OdooError, projects.NotConfigured):
        # A missing title is cosmetic; refusing to start a review over it is not.
        log.info("Could not read task %s's title; starting the run anyway", task_id)

    try:
        run = review.start(
            client_id, task_id, name,
            commit_sha=(snap.commit_sha if snap else ""),
            persona_key="primary", started_by=session.user.id)
    except review.ReviewError as exc:
        existing = review.active_for_client(client_id)
        if existing:
            return RedirectResponse(f"/runs/{existing.id}?busy=1", status_code=303)
        return deps.render(request, "error.html",
                           {"code": 409, "message": str(exc)}, status_code=409)

    log.info("Review run %s opened for task %s (%s) by %s",
             run.id, task_id, client.slug, session.user.login)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)
