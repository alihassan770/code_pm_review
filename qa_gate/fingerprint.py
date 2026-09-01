"""The instance fingerprint (plan §8) and the drift feed derived from it.

The fingerprint exists so that a stale baseline declares itself rather than
lying. Section 8 raises the temporal baseline to a *critical* risk in revision 3
precisely because the database clone that used to mitigate it is gone — the only
thing standing between "staging changed underneath us" and a false regression is
a cheap hash taken at both ends.

It is computed from the census, which means watching for baseline validity and
watching for knowledge drift are the same operation (§9, "the census doubles as
a change feed"). That is not a coincidence to be tidied away; it is why the drift
report below lives in this module rather than in census.py.

`volumes` is specified in §8 as row counts for the blast-radius models with a
tolerance band. There is no blast radius until the impact engine lands in phase
D, so the key is present and empty rather than absent — a fingerprint that
changes shape between versions cannot be compared to an older one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db
from .census import OUR_PARAM_PREFIX, Census

SENTINEL_KEY = "hst_qa.instance_role"


@dataclass(frozen=True)
class Fingerprint:
    modules_sha: str
    config_sha: str
    view_count: int
    view_max_write: str
    sentinel: str
    taken_at: datetime
    # Module name -> "version|state". The inputs the modules hash covers, kept
    # so a drift report can name the module that appeared. A hash nobody can
    # explain sends someone diffing an instance by hand.
    modules: dict[str, str] = field(default_factory=dict)
    volumes: dict[str, int] = field(default_factory=dict)
    manual_field_count: int = 0

    def as_payload(self) -> dict:
        return {
            "modules": self.modules,
            "volumes": self.volumes,
            "manual_field_count": self.manual_field_count,
        }


def _sha(parts: list[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x00")
    return h.hexdigest()


def compute(census: Census) -> Fingerprint:
    modules = {m.name: f"{m.version}|{m.state}" for m in census.modules}
    # Our own parameters are excluded: the sentinel is recorded separately and
    # re-opting-in an instance should show up as a sentinel change, not as the
    # whole config having drifted.
    config = {k: v for k, v in census.config_params.items()
              if not k.startswith(OUR_PARAM_PREFIX)}
    return Fingerprint(
        modules_sha=_sha([f"{k}={v}" for k, v in sorted(modules.items())]),
        config_sha=_sha([f"{k}={v}" for k, v in sorted(config.items())]),
        view_count=census.view_count,
        view_max_write=census.view_max_write,
        sentinel=census.config_params.get(SENTINEL_KEY, ""),
        taken_at=census.taken_at or datetime.now(timezone.utc),
        modules=modules,
        manual_field_count=len(census.manual_fields),
    )


# ---- persistence -----------------------------------------------------------

def record(client_id: int, fp: Fingerprint, *, audit_id: int | None = None) -> int:
    row = db.query_one(
        """
        INSERT INTO instance_fingerprints
            (client_id, audit_id, modules_sha, config_sha, view_count,
             view_max_write, sentinel, payload, taken_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (client_id, audit_id, fp.modules_sha, fp.config_sha, fp.view_count,
         fp.view_max_write or None, fp.sentinel,
         json.dumps(fp.as_payload()), fp.taken_at),
    )
    return int(row["id"])


def latest(client_id: int, *, before_id: int | None = None) -> dict | None:
    """The most recent stored fingerprint, optionally the one before `before_id`.

    `before_id` is how the drift feed compares this run against the previous one
    without having to know when the previous one happened.
    """
    sql = "SELECT * FROM instance_fingerprints WHERE client_id = %s"
    params: list = [client_id]
    if before_id is not None:
        sql += " AND id < %s"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT 1"
    return db.query_one(sql, tuple(params))


# ---- the change feed -------------------------------------------------------

@dataclass(frozen=True)
class Drift:
    """One difference between two fingerprints, in the §9 feed's vocabulary."""
    kind: str      # module_added | module_removed | module_changed | config | views | sentinel
    subject: str
    detail: str

    @property
    def marker(self) -> str:
        return {"module_added": "+", "module_removed": "-"}.get(self.kind, "~")


def diff(previous: dict | None, current: Fingerprint) -> list[Drift]:
    """What changed since the previous stored fingerprint.

    Returns an empty list when there is no previous one — a first census has
    nothing to be surprised about, and inventing "everything is new" would bury
    the first real drift under three hundred lines of noise.
    """
    if not previous:
        return []
    old_modules: dict[str, str] = (previous.get("payload") or {}).get("modules") or {}
    out: list[Drift] = []

    for name in sorted(set(current.modules) - set(old_modules)):
        out.append(Drift("module_added", name,
                         f"installed ({current.modules[name].split('|')[0]})"))
    for name in sorted(set(old_modules) - set(current.modules)):
        out.append(Drift("module_removed", name, "no longer installed"))
    for name in sorted(set(old_modules) & set(current.modules)):
        if old_modules[name] != current.modules[name]:
            out.append(Drift("module_changed", name,
                             f"{old_modules[name]} → {current.modules[name]}"))

    if previous.get("config_sha") != current.config_sha:
        out.append(Drift("config", "ir.config_parameter",
                         "one or more system parameters changed"))
    if previous.get("sentinel") != current.sentinel:
        out.append(Drift("sentinel", SENTINEL_KEY,
                         f"{previous.get('sentinel') or '(unset)'} → "
                         f"{current.sentinel or '(unset)'}"))
    if int(previous.get("view_count") or 0) != current.view_count:
        out.append(Drift("views", "ir.ui.view",
                         f"{previous.get('view_count')} → {current.view_count} views"))
    return out
