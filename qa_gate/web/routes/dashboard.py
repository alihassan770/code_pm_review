"""The landing page once you are logged in.

Phase A shows the client list and what is still missing per client. It is
deliberately honest about emptiness: a dashboard that renders fake zeroes for
capabilities that do not exist yet teaches people to ignore it.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ... import clients as clients_mod
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

    return deps.render(request, "dashboard.html", {
        "clients": visible,
        "unattached_count": len(unattached),
        "needs_credentials": [c for c in visible if not c.has_credentials],
    })
