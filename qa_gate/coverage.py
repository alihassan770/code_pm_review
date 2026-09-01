"""The coverage map — §9, "what the humans get".

One page per client, and the plan is explicit that its most useful output is not
context for a language model. It is the answer to *"which of this client's
modules is most exposed right now"*.

The row that earns the page is a module we wrote, changed recently, with zero
scenarios covering it. That is where the next client-reported regression comes
from, and no amount of prose in a wiki surfaces it as clearly as a sorted column.
The bottom rows — vendor modules, view-only themes, `studio_customization` — are
what the instance census adds that a repo-only knowledge base cannot see at all.

Everything here is a join between two things that already exist: the census
(what is installed, which models each module touches, who patches which view)
and the client repo (which modules we hold source for, and what scenarios there
are). Nothing is stored that is not derivable from those two.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .census import STUDIO_MODULE, Census
from .knowledge import Knowledge
from .scenarios import Scenario

# Source of a module, in the order the coverage table groups by.
OURS, VENDOR, NO_SOURCE, CORE = "ours", "vendor", "none", "core"

# Exposure, worst first. The order is the sort order of the page.
EXPOSED = "exposed"          # ours, no scenario covers it
NO_SOURCE_RISK = "no source"  # exists on the instance, in no repository
VIEW_RISK = "view risk"      # patches views, no scenarios, we hold no source
NEEDS_STUB = "stub only"     # a connector; §3's stub convention lands in phase E
COVERED = "covered"
UNUSED = "not used"          # the client says so in qa/knowledge.yml

EXPOSURE_ORDER = [EXPOSED, NO_SOURCE_RISK, VIEW_RISK, NEEDS_STUB, COVERED, UNUSED]

_CONNECTOR_HINTS = ("connector", "quickbooks", "xero", "stripe", "paypal",
                    "shopify", "amazon", "twilio", "sendgrid", "sync", "api")


@dataclass
class ModuleRow:
    name: str
    source: str
    models: list[str] = field(default_factory=list)
    view_patches: int = 0
    scenarios: list[Scenario] = field(default_factory=list)
    last_changed: datetime | None = None
    last_changed_by: str = ""
    exposure: str = COVERED
    version: str = ""
    author: str = ""
    note: str = ""

    @property
    def scenario_count(self) -> int:
        return len(self.scenarios)

    @property
    def sort_key(self) -> tuple:
        rank = EXPOSURE_ORDER.index(self.exposure) if self.exposure in EXPOSURE_ORDER else 99
        # Within an exposure band, most recently changed first: a module nobody
        # has touched in a year is exposed in theory, and one changed on Tuesday
        # is exposed in practice.
        recency = -(self.last_changed.timestamp() if self.last_changed else 0)
        return (rank, recency, self.name)


@dataclass
class Coverage:
    rows: list[ModuleRow] = field(default_factory=list)
    core_module_count: int = 0
    scenario_count: int = 0
    unattributed: list[Scenario] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    @property
    def exposed(self) -> list[ModuleRow]:
        return [r for r in self.rows if r.exposure == EXPOSED]

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in EXPOSURE_ORDER}
        for r in self.rows:
            out[r.exposure] = out.get(r.exposure, 0) + 1
        return out


def build(census: Census, *, repo_modules: dict[str, str] | None = None,
          scenario_list: list[Scenario] | None = None,
          knowledge: Knowledge | None = None,
          last_changed: dict[str, tuple[datetime | None, str]] | None = None) -> Coverage:
    """Join the census with the repo. Neither argument is required.

    A client with no GitHub repo configured still gets a coverage map — every
    module reads as `no source`, which is a true and useful statement about that
    client rather than an empty page.
    """
    repo_modules = repo_modules or {}
    scenario_list = scenario_list or []
    last_changed = last_changed or {}
    unused = set(knowledge.unused_apps if knowledge else [])

    patches_by_module: dict[str, int] = {}
    for patch in census.view_patches:
        if patch.module:
            patches_by_module[patch.module] = patches_by_module.get(patch.module, 0) + 1

    coverage = Coverage(scenario_count=len(scenario_list),
                        conflicts=census.conflicts())
    attributed: set[str] = set()

    for module in census.modules:
        if module.is_odoo and module.name not in repo_modules:
            coverage.core_module_count += 1
            continue

        models = census.module_models.get(module.name, [])
        matching = [s for s in scenario_list if set(s.models) & set(models)]
        for s in matching:
            attributed.add(s.path)

        row = ModuleRow(
            name=module.name,
            source=_source_of(module.name, repo_modules),
            models=models,
            view_patches=patches_by_module.get(module.name, 0),
            scenarios=matching,
            version=module.version,
            author=module.author,
        )
        row.last_changed, row.last_changed_by = last_changed.get(module.name, (None, ""))
        row.exposure, row.note = _exposure_of(row, unused)
        coverage.rows.append(row)

    # Studio lives on the instance and in no repository. It is a row even though
    # it is not really a module, because §9's point is that repo-based analysis
    # cannot see it at all.
    if census.manual_fields:
        studio_models = sorted({f["model"] for f in census.manual_fields if f.get("model")})
        if not any(r.name == STUDIO_MODULE for r in coverage.rows):
            coverage.rows.append(ModuleRow(
                name=STUDIO_MODULE, source=NO_SOURCE, models=studio_models,
                exposure=NO_SOURCE_RISK,
                note=f"{len(census.manual_fields)} manual field(s) declared by no module.",
            ))

    coverage.rows.sort(key=lambda r: r.sort_key)
    # A scenario matching no installed module is worth surfacing: it usually
    # means the module it tests was uninstalled, and the scenario has been
    # quietly passing against nothing.
    coverage.unattributed = [s for s in scenario_list if s.path not in attributed]
    return coverage


def _source_of(name: str, repo_modules: dict[str, str]) -> str:
    if name in repo_modules:
        return OURS
    if name == STUDIO_MODULE:
        return NO_SOURCE
    return VENDOR


def _exposure_of(row: ModuleRow, unused: set[str]) -> tuple[str, str]:
    if row.name in unused:
        return UNUSED, "Listed in qa/knowledge.yml unused_apps, so its flows are never selected."
    if row.scenarios:
        return COVERED, ""
    if row.source == NO_SOURCE:
        return NO_SOURCE_RISK, "Exists on the instance and in no repository."
    if row.source == OURS:
        return EXPOSED, ("We wrote this and nothing tests it. This is where the next "
                         "client-reported regression comes from.")
    if any(hint in row.name.lower() for hint in _CONNECTOR_HINTS):
        return NEEDS_STUB, ("Looks like an outbound connector. It needs a stub descriptor "
                            "before any scenario touches it (§3; the registry lands in phase E).")
    if row.view_patches and not row.models:
        return VIEW_RISK, (f"Patches {row.view_patches} view(s) and adds no fields. "
                           "Breakage here is visual and no assertion will catch it.")
    return VIEW_RISK if row.view_patches else NO_SOURCE_RISK, (
        "Third-party code we hold no source for. Until phase D can read it, its flows "
        "are selected rather than reasoned about.")
