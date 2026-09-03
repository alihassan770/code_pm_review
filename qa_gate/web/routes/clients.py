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
from starlette.concurrency import run_in_threadpool

from ... import app_secrets, clients as clients_mod, personas, projects, repos as repos_mod
from ...clients import HOSTING_PLATFORMS, ODOO_VERSIONS, ClientError
from ...repos import BRANCH_MODES, RepoError
from ...personas import PersonaError
from ...projects import NotConfigured
from ...odoo_client import OdooAuthError, OdooClient, OdooError
from .. import deps

log = logging.getLogger(__name__)
router = APIRouter()

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,48}$")
GITHUB_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _form_context(request, **overrides) -> dict:
    ctx = {
        "platforms": HOSTING_PLATFORMS,
        "versions": ODOO_VERSIONS,
        "branch_modes": BRANCH_MODES,
        "error": None, "lookup_error": None,
        "client": None, "values": {}, "repos": None,
        "client_projects": None, "stage_names": [], "stage_counts": {},
        "task_preview": [], "preview_stage": "",
        "persona_saved": False,
        "service_configured": app_secrets.is_configured(app_secrets.IDENTITY_RPC),
        "all_projects": [], "projects_error": "",
    }
    ctx.update(overrides)
    if not ctx["all_projects"]:
        ctx["all_projects"], ctx["projects_error"] = _project_choices(request)
    return ctx


#: Every active project, not a page of them. At ~126 projects this is a few
#: kilobytes of HTML and the filter can then be instant and client-side; paging
#: it would trade that for a round trip per keystroke to solve a problem this
#: installation does not have.
MAX_PROJECT_CHOICES = 400


def _project_choices(request) -> tuple[list, str]:
    """The projects a person can pick from, or why there are none.

    Offered as a list because asking someone to type an id means asking them to
    go and find it in another tab first.
    """
    if not app_secrets.is_configured(app_secrets.IDENTITY_RPC):
        return [], ("No Odoo service credential is set, so the project list cannot "
                    "be loaded.")
    try:
        found = projects.connect(request.app.state.config).search_projects(
            "", limit=MAX_PROJECT_CHOICES)
    except NotConfigured as exc:
        return [], str(exc)
    except (OdooAuthError, OdooError) as exc:
        return [], f"The project list could not be read from Odoo: {exc}"
    return found, ""


def _project_rows(form) -> list[dict]:
    """Just the ids. The name is looked up, never submitted."""
    return [{"odoo_project_id": raw.strip()}
            for raw in form.getlist("project_id") if (raw or "").strip()]


def _lookup_projects(request, ctx: dict, rows: list[dict],
                     stage_name: str = "") -> None:
    """Resolve each project id, collect the stages they share, and preview tasks.

    Stages are gathered by NAME across every project, because ids differ per
    project — that is what makes one stage setting mean the same thing for a
    client with three projects.

    The project name is read here rather than typed: an id identifies a project,
    so asking a person to also write its name is asking them to keep a copy in
    sync by hand.

    A failure is never fatal to the form. Someone should be able to record a
    client while Odoo is unreachable and fill in the detail later.
    """
    if not rows:
        return
    try:
        identity = projects.connect(request.app.state.config)
    except NotConfigured as exc:
        ctx["lookup_error"] = str(exc)
        return

    resolved, counts, missing, preview = [], {}, [], []
    for r in rows:
        raw = str(r.get("odoo_project_id") or "").strip()
        if not raw.isdigit():
            missing.append(f"{raw!r} is not a numeric id")
            continue
        pid = int(raw)
        try:
            project = identity.project(pid)
            if not project:
                missing.append(f"no project with id {pid}")
                continue
            resolved.append({"odoo_project_id": pid, "odoo_project_name": project.name})

            stages = identity.stages(pid)
            by_id = identity.task_counts_by_stage(pid)
            for st in stages:
                counts[st.name] = counts.get(st.name, 0) + by_id.get(st.id, 0)

            # Show what the setting will actually select. A stage name and a
            # count are abstract; the task titles are the thing being claimed.
            if stage_name:
                stage_id = next((st.id for st in stages if st.name == stage_name), None)
                if stage_id is not None:
                    for t in identity.tasks(pid, stage_id=stage_id):
                        preview.append((project.name, t))
        except OdooError as exc:
            missing.append(f"#{pid}: {exc}")

    ctx["client_projects"] = resolved or None
    ctx["stage_counts"] = counts
    ctx["stage_names"] = sorted(counts, key=lambda n: (-counts[n], n))
    ctx["task_preview"] = preview
    ctx["preview_stage"] = stage_name
    if missing:
        ctx["lookup_error"] = "; ".join(missing)


def _repo_rows(form) -> list[dict]:
    """The repeated repository fields, zipped back into rows."""
    gh = form.getlist("repo_github")
    base = form.getlist("repo_base_branch")
    mode = form.getlist("repo_branch_mode")
    rows = []
    for i, g in enumerate(gh):
        if not (g or "").strip():
            continue
        rows.append({
            "github": g,
            "base_branch": base[i] if i < len(base) else "staging",
            "branch_mode": mode[i] if i < len(mode) else "per_task",
        })
    return rows


def _check_repo_rows(form) -> str | None:
    """Validate repository rows before anything is written.

    Order matters here. Repositories are saved after the client row exists, so
    validating them afterwards would leave a half-made client behind every
    typo — and the slug is unique, so the retry then fails for a second,
    unrelated reason. Check first, write once.
    """
    try:
        for row in _repo_rows(form):
            repos_mod.normalize_github(row["github"])
            repos_mod.validate_base_branch(row["base_branch"])
    except RepoError as exc:
        return str(exc)
    return None


def _save_related(client_id: int, form, ctx: dict, *, secret_key: str,
                  user_id: int) -> str | None:
    """Repositories, project link and browser persona. Returns an error or None."""
    try:
        repos_mod.replace_all(client_id, _repo_rows(form))
    except RepoError as exc:
        return str(exc)

    rows = ctx.get("client_projects") or _project_rows(form)
    try:
        clients_mod.set_projects(client_id, rows,
                                 stage_name=(form.get("task_stage_name") or "").strip())
    except ClientError as exc:
        return str(exc)

    login = (form.get("persona_login") or "").strip()
    password = form.get("persona_password") or ""
    if login:
        try:
            personas.save(client_id, key="primary", label="Primary browser user",
                          login=login, password=password, secret_key=secret_key,
                          updated_by=user_id)
        except PersonaError as exc:
            return str(exc)
    return None


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
    return deps.render(request, "client_form.html", _form_context(request))


@router.post("/clients/new")
async def create_client(request: Request):
    try:
        session = deps.require_session(request)
    except deps.NotLoggedIn:
        return deps.login_redirect(request)
    form = await request.form()
    deps.verify_csrf(session, form.get("csrf_token"))

    values = {k: v for k, v in form.items()}
    ctx = _form_context(request, values=values, repos=_repo_rows(form) or None)
    _lookup_projects(request, ctx, _project_rows(form),
                     (form.get("task_stage_name") or "").strip())

    # "Look up" re-renders with the project resolved rather than saving, so the
    # stage list can be chosen from before committing to anything.
    if form.get("action") == "lookup":
        return deps.render(request, "client_form.html", ctx)

    name = (form.get("name") or "").strip()
    # Derived, not typed. The form stopped asking for a handle; it is still what
    # the CLI and the log lines use, so it is made here from the name.
    slug = (form.get("slug") or "").strip().lower() or derive_slug(name)
    error = _validate(slug, name, form) or _check_repo_rows(form)
    if error:
        ctx["error"] = error
        return deps.render(request, "client_form.html", ctx)
    try:
        client = clients_mod.create(
            slug=slug, name=name, created_by=session.user.id, **_fields(form))
    except ClientError as exc:
        ctx["error"] = str(exc)
        return deps.render(request, "client_form.html", ctx)

    cfg = request.app.state.config
    related_error = _save_related(client.id, form, ctx,
                                  secret_key=cfg.secret_key, user_id=session.user.id)
    if related_error:
        # The client exists; only the extras failed. Say so rather than implying
        # nothing was saved, and land them on the edit form to finish.
        ctx.update(client=clients_mod.get(client.id),
                   error=f"Client created, but: {related_error}")
        return deps.render(request, "client_form.html", ctx)

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
    return deps.render(request, "client_detail.html", {
        "client": client,
        "client_projects": clients_mod.projects_for(client.id),
        "repos": repos_mod.for_client(client.id),
        "client_personas": personas.for_client(client.id),
    })


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
    ctx = _form_context(request, client=client, values=dict(client.__dict__),
                        repos=[r.__dict__ for r in repos_mod.for_client(client.id)] or None,
                        persona_saved=any(p.key == "primary"
                                          for p in personas.for_client(client.id)))
    ctx["values"]["task_stage_name"] = client.task_stage_name
    existing = personas.for_client(client.id)
    primary = next((p for p in existing if p.key == "primary"), None)
    if primary:
        ctx["values"]["persona_login"] = primary.login
    _lookup_projects(request, ctx,
                     [{"odoo_project_id": str(cp.odoo_project_id)}
                      for cp in clients_mod.projects_for(client.id)],
                     client.task_stage_name)
    return deps.render(request, "client_form.html", ctx)


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
    form = await request.form()
    deps.verify_csrf(session, form.get("csrf_token"))

    values = {k: v for k, v in form.items()}
    ctx = _form_context(request, client=client, values=values,
                        repos=_repo_rows(form) or None,
                        persona_saved=any(p.key == "primary"
                                          for p in personas.for_client(client_id)))
    _lookup_projects(request, ctx, _project_rows(form),
                     (form.get("task_stage_name") or "").strip())

    if form.get("action") == "lookup":
        return deps.render(request, "client_form.html", ctx)

    name = (form.get("name") or "").strip()
    error = _validate(client.slug, name, form, slug_required=False) or _check_repo_rows(form)
    if error:
        ctx["error"] = error
        return deps.render(request, "client_form.html", ctx)

    clients_mod.update(client_id, name=name,
                       active=form.get("active") == "on", **_fields(form, client))
    cfg = request.app.state.config
    related_error = _save_related(client_id, form, ctx,
                                  secret_key=cfg.secret_key, user_id=session.user.id)
    if related_error:
        ctx.update(client=clients_mod.get(client_id), error=related_error)
        return deps.render(request, "client_form.html", ctx)
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
        return deps.render(request, "client_detail.html", {
            "client": client, "cred_error": message,
            "client_projects": clients_mod.projects_for(client.id),
            "repos": repos_mod.for_client(client.id),
            "client_personas": personas.for_client(client.id),
        }, status_code=200)

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


@router.post("/clients/{client_id}/access-mode")
async def set_access_mode(request: Request, client_id: int):
    """Choose browser sign-in or API key for this client.

    Stored rather than inferred, so `instance.connect` honours a choice instead
    of falling back between the two. A silent fallback would let a revoked API
    key look like a healthy instance reached as a different user, and the audit
    would then attribute its findings to the wrong account.
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

    mode = (form.get("access_mode") or "").strip()
    if mode not in ("browser", "api_key"):
        # The column carries a CHECK constraint; refusing here keeps the failure
        # a redirect rather than a 500 from Postgres.
        return RedirectResponse(f"/clients/{client_id}", status_code=303)

    clients_mod.set_access_mode(client_id, mode)
    log.info("Access mode for %s set to %s by %s", client.slug, mode, session.user.login)
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/personas/{persona_id}/verify")
async def verify_persona(request: Request, client_id: int, persona_id: int):
    """Prove one browser login can actually open a web session.

    A persona is stored without being checked, because the password is typed on
    the same form as several others and failing the whole save over one of them
    would be worse than saving them all and checking after. That trade only
    works if the check is reachable, though — and until now it was not: the
    badge on the client page said "Not verified yet" and there was nothing
    anywhere that could ever change it.

    `personas.verify` uses `open_session`, not `authenticate`, because an Odoo
    API key passes the second and fails the first. Discovering that mid-run, as
    a screenshot flow that cannot log in, is exactly what this prevents.
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

    cfg = request.app.state.config
    try:
        # Reaching a client's Odoo is network I/O and can be slow; off the event
        # loop so one unreachable instance does not stall every other request.
        persona = await run_in_threadpool(
            personas.verify, persona_id, client, cfg.secret_key)
    except PersonaError as exc:
        return deps.render(request, "client_detail.html", {
            "client": client, "cred_error": str(exc),
            "client_projects": clients_mod.projects_for(client.id),
            "repos": repos_mod.for_client(client.id),
            "client_personas": personas.for_client(client.id),
        }, status_code=200)

    log.info("Persona %s for %s verified by %s: %s", persona.key, client.slug,
             session.user.login, persona.state)
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


# ---- validation ------------------------------------------------------------

def derive_slug(name: str) -> str:
    """A command-line handle from the client's name.

    The form no longer asks for one. It exists for `qa-gate audit <slug>` and for
    tagging log lines, which is a reason to have it and not a reason to make
    somebody think one up.
    """
    base = re.sub(r"[^a-z0-9]+", "", (name or "").lower())[:49]
    if len(base) < 2:
        base = f"client{base}"
    candidate, n = base, 2
    while clients_mod.get_by_slug(candidate):
        suffix = str(n)
        candidate = base[: 49 - len(suffix)] + suffix
        n += 1
    return candidate


def _validate(slug: str, name: str, form: dict, *, slug_required: bool = True) -> str | None:
    if slug_required and not SLUG_RE.match(slug or ""):
        return ("Slug must be lowercase letters, numbers, hyphens or underscores, "
                "2 to 49 characters.")
    if not name:
        return "Name is required."
    url = (form.get("staging_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return "Staging URL must start with http:// or https://"
    return None


def _fields(form: dict, existing=None) -> dict:
    """The client's own columns, from the form.

    `existing` matters for fields the form no longer shows. The database name
    pattern was removed from the UI, and without this an edit would send nothing
    and silently reset a stored pattern to the default on every save. A field
    that has left the form must keep its value, not acquire a new one.
    """
    keep = lambda key, default: (                       # noqa: E731
        form.get(key) if form.get(key) is not None
        else (getattr(existing, key, None) or default))
    return {
        "odoo_version": form.get("odoo_version", "17.0"),
        "hosting_platform": form.get("hosting_platform", "other"),
        "staging_url": form.get("staging_url", ""),
        "staging_db": form.get("staging_db", ""),
        "db_name_pattern": keep("db_name_pattern", "%_staging"),
        "branch_mode": keep("branch_mode", "per_task"),
        "base_branch": keep("base_branch", "main"),
    }
