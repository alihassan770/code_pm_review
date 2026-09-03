"""The landing page once you are logged in.

Phase A shows the client list and what is still missing per client. It is
deliberately honest about emptiness: a dashboard that renders fake zeroes for
capabilities that do not exist yet teaches people to ignore it.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import logging
from datetime import datetime, timezone

from ... import clients as clients_mod, projects, review
from ...odoo_client import OdooError

log = logging.getLogger(__name__)
from .. import deps

router = APIRouter()


@router.get("/dashboard")
def dashboard(request: Request):
    try:
        session = deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)

    mine = clients_mod.list_for_user(session.user.id)
    everything = clients_mod.list_all()
    # Admins routinely need to see a client they were never attached to; for
    # everyone else the distinction is what keeps the page short.
    visible = everything if session.user.is_admin else mine
    unattached = [c for c in everything if c not in visible]

    # What the gate has actually concluded. A dashboard for a review tool that
    # says only how many clients exist is a page about setup, not about work.
    ids = [c.id for c in visible]
    return deps.render(request, "dashboard.html", {
        "clients": visible,
        "unattached_count": len(unattached),
        "needs_credentials": [c for c in visible if not c.has_credentials],
        "tally": review.verdict_tally(ids),
        "recent": review.recent(ids),
        "active": review.active_count(ids),
    })


#: Sorts last for a task Odoo gave no write date, without ever being compared
#: against a real one.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: How many waiting tasks the panel lists. Past this it stops being a call to
#: action and becomes a backlog, which is what the client pages are for.
PENDING_SHOWN = 6


@router.get("/dashboard/pending")
def pending_panel(request: Request):
    """Tasks sitting in a review stage that nobody has reviewed yet.

    A fragment, loaded after the page rather than with it. Working this out
    means asking Odoo for the tasks of every project of every visible client,
    which is several round trips and takes seconds on a real account. Blocking
    the dashboard on that would make the whole app feel slow to open, when the
    only slow part is one panel.

    A client whose Odoo call fails is skipped rather than failing the panel: a
    list that is short and says so beats an error where a list should be.
    """
    try:
        session = deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)

    mine = clients_mod.list_for_user(session.user.id)
    visible = clients_mod.list_all() if session.user.is_admin else mine

    try:
        identity = projects.connect(request.app.state.config)
    except projects.NotConfigured:
        return deps.render(request, "_dashboard_pending.html",
                           {"waiting": [], "stalled": 0, "unconfigured": True,
                            "total": 0})

    waiting: list[dict] = []
    stalled = 0          # clients we could not read, so the count is honest
    for client in visible:
        client = clients_mod.with_projects(client)
        stage_name = (client.task_stage_name or "").strip()
        if not client.has_project or not stage_name:
            # No review stage configured means no opinion about what is
            # waiting. Guessing one would invent work.
            continue
        found = []
        try:
            for cp in client.projects:
                stages = identity.stages(cp.odoo_project_id)
                stage_id = next((st.id for st in stages if st.name == stage_name), None)
                if stage_id is None:
                    continue
                found += identity.tasks(cp.odoo_project_id, stage_id=stage_id)
        except OdooError as exc:
            log.info("pending tasks for %s could not be read: %s", client.slug, exc)
            stalled += 1
            continue

        # Only the ones with no verdict on record. A task reviewed to `fail` is
        # not waiting for a review, it is waiting for a developer.
        reviewed = review.verdicts_by_task(client.id, [t.id for t in found])
        for t in found:
            if t.id not in reviewed:
                waiting.append({"client": client, "task": t})

    # Urgent first, then oldest, so the top of the list is the thing to do next
    # rather than the thing most recently touched.
    # Urgent first, then least recently touched. `write_date` can be None, and
    # `or 0` here would compare an int against a datetime and raise, so missing
    # dates sort last through a separate key rather than a substitute value.
    waiting.sort(key=lambda w: (not getattr(w["task"], "is_urgent", False),
                                w["task"].write_date is None,
                                w["task"].write_date or _EPOCH))
    return deps.render(request, "_dashboard_pending.html", {
        "waiting": waiting[:PENDING_SHOWN],
        "total": len(waiting),
        "stalled": stalled,
        "unconfigured": False,
    })
