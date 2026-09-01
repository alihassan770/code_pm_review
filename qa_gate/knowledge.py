"""Layer 3, the curated overlay: `qa/knowledge.yml` in the client's own repo.

§9 opens by naming the failure mode this layer has to avoid: *"The mistake to
avoid is treating this as a document somebody maintains."* Three of the four
knowledge layers cannot go stale because they are re-derived or append-only.
This one depends on a person writing something, so it is the smallest layer and
the only one with a decay mechanism attached.

**It lives in git, not in the database.** That is a house rule and it is load
bearing: it changes through the same pull-request review the code does, by the
people who just learned the thing being written down. What Postgres holds is a
parsed read-model cache keyed by commit sha — a copy that knows which commit it
came from and can therefore be thrown away, not a second source of truth.

Four mechanisms keep it current, none of which is "remember to update the wiki":

  * **Decay.** Every entry carries `review_after`. Past that date it renders as
    stale. A stale entry still applies — it just announces that nobody has
    vouched for it lately. Suppressing it would be worse: it would silently
    remove a constraint that is probably still true.
  * **Confirm at the moment of relevance.** When a run's blast radius intersects
    an entry's scope, the bundle asks whoever reviews the run to confirm it.
    Needs the impact engine, so it arrives with phase D.
  * **Contradiction detection.** An entry naming a model, module, or report the
    census says no longer exists is flagged. This catches the commonest form of
    rot: knowledge about something that was renamed or uninstalled. Implemented
    here, because the census is already available.
  * **Pull request review.** Free, by construction, because of where it lives.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import yaml

log = logging.getLogger(__name__)

PATH = "qa/knowledge.yml"

INVARIANT = "invariant"
DANGER_ZONE = "danger_zone"


@dataclass(frozen=True)
class Entry:
    """One invariant or danger zone.

    `scope` is kept as a plain dict of lists — models, modules, reports — rather
    than being flattened, because contradiction detection has to know *what kind*
    of thing is missing to say anything useful about it.
    """
    id: str
    kind: str
    text: str
    scope: dict[str, list[str]] = field(default_factory=dict)
    added_by: str = ""
    last_confirmed: date | None = None
    review_after: date | None = None

    def is_stale(self, today: date | None = None) -> bool:
        if not self.review_after:
            return False
        return self.review_after < (today or date.today())

    @property
    def scope_summary(self) -> str:
        return ", ".join(f"{k}: {', '.join(v)}" for k, v in self.scope.items() if v)

    def scoped_names(self) -> list[tuple[str, str]]:
        return [(kind, name) for kind, names in self.scope.items() for name in names]


@dataclass
class Knowledge:
    """A parsed `qa/knowledge.yml`, plus whatever was wrong with it.

    Parse errors are collected rather than raised. A knowledge file with one
    malformed entry should contribute its other nine — refusing the whole file
    would mean a typo silently removes every danger zone the client has, which
    is the opposite of what a danger zone is for.
    """
    client: str = ""
    invariants: list[Entry] = field(default_factory=list)
    danger_zones: list[Entry] = field(default_factory=list)
    expected_values: dict = field(default_factory=dict)
    unused_apps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    present: bool = False

    @property
    def entries(self) -> list[Entry]:
        return self.invariants + self.danger_zones

    def stale(self, today: date | None = None) -> list[Entry]:
        return [e for e in self.entries if e.is_stale(today)]

    def as_payload(self) -> dict:
        """The cache representation. Dates become ISO strings; jsonb has no date."""
        return {
            "client": self.client,
            "present": self.present,
            "expected_values": self.expected_values,
            "unused_apps": self.unused_apps,
            "errors": self.errors,
            "entries": [
                {
                    "id": e.id, "kind": e.kind, "text": e.text, "scope": e.scope,
                    "added_by": e.added_by,
                    "last_confirmed": e.last_confirmed.isoformat() if e.last_confirmed else None,
                    "review_after": e.review_after.isoformat() if e.review_after else None,
                }
                for e in self.entries
            ],
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "Knowledge":
        k = cls(
            client=payload.get("client", ""),
            expected_values=payload.get("expected_values") or {},
            unused_apps=payload.get("unused_apps") or [],
            errors=list(payload.get("errors") or []),
            present=bool(payload.get("present")),
        )
        for raw in payload.get("entries") or []:
            entry = Entry(
                id=raw.get("id", ""), kind=raw.get("kind", INVARIANT),
                text=raw.get("text", ""), scope=raw.get("scope") or {},
                added_by=raw.get("added_by", ""),
                last_confirmed=_as_date(raw.get("last_confirmed")),
                review_after=_as_date(raw.get("review_after")),
            )
            (k.danger_zones if entry.kind == DANGER_ZONE else k.invariants).append(entry)
        return k


def parse(text: str) -> Knowledge:
    k = Knowledge(present=True)
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        k.errors.append(f"{PATH} is not valid YAML: {exc}")
        return k
    if not isinstance(raw, dict):
        k.errors.append(f"{PATH} must be a mapping at the top level.")
        return k

    k.client = str(raw.get("client") or "")
    k.invariants = _entries(raw.get("invariants"), INVARIANT, k.errors)
    k.danger_zones = _entries(raw.get("danger_zones"), DANGER_ZONE, k.errors)

    expected = raw.get("expected_values")
    if expected is not None and not isinstance(expected, dict):
        k.errors.append("expected_values must be a mapping of name to value.")
    else:
        k.expected_values = expected or {}

    unused = raw.get("unused_apps")
    if unused is not None and not isinstance(unused, list):
        k.errors.append("unused_apps must be a list of module names.")
    else:
        k.unused_apps = [str(u) for u in (unused or [])]

    seen: set[str] = set()
    for entry in k.entries:
        if entry.id in seen:
            k.errors.append(f"Duplicate entry id {entry.id!r}.")
        seen.add(entry.id)
    return k


def _entries(raw, kind: str, errors: list[str]) -> list[Entry]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{kind}s must be a list.")
        return []
    out: list[Entry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"{kind}[{i}] must be a mapping.")
            continue
        entry_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not entry_id:
            errors.append(f"{kind}[{i}] has no id.")
            continue
        if not text:
            errors.append(f"{entry_id} has no text, so it says nothing to anybody.")
            continue
        scope_raw = item.get("scope") or {}
        scope: dict[str, list[str]] = {}
        if isinstance(scope_raw, dict):
            for key, names in scope_raw.items():
                if isinstance(names, str):
                    names = [names]
                if isinstance(names, list):
                    scope[str(key)] = [str(n) for n in names]
        else:
            errors.append(f"{entry_id}: scope must be a mapping such as "
                          "{models: [...], modules: [...]}.")
        out.append(Entry(
            id=entry_id, kind=kind, text=text, scope=scope,
            added_by=str(item.get("added_by") or ""),
            last_confirmed=_as_date(item.get("last_confirmed")),
            review_after=_as_date(item.get("review_after")),
        ))
    return out


def _as_date(value) -> date | None:
    """YAML gives a `date` for an unquoted 2026-08-12 and a `str` for a quoted
    one. Both are things people write, so both parse."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


# ---- contradiction detection -----------------------------------------------

@dataclass(frozen=True)
class Contradiction:
    entry_id: str
    kind: str          # models | modules | reports | unused_apps
    name: str
    message: str


def contradictions(k: Knowledge, census) -> list[Contradiction]:
    """Entries naming something the instance says no longer exists.

    Only modules and `unused_apps` are checked against the census today. Models
    are deliberately not: the census reads field ownership for non-core modules
    only, so a model it has not seen is far more likely to be one nobody
    customised than one that was deleted — and a contradiction report that cries
    wolf gets ignored, which costs more than the check is worth. Reports need
    `ir.actions.report`, which the census does not read yet.
    """
    if census is None:
        return []
    installed = census.module_names
    out: list[Contradiction] = []
    for entry in k.entries:
        for kind, name in entry.scoped_names():
            if kind != "modules":
                continue
            if name not in installed:
                out.append(Contradiction(
                    entry.id, "modules", name,
                    f"{entry.id} is scoped to module {name!r}, which is not "
                    "installed on this instance. It was probably renamed or "
                    "uninstalled, and the entry no longer applies to anything."))
    for name in k.unused_apps:
        if name not in installed:
            out.append(Contradiction(
                "unused_apps", "unused_apps", name,
                f"unused_apps lists {name!r}, which is not installed anyway. "
                "Harmless, but it suggests the list has not been read lately."))
    return out
