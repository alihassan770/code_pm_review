"""Indexing `qa/scenarios/**/*.yml` — the §12 scenario format.

Phase C reads scenarios; it does not run them. What the coverage map needs from
a scenario file is which models it exercises and how many of them exist per
module, and validating the header while we are already parsing the YAML is free.

The one rule enforced here rather than left to the executor is that **`tier` is
required and has no default**. §12 calls that the schema-level expression of the
whole revision: an author has to state what a scenario may do to a client's
instance, so there is no accidental path to committing on staging. A tier that
defaults to anything is a tier nobody had to think about.

Steps are deliberately *not* validated. The step grammar belongs to the runner
(phase F), and a control plane that half-validates it would reject valid
scenarios the moment the grammar grows — which teaches people that the index is
wrong rather than that their file is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

PREFIX = "qa/scenarios/"
VALID_TIERS = (1, 2, 3)
# Revision 3 deleted tier 4 along with the database clone. Named explicitly so
# a scenario carried over from an older repo gets an explanation rather than a
# range error.
REMOVED_TIER = 4


@dataclass(frozen=True)
class Scenario:
    path: str
    id: str = ""
    title: str = ""
    tier: int | None = None
    tags: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    personas: list[str] = field(default_factory=list)
    drift: str = ""
    covers: list[str] = field(default_factory=list)
    extends: str = ""
    models: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def ratified(self) -> bool:
        """Scenarios the AI proposed and a human accepted carry this tag (§2).
        Everything else is a proposal until somebody says otherwise."""
        return "ratified" in self.tags

    def as_payload(self) -> dict:
        return {
            "path": self.path, "id": self.id, "title": self.title,
            "tier": self.tier, "tags": self.tags, "versions": self.versions,
            "personas": self.personas, "drift": self.drift, "covers": self.covers,
            "extends": self.extends, "models": self.models, "errors": self.errors,
        }

    @classmethod
    def from_payload(cls, raw: dict) -> "Scenario":
        """Rehydrate from the cache, ignoring keys this version does not know.

        Splatting the payload straight in would make an older cached row a
        TypeError the day a field is added or removed, which is a migration
        nobody should have to write for a cache.
        """
        return cls(
            path=raw.get("path", ""), id=raw.get("id", ""), title=raw.get("title", ""),
            tier=raw.get("tier"), tags=list(raw.get("tags") or []),
            versions=list(raw.get("versions") or []),
            personas=list(raw.get("personas") or []),
            drift=raw.get("drift", ""), covers=list(raw.get("covers") or []),
            extends=raw.get("extends", ""), models=list(raw.get("models") or []),
            errors=list(raw.get("errors") or []),
        )


def parse(path: str, text: str) -> Scenario:
    errors: list[str] = []
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return Scenario(path=path, errors=[f"not valid YAML: {exc}"])
    if not isinstance(raw, dict):
        return Scenario(path=path, errors=["must be a mapping at the top level."])

    scenario_id = str(raw.get("id") or "").strip()
    if not scenario_id:
        errors.append("no id. The id is how a run report refers to this scenario.")

    tier = raw.get("tier")
    if tier is None:
        errors.append(
            "no tier. It is required and has no default: a scenario has to state "
            "whether it may write to the client's instance.")
    elif tier == REMOVED_TIER:
        errors.append(
            "tier 4 no longer exists. Revision 3 removed the database-clone path, "
            "because managed hosts will not let us create a database. Re-scope it "
            "to tier 2 or 3.")
    elif tier not in VALID_TIERS:
        errors.append(f"tier {tier!r} is not one of 1, 2 or 3.")

    drift = str(raw.get("drift") or "")
    if drift and drift not in ("immune", "data_dependent"):
        errors.append(f"drift {drift!r} must be 'immune' or 'data_dependent'.")

    return Scenario(
        path=path,
        id=scenario_id,
        title=str(raw.get("title") or ""),
        tier=tier if isinstance(tier, int) else None,
        tags=_strings(raw.get("tags")),
        versions=_strings(raw.get("versions")),
        personas=_strings(raw.get("personas")),
        drift=drift,
        covers=_strings(raw.get("covers")),
        extends=str(raw.get("extends") or ""),
        models=models_in(raw),
        errors=errors,
    )


def index(files: list[tuple[str, str]]) -> list[Scenario]:
    """(path, text) pairs -> scenarios, in path order."""
    return [parse(path, text) for path, text in files]


def models_in(node, found: set[str] | None = None) -> list[str]:
    """Every `model:` value anywhere in the file.

    A walk rather than a schema read, because a model name appears in fixtures,
    in `create` steps, and in whatever verbs the grammar grows next. Over-reading
    here is safe: the worst case is attributing a scenario to one more module
    than it strictly exercises, which errs toward saying a module is covered by
    something rather than pretending it is not.
    """
    if found is None:
        found = set()
    if isinstance(node, dict):
        value = node.get("model")
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
        for v in node.values():
            models_in(v, found)
    elif isinstance(node, list):
        for v in node:
            models_in(v, found)
    return sorted(found)


def _strings(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
