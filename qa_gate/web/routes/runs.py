"""The run page: what a review is doing, and the controls to steer it.

A run outlives the request that started it, deliberately. Driving the phases
inside the POST that presses Start would tie the run's life to a socket, and a
closed tab would abandon a run still holding a client's staging instance. So the
work happens in a background thread and this page reports on it.

`advance` is idempotent per phase: it walks to the first phase that is not done
and runs from there. That is what makes resume free — a paused run resumed is
the same call as a fresh run started, and neither replays work already finished.
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from ... import clients as clients_mod, html_clean, projects, review
from ...odoo_client import OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()

#: Runs in flight in this process, so a second Resume cannot start a second
#: worker for the same run. Not a substitute for the database's uniqueness
#: constraint — that one survives a restart and this does not — but it is what
#: stops a double-click spawning two threads.
_workers: dict[int, threading.Thread] = {}
_lock = threading.Lock()


def _guard(request: Request):
    try:
        return deps.require_session(request), None
    except deps.NotLoggedIn:
        return None, deps.login_redirect(request)


def _run_or_404(request: Request, run_id: int):
    run = review.get(run_id)
    if run:
        return run, None
    return None, deps.render(request, "error.html",
                             {"code": 404, "message": "No such review run."},
                             status_code=404)


def _kick(run_id: int, cfg) -> None:
    """Start the phase loop in the background, unless it is already running."""
    with _lock:
        thread = _workers.get(run_id)
        if thread and thread.is_alive():
            return

        def work() -> None:
            from ... import db
            try:
                run = review.get(run_id)
                if not run:
                    return
                description, images = _task_inputs(cfg, run.task_id)
                review.advance(run_id, secret_key=cfg.secret_key,
                               description=description, images=images)
            except review.Paused:
                log.info("Run %s paused", run_id)
            except Exception as exc:  # noqa: BLE001 - a worker must not die silently
                log.exception("Run %s crashed", run_id)
                try:
                    db.execute(
                        "UPDATE review_runs SET state = 'failed', error = %s, "
                        "finished_at = now() WHERE id = %s", (str(exc)[:2000], run_id))
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(target=work, name=f"review-{run_id}", daemon=True)
        _workers[run_id] = thread
        thread.start()


def _task_inputs(cfg, task_id: int) -> tuple[str, list]:
    """The task's description and any images attached to it.

    Attachments are fetched with the service credential, which today is a portal
    account — `ir.attachment` needs Internal User, so this usually comes back
    empty with a reason. The reason is logged rather than swallowed: a mockup
    that marks where a field goes is a requirement, and silently reviewing
    without it would be reviewing against half the specification.
    """
    try:
        identity = projects.connect(cfg)
        detail = identity.task_detail(task_id)
    except (OdooError, projects.NotConfigured) as exc:
        log.warning("Could not read task %s: %s", task_id, exc)
        return "", []
    if not detail:
        return "", []

    description = html_clean.to_text(detail.description_html or "", limit=100_000)
    images: list = []
    files, error = identity.attachments_of(task_id)
    if error:
        log.info("Task %s attachments unavailable: %s", task_id, error)
    for att in files:
        if not att.is_image:
            continue
        blob = identity.attachment_bytes(att.id)
        if blob:
            images.append((att.name, blob[0], blob[1]))
    return description, images


def _task_url(cfg, task_id: int) -> str:
    """A link to the task that works for whoever clicks it.

    `/mail/view` rather than `/web#id=...&model=project.task`. The backend URL
    is wrong for half our staff: `web_client` in `web/controllers/home.py` ends
    with

        if not is_user_internal(request.session.uid):
            return request.redirect('/web/login_successful', 303)

    so a **portal** user following a backend link never reaches the task, they
    land on a "you are logged in" page with no way back. The fragment does not
    even survive it, since a browser never sends `#...` to the server.

    `/mail/view` is Odoo's own answer to this, and it is what the links in
    notification emails use. It resolves the record, then hands off through
    `_get_access_action`, which branches on `user.share`: an internal user gets
    the backend form, a portal user gets the portal page (`/my/tasks/<id>`),
    and somebody not signed in gets the login page with this URL preserved as
    the redirect. One link, correct for all three.

    No `access_token` is passed, deliberately. A token would grant the record
    to anyone holding the link, and these links sit on a page about a client's
    staging instance. Access stays whatever Odoo already grants the person
    signed in, so a portal user who cannot see the task still cannot, which is
    the correct outcome rather than a bug to route around.
    """
    base = (getattr(cfg.odoo, "url", "") or "").rstrip("/")
    if not base or not task_id:
        return ""
    return f"{base}/mail/view?model=project.task&res_id={task_id}"


@router.get("/runs/{run_id}")
def run_page(request: Request, run_id: int):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    run, missing = _run_or_404(request, run_id)
    if missing:
        return missing

    cfg = request.app.state.config
    # A queued run is one nobody has driven yet — pressing Start created it and
    # redirected here, so this is where it actually begins.
    if run.state == "queued":
        _kick(run_id, cfg)
        run = review.get(run_id)

    return deps.render(request, "run.html", {
        "run": run,
        "client": clients_mod.get(run.client_id),
        "phases": review.PHASES,
        "phase_titles": review.PHASE_TITLES,
        "busy": request.query_params.get("busy") == "1",
        "answers": review.answers_for(run_id),
        "blocked_on": review.resume_blocked_by(run_id),
        "has_plan": any(s.phase == "plan" and s.state == "done" for s in run.steps),
        "elapsed": review.phase_elapsed(run_id),
        "durations": review.phase_durations(run_id),
        "total_secs": review.run_duration(run_id),
        "shots": review.screenshots_for(run_id),
        "created": review.created_records(run_id),
        "groups": review.grouped_progress(run, review.phase_durations(run_id)),
        "result_order": review.RESULT_ORDER,
        "task_url": _task_url(cfg, run.task_id),
        "working": run_id in _workers and _workers[run_id].is_alive(),
    })


@router.get("/runs/{run_id}/shot/{shot_id}")
def run_screenshot(request: Request, run_id: int, shot_id: int):
    """One screenshot's bytes.

    Scoped by run as well as by id so a guessed number cannot pull an image out
    of another client's review.
    """
    session, redirect = _guard(request)
    if redirect:
        return redirect
    found = review.screenshot_bytes(shot_id, run_id)
    if not found:
        return deps.render(request, "error.html",
                           {"code": 404, "message": "No such screenshot."},
                           status_code=404)
    png, mimetype = found
    return Response(png, media_type=mimetype,
                    headers={"Cache-Control": "private, max-age=3600"})


@router.post("/runs/{run_id}/{action}")
async def control(request: Request, run_id: int, action: str):
    session, redirect = _guard(request)
    if redirect:
        return redirect
    run, missing = _run_or_404(request, run_id)
    if missing:
        return missing
    form = dict(await request.form())
    deps.verify_csrf(session, form.get("csrf_token"))

    if action in ("answer", "skip"):
        try:
            index = int(form.get("index") or -1)
        except ValueError:
            index = -1
        if index < 0:
            return RedirectResponse(f"/runs/{run_id}", status_code=303)

        question = form.get("question") or ""
        if action == "skip":
            review.skip_answer(run_id, index, question, by=session.user.login)
        else:
            review.save_answer(run_id, index, question,
                               form.get("answer") or "", by=session.user.login)

        # Nothing is thrown away and nothing restarts. A run that asked a
        # question paused at the moment it asked, so everything after the
        # question is still unbuilt — answering simply unblocks it and it goes
        # forward. Rebuilding from the top would discard phases the answer
        # cannot have invalidated, which is what it used to do.
        if not review.resume_blocked_by(run_id):
            planned = any(s.phase == "plan" and s.state == "done"
                          for s in (review.get(run_id).steps or []))
            if not planned:
                review.request_resume(run_id)
                _kick(run_id, request.app.state.config)
        log.info("Run %s question %s %s by %s", run_id, index,
                 "skipped" if action == "skip" else "answered", session.user.login)

    elif action == "pause":
        review.request_pause(run_id)
        log.info("Run %s pause requested by %s", run_id, session.user.login)
    elif action == "replan":
        # The explicit, opt-in version of what answering used to do silently.
        # Offered only when a plan already exists, because that is the only time
        # an answer arrives too late to simply be picked up.
        review.replan(run_id)
        _kick(run_id, request.app.state.config)
        log.info("Run %s re-planned from scratch by %s", run_id, session.user.login)
    elif action == "resume":
        # Resume is the same call as start. `advance` walks to the first phase
        # that is not done, so nothing already finished is repeated.
        review.request_resume(run_id)
        _kick(run_id, request.app.state.config)
        log.info("Run %s resumed by %s", run_id, session.user.login)
    elif action == "report":
        # Retry the write-back on its own. Offered because the commonest reason
        # it fails is a permission in Odoo, which somebody fixes there and then
        # wants to press again without re-running an hour of scenarios.
        detail = review.retry_report(run_id)
        log.info("Run %s report retried by %s: %s", run_id, session.user.login,
                 "posted" if detail.get("posted") else detail.get("error")
                 or detail.get("skipped"))
    elif action == "cancel":
        review.cancel(run_id, secret_key=request.app.state.config.secret_key)
        log.info("Run %s cancelled by %s", run_id, session.user.login)
    else:
        return deps.render(request, "error.html",
                           {"code": 404, "message": "No such action."}, status_code=404)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)
