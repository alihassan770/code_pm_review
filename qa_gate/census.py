"""Layer 1 of the knowledge base: what the instance says about itself.

Plan §9's central claim is that Odoo already holds the knowledge base and nobody
has to maintain it: `ir.model.data` attributes every field, view, ACL and action
to the module that created it, so over plain authenticated RPC — no shell, no
Postgres — the gate can derive the installed module set, who owns which field,
which module patches whose view, and the Studio customizations that exist in no
repository at all.

Two rules carried straight from the plan:

  * **Never stored.** A census is re-derived every run. A cached one lies the
    moment somebody installs an app through the hosting panel, and the whole
    reason this is affordable — a handful of `search_read` calls taking seconds
    — is what makes caching unnecessary.
  * **`ir.model.data` rather than convenience columns.** Some versions grow a
    `modules` field or similar; `ir.model.data` behaves identically on 17, 18
    and 19, which is the only reason the version adapters in §16 stay thin.

The honest limit, also from §9: metadata says what a module *declares*, not what
its Python does. A third-party `write()` override that breaks your compute leaves
no trace here. Layer 2 (phase D, the AST source map) covers that where we hold
the source; where we do not, an unknown-source module is treated as high risk
rather than assumed inert.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .instance import Connection

log = logging.getLogger(__name__)

# The module Odoo attributes every Studio change to. Records owned by it exist
# in no git repository, which is exactly why they are worth counting.
STUDIO_MODULE = "studio_customization"

# Config parameters we set ourselves. Excluded from the config hash so that our
# own bookkeeping never registers as client drift.
OUR_PARAM_PREFIX = "hst_qa."


@dataclass(frozen=True)
class Module:
    name: str
    version: str
    state: str
    author: str = ""
    summary: str = ""

    @property
    def is_odoo(self) -> bool:
        """Whether this is core/OCA-published rather than someone's custom work.

        Author string rather than a module-name prefix: `hst_` and `x_` tell you
        about our conventions, and the census has to be right about clients
        whose previous partner used none of them.
        """
        a = (self.author or "").lower()
        return "odoo s.a" in a or "odoo sa" in a or a.startswith("odoo")


@dataclass(frozen=True)
class ViewPatch:
    """One inherited view, and the module that owns the patch."""
    view_id: int
    name: str
    model: str
    inherit_id: int | None
    module: str


@dataclass
class Census:
    taken_at: datetime
    server_version: str
    modules: list[Module] = field(default_factory=list)
    config_params: dict[str, str] = field(default_factory=dict)
    view_count: int = 0
    view_max_write: str = ""
    # Fields with state = 'manual': Studio and hand-added columns. The set that
    # repo-based analysis cannot see at all.
    manual_fields: list[dict] = field(default_factory=list)
    studio_records: int = 0
    view_patches: list[ViewPatch] = field(default_factory=list)
    # Module -> the models it adds fields to. §9's "every field on a model and
    # which module added it", narrowed to non-core modules because that is the
    # question the coverage map asks and reading every field on a large instance
    # is the one census query that would stop being cheap.
    module_models: dict[str, list[str]] = field(default_factory=dict)
    # Models this census could not read, with the reason. Recorded rather than
    # dropped so a thin census announces itself instead of looking like a clean
    # instance.
    gaps: dict[str, str] = field(default_factory=dict)

    @property
    def custom_modules(self) -> list[Module]:
        return [m for m in self.modules if not m.is_odoo]

    @property
    def module_names(self) -> set[str]:
        return {m.name for m in self.modules}

    def conflicts(self) -> list[dict]:
        """Views patched by more than one module.

        §9's coverage page calls these out because two modules editing the same
        view is the most common cause of a break that neither module's author
        can reproduce alone.
        """
        by_view: dict[int, set[str]] = defaultdict(set)
        meta: dict[int, ViewPatch] = {}
        for p in self.view_patches:
            if p.inherit_id:
                by_view[p.inherit_id].add(p.module)
                meta[p.inherit_id] = p
        out = []
        for base_id, modules in by_view.items():
            named = {m for m in modules if m}
            if len(named) > 1:
                out.append({
                    "view_id": base_id,
                    "model": meta[base_id].model,
                    "modules": sorted(named),
                })
        return sorted(out, key=lambda c: (-len(c["modules"]), c["model"]))


def take(conn: Connection) -> Census:
    """Read the instance. Read-only: nothing here writes, ever.

    Failures of individual sections are recorded in `gaps` rather than raised.
    A census that returns nine sections and names the tenth as unreadable is
    useful; one that raises because `base.automation` is not installed is not.
    """
    census = Census(
        taken_at=datetime.now(timezone.utc),
        server_version=conn.server_version,
    )
    _modules(conn, census)
    _config(conn, census)
    _views(conn, census)
    _manual_fields(conn, census)
    _module_models(conn, census)
    return census


def _module_models(conn: Connection, census: Census) -> None:
    """Which models each non-core module touches, from field ownership.

    Two calls rather than one read of `ir.model.fields`: ask `ir.model.data`
    for the field ids owned by the modules we care about, then read just those
    fields. Reading every field on an instance with forty modules installed is
    tens of thousands of rows to discard almost all of, and it is the difference
    between a census that takes seconds and one people stop running.

    View-only modules — themes, most notably — legitimately appear here with no
    models at all. That is a fact about them worth showing, not a gap.
    """
    custom = [m.name for m in census.custom_modules]
    if not custom:
        return
    try:
        owned = conn.search_read(
            "ir.model.data",
            [("model", "=", "ir.model.fields"), ("module", "in", custom)],
            ["module", "res_id"],
        )
        if not owned:
            return
        by_field = {r["res_id"]: r["module"] for r in owned}
        fields = conn.search_read(
            "ir.model.fields", [("id", "in", list(by_field))], ["model"],
        )
    except Exception as exc:  # noqa: BLE001
        census.gaps["ir.model.fields (ownership)"] = str(exc)
        return

    out: dict[str, set[str]] = {}
    for row in fields:
        module = by_field.get(row["id"])
        if module and row.get("model"):
            out.setdefault(module, set()).add(row["model"])
    census.module_models = {k: sorted(v) for k, v in sorted(out.items())}


def _modules(conn: Connection, census: Census) -> None:
    try:
        rows = conn.search_read(
            "ir.module.module",
            [("state", "in", ("installed", "to upgrade", "to remove"))],
            ["name", "latest_version", "state", "author", "shortdesc"],
            order="name",
        )
    except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
        census.gaps["ir.module.module"] = str(exc)
        return
    census.modules = [
        Module(
            name=r.get("name") or "",
            version=r.get("latest_version") or "",
            state=r.get("state") or "",
            author=r.get("author") or "",
            summary=r.get("shortdesc") or "",
        )
        for r in rows
    ]


def _config(conn: Connection, census: Census) -> None:
    try:
        rows = conn.search_read("ir.config_parameter", [], ["key", "value"])
    except Exception as exc:  # noqa: BLE001
        census.gaps["ir.config_parameter"] = str(exc)
        return
    census.config_params = {r.get("key") or "": r.get("value") or "" for r in rows}


def _views(conn: Connection, census: Census) -> None:
    """View inheritance with per-node module ownership.

    Capped at MAX_VIEWS patches: a large instance has tens of thousands of views
    and the conflict list only needs the inherited ones. The cap is reported in
    `gaps` when hit, because a silently truncated conflict list would read as
    "no conflicts".
    """
    MAX_VIEWS = 4000
    try:
        census.view_count = int(conn.call("ir.ui.view", "search_count", [[]]) or 0)
        newest = conn.search_read("ir.ui.view", [], ["write_date"],
                                  limit=1, order="write_date desc")
        census.view_max_write = (newest[0].get("write_date") or "") if newest else ""
        rows = conn.search_read(
            "ir.ui.view", [("inherit_id", "!=", False)],
            ["name", "model", "inherit_id"], limit=MAX_VIEWS, order="id",
        )
    except Exception as exc:  # noqa: BLE001
        census.gaps["ir.ui.view"] = str(exc)
        return
    if len(rows) >= MAX_VIEWS:
        census.gaps["ir.ui.view"] = (
            f"more than {MAX_VIEWS} inherited views; the conflict list is partial")

    owners = _owners(conn, "ir.ui.view", [r["id"] for r in rows])
    census.view_patches = [
        ViewPatch(
            view_id=r["id"],
            name=r.get("name") or "",
            model=r.get("model") or "",
            inherit_id=(r["inherit_id"][0] if isinstance(r.get("inherit_id"), list)
                        else r.get("inherit_id") or None),
            module=owners.get(r["id"], ""),
        )
        for r in rows
    ]


def _manual_fields(conn: Connection, census: Census) -> None:
    try:
        census.manual_fields = conn.search_read(
            "ir.model.fields", [("state", "=", "manual")],
            ["name", "model", "ttype", "store", "relation"], order="model,name",
        )
    except Exception as exc:  # noqa: BLE001
        census.gaps["ir.model.fields"] = str(exc)
    try:
        census.studio_records = int(conn.call(
            "ir.model.data", "search_count", [[("module", "=", STUDIO_MODULE)]]) or 0)
    except Exception as exc:  # noqa: BLE001
        census.gaps["ir.model.data"] = str(exc)


def _owners(conn: Connection, model: str, res_ids: list[int]) -> dict[int, str]:
    """res_id -> owning module, via ir.model.data.

    Chunked because a domain with ten thousand ids in it is a query some managed
    hosts refuse outright, and the failure looks like a timeout rather than a
    request that was too big.
    """
    CHUNK = 800
    out: dict[int, str] = {}
    for i in range(0, len(res_ids), CHUNK):
        batch = res_ids[i:i + CHUNK]
        try:
            rows = conn.search_read(
                "ir.model.data",
                [("model", "=", model), ("res_id", "in", batch)],
                ["module", "res_id"],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ownership lookup failed for %s: %s", model, exc)
            return out
        for r in rows:
            out[r["res_id"]] = r.get("module") or ""
    return out
