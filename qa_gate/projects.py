"""Reading projects, stages and tasks from *our* Odoo.

This talks to the identity Odoo — the one holding `project.task` — not to a
client's staging instance. The two are constantly confused when reading this
codebase, so as a rule of thumb: anything in `instance.py` / `census.py` /
`audit.py` points at a client's Odoo, anything here points at ours.

Tasks are read live rather than mirrored into Postgres. A cached task list is
wrong the moment a project manager drags a card, and the whole premise of the
gate is that the Odoo task is the source of truth (plan §1). The cost is one RPC
round trip per page view, which is a few hundred milliseconds against a handful
of rows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from . import app_secrets, config as config_mod
from .odoo_client import OdooAuthError, OdooClient, OdooError

log = logging.getLogger(__name__)

TASK_MODEL = "project.task"
PROJECT_MODEL = "project.project"
STAGE_MODEL = "project.task.type"

MAX_TASKS = 200
MAX_PROJECT_HITS = 30
ATTACHMENT_MODEL = "ir.attachment"
#: Refuse to stream anything larger than this through the app. Attachments are
#: proxied because they need Odoo authentication, not because we want to be a
#: file server; a 200 MB video would pin a worker for no good reason.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class NotConfigured(Exception):
    """No identity Odoo, or no service credential for it."""


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    company: str = ""
    task_count: int = 0


@dataclass(frozen=True)
class Stage:
    id: int
    name: str
    sequence: int = 0


@dataclass(frozen=True)
class Attachment:
    id: int
    name: str
    mimetype: str
    size: int

    @property
    def is_image(self) -> bool:
        return self.mimetype.startswith("image/")

    @property
    def is_pdf(self) -> bool:
        return self.mimetype == "application/pdf"

    @property
    def human_size(self) -> str:
        n = float(self.size or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} GB"


@dataclass(frozen=True)
class TaskDetail:
    id: int
    name: str
    description_html: str
    attachments: list = field(default_factory=list)
    #: Why the attachment list is empty, when it is empty for a reason other
    #: than there being none. Kept separate from an empty list because "this
    #: task has no files" and "we are not allowed to look" are different facts
    #: and only one of them is the reader's problem to act on.
    attachments_error: str = ""


@dataclass(frozen=True)
class Task:
    id: int
    name: str
    stage_id: int
    stage_name: str
    assignees: list[str] = field(default_factory=list)
    priority: str = "0"
    allocated_hours: float = 0.0
    deadline: str = ""
    write_date: datetime | None = None
    url: str = ""

    @property
    def is_urgent(self) -> bool:
        # Odoo stores priority as a selection string; anything above '0' is starred.
        return self.priority not in ("0", "", None)


class Identity:
    """An authenticated connection to our own Odoo, using the service account."""

    def __init__(self, client: OdooClient, uid: int, secret: str, base_url: str) -> None:
        self.client = client
        self.uid = uid
        self.secret = secret
        self.base_url = base_url

    def read(self, model: str, method: str, args, kwargs=None):
        return self.client.execute_kw(self.uid, self.secret, model, method, args, kwargs or {})

    # ---- projects ----------------------------------------------------------

    def search_projects(self, query: str = "", limit: int = MAX_PROJECT_HITS) -> list[Project]:
        """Find projects by id or by name fragment.

        A bare number is treated as an id first, because that is what the client
        form asks for and an exact id match should never be buried under name
        matches that happen to contain the same digits.
        """
        domain: list = []
        query = (query or "").strip()
        if query.isdigit():
            domain = ["|", ("id", "=", int(query)), ("name", "ilike", query)]
        elif query:
            domain = [("name", "ilike", query)]

        rows = self.read(PROJECT_MODEL, "search_read",
                         [domain], {"fields": ["name", "company_id"],
                                    "limit": limit, "order": "name"})
        return [Project(id=r["id"], name=r["name"],
                        company=_m2o_name(r.get("company_id"))) for r in rows]

    def project(self, project_id: int) -> Project | None:
        rows = self.read(PROJECT_MODEL, "read",
                         [[int(project_id)]], {"fields": ["name", "company_id"]})
        if not rows:
            return None
        r = rows[0]
        return Project(id=r["id"], name=r["name"], company=_m2o_name(r.get("company_id")))

    # ---- stages ------------------------------------------------------------

    def stages(self, project_id: int) -> list[Stage]:
        """Stages available on a project, in board order.

        `project.task.type` is shared between projects via a many2many, so this
        filters rather than assuming a stage belongs to one project.
        """
        rows = self.read(STAGE_MODEL, "search_read",
                         [[("project_ids", "in", [int(project_id)])]],
                         {"fields": ["name", "sequence"], "order": "sequence, id"})
        return [Stage(id=r["id"], name=r["name"], sequence=r.get("sequence") or 0)
                for r in rows]

    # ---- tasks -------------------------------------------------------------

    def tasks(self, project_id: int, *, stage_id: int | None = None,
              limit: int = MAX_TASKS) -> list[Task]:
        domain: list = [("project_id", "=", int(project_id))]
        if stage_id:
            domain.append(("stage_id", "=", int(stage_id)))

        fields = ["name", "stage_id", "priority", "date_deadline", "write_date",
                  "allocated_hours", "user_ids"]
        try:
            rows = self.read(TASK_MODEL, "search_read", [domain],
                             {"fields": fields, "limit": limit,
                              "order": "priority desc, write_date desc"})
        except OdooError as exc:
            # Field names drift across versions — `user_ids` replaced `user_id`,
            # and `allocated_hours` was `planned_hours` before 17. Rather than
            # branch on a version string we do not always know, retry with the
            # portable subset and lose a column instead of the whole page.
            log.info("task search_read with full fields failed (%s); retrying lean", exc)
            rows = self.read(TASK_MODEL, "search_read", [domain],
                             {"fields": ["name", "stage_id", "priority", "write_date"],
                              "limit": limit, "order": "write_date desc"})

        out: list[Task] = []
        for r in rows:
            stage = r.get("stage_id") or []
            out.append(Task(
                id=r["id"], name=r.get("name") or f"Task {r['id']}",
                stage_id=stage[0] if stage else 0,
                stage_name=_m2o_name(stage),
                assignees=_names(r.get("user_ids")),
                priority=str(r.get("priority") or "0"),
                allocated_hours=float(r.get("allocated_hours") or 0),
                deadline=r.get("date_deadline") or "",
                write_date=_parse(r.get("write_date")),
                url=task_url(self.base_url, r["id"]),
            ))
        return out

    def task_detail(self, task_id: int) -> TaskDetail | None:
        """The description and attachments for one task.

        Fetched on expand rather than with the list: descriptions are HTML of
        arbitrary length and most rows are never opened, so loading them all
        would make the common case pay for the rare one.
        """
        rows = self.read(TASK_MODEL, "read", [[int(task_id)]],
                         {"fields": ["name", "description"]})
        if not rows:
            return None
        row = rows[0]
        files, files_error = self.attachments_of(task_id)
        return TaskDetail(
            id=row["id"], name=row.get("name") or "",
            description_html=row.get("description") or "",
            attachments=files, attachments_error=files_error,
        )

    def attachments_of(self, task_id: int) -> tuple[list[Attachment], str]:
        """(files, error). Includes images embedded in the description.

        Odoo stores inline description images as attachments too, which is what
        makes the `/web/image/<id>` URLs proxyable by attachment id.

        Returns the reason rather than swallowing it. `ir.attachment` requires
        **Internal User** (`base.group_user`); a portal account can read tasks
        perfectly well and still be refused every attachment. Reporting that as
        "no attachments" would send someone looking for a missing file that is
        actually right there in Odoo.
        """
        try:
            rows = self.read(ATTACHMENT_MODEL, "search_read",
                             [[("res_model", "=", TASK_MODEL), ("res_id", "=", int(task_id))]],
                             {"fields": ["name", "mimetype", "file_size"],
                              "order": "id"})
        except OdooError as exc:
            message = str(exc)
            log.info("could not list attachments for task %s: %s", task_id, message)
            if "AccessError" in message or "not allowed to access" in message:
                return [], (
                    "Attachments cannot be read with the current Odoo service "
                    "account. Odoo restricts ir.attachment to Internal Users, and "
                    "this account is not one. Descriptions still work; files and "
                    "inline images need an internal account."
                )
            return [], f"Attachments could not be read: {message}"
        return ([Attachment(id=r["id"], name=r.get("name") or f"file-{r['id']}",
                            mimetype=r.get("mimetype") or "application/octet-stream",
                            size=int(r.get("file_size") or 0)) for r in rows], "")

    def attachment_bytes(self, attachment_id: int) -> tuple[bytes, str, str] | None:
        """(content, mimetype, filename), or None.

        Streamed through this app rather than linked directly, because a
        `/web/image/...` URL only resolves for someone with an Odoo session —
        and the whole point is that the reader does not need one.
        """
        import base64
        rows = self.read(ATTACHMENT_MODEL, "read", [[int(attachment_id)]],
                         {"fields": ["name", "mimetype", "datas", "file_size"]})
        if not rows:
            return None
        row = rows[0]
        if int(row.get("file_size") or 0) > MAX_ATTACHMENT_BYTES:
            return None
        raw = row.get("datas")
        if not raw:
            return None
        try:
            content = base64.b64decode(raw)
        except (ValueError, TypeError):
            return None
        return (content,
                row.get("mimetype") or "application/octet-stream",
                row.get("name") or f"attachment-{attachment_id}")

    # ---- writing back ------------------------------------------------------

    @staticmethod
    def _message_id(raw) -> int:
        """The id of the message that was posted, however Odoo phrased it.

        A list on every version we support, because the server turns the
        returned recordset into `.ids`. Handled rather than assumed, since a
        future version returning a bare id would otherwise break the other way,
        and the id is only used for logging: refusing to parse it must never
        cost a note that was posted successfully.
        """
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.info("message_post returned %r, which is not an id", raw)
            return 0

    def can_post_html(self) -> bool:
        """Whether this credential's HTML would survive `message_post`.

        `mail_thread.message_post` escapes a plain string body, and the only
        RPC-reachable escape hatch, `body_is_html=True`, is honoured **only for
        an internal user**:

            if body_is_html and self.env.user._is_internal():

        A portal service account therefore cannot post formatted text at all,
        and sending it markup produces a note reading `<p><b>PM REVIEW SUMMARY`
        as literal characters. So this is asked rather than assumed, and a
        failure to answer means plain text, which is always readable.
        """
        try:
            rows = self.read("res.users", "read", [[self.uid]], {"fields": ["share"]})
        except (OdooError, OdooAuthError) as exc:
            log.info("could not read the service account's share flag: %s", exc)
            return False
        return bool(rows) and not rows[0].get("share", True)

    def post_note(self, task_id: int, body_html: str, *, is_html: bool = False) -> int:
        """Log an internal note on a task's chatter. Returns the message id.

        **A log note, never a message.** Odoo's chatter has two kinds of post and
        the difference is who finds out: `mail.mt_comment` notifies every
        follower and emails the ones who are customers, while `mail.mt_note` is
        the internal "Log note" tab and reaches nobody. A review summary is for
        the team, and a tool that mails a client every time it finishes a run
        would be switched off within a week.

        **No attachments, ever.** `attachment_ids` is not passed and no argument
        exposes it: the note carries its text and nothing else.

        The fallback exists because `subtype_xmlid` is keyword-only and has
        moved between versions. `message_type='notification'` with no subtype
        also lands as a note on 17 through 19, so a version that rejects the
        first form still posts rather than losing the summary.

        **The return value is a list, not an id.** `message_post` returns a
        `mail.message` record, and `odoo/service/model.py` serialises any
        returned recordset to `result.ids` before it leaves the server. So this
        gets `[1234]` back and `int()` on it raised

            int() argument must be a string, a bytes-like object or a real
            number, not 'list'

        which surfaced as "the summary could not be posted" on a run where the
        note had in fact been posted a moment earlier. The write had already
        happened; only our reading of the answer failed.
        """
        body = (body_html or "").strip()
        if not body:
            raise OdooError("Refusing to post an empty note.")
        try:
            return self._message_id(self.read(
                TASK_MODEL, "message_post", [[int(task_id)]],
                {"body": body, "message_type": "comment",
                 "subtype_xmlid": "mail.mt_note",
                 **({"body_is_html": True} if is_html else {})},
            ))
        except (OdooError, OdooAuthError) as exc:
            log.info("message_post with an explicit subtype failed on task %s "
                     "(%s); retrying as a plain notification", task_id, exc)
            return self._message_id(self.read(
                TASK_MODEL, "message_post", [[int(task_id)]],
                {"body": body, "message_type": "notification",
                 **({"body_is_html": True} if is_html else {})},
            ))

    def task_counts_by_stage(self, project_id: int) -> dict[int, int]:
        """How many tasks sit in each stage, for the stage picker.

        `read_group` rather than counting client-side: the point of showing the
        number is to choose a stage without loading every task in the project.
        """
        try:
            rows = self.read(TASK_MODEL, "read_group",
                             [[("project_id", "=", int(project_id))], ["id"], ["stage_id"]],
                             {"lazy": True})
        except OdooError as exc:
            log.info("read_group on tasks failed, stage counts unavailable: %s", exc)
            return {}
        counts: dict[int, int] = {}
        for r in rows:
            stage = r.get("stage_id") or []
            if stage:
                counts[stage[0]] = r.get("stage_id_count") or r.get("__count") or 0
        return counts


# ---- connecting ------------------------------------------------------------

def connect(cfg=None) -> Identity:
    """Authenticate to our Odoo with the stored service account."""
    cfg = cfg or config_mod.load()
    if not cfg.odoo.configured:
        raise NotConfigured(
            "No identity Odoo is configured. Set it on the setup page first.")

    cred = app_secrets.get(app_secrets.IDENTITY_RPC, cfg.secret_key)
    if not cred.configured:
        raise NotConfigured(
            "No service credential for reading Odoo tasks. Add one under "
            "Settings → Odoo connection. It needs an API key, not a password, "
            "if the account has two-factor authentication enabled."
        )

    client = OdooClient(cfg.odoo.url, cfg.odoo.db)
    try:
        uid = client.authenticate(cred.login, cred.secret)
    except OdooAuthError as exc:
        raise NotConfigured(
            f"Odoo rejected the service credential for {cred.login!r}. {exc}") from exc
    return Identity(client, uid, cred.secret, cfg.odoo.url)


def task_url(base_url: str, task_id: int) -> str:
    """A link a human can open. Kept in one place so the format changes once."""
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/odoo/project/task/{int(task_id)}"


# ---- helpers ---------------------------------------------------------------

def _m2o_name(value) -> str:
    """Odoo many2one reads come back as [id, display_name] or False."""
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    return ""


def _names(value) -> list[str]:
    """user_ids reads back as a list of ids; we only have names when told."""
    if not value:
        return []
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
        return [str(v[1]) for v in value]
    return []


def _parse(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
