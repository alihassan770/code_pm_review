"""Fetching, parsing, and caching one client's repository.

The orchestration layer over `github.py`: one commit sha, one tree, then the
handful of blobs that matter. Everything downstream — the knowledge page, the
coverage map, and later the impact engine — reads the cached parse rather than
GitHub, so a page load is never a rate-limited network call.

The cache is refreshed explicitly, never on a timer. That is the prior art's
rule about never auto-pulling, applied to a read: a background poller that
silently updated the knowledge base would mean the constraint a run was checked
against is not the one anybody looked at. `stale` below says when the branch has
moved on and lets a human decide.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db, github, knowledge as knowledge_mod, scenarios as scenarios_mod
from .clients import Client
from .github import GitHub, GitHubError, NotFound
from .knowledge import Knowledge
from .scenarios import Scenario

log = logging.getLogger(__name__)

# The coverage map's "last changed" column costs one API request per module we
# hold source for. Capped because a repo with sixty modules would otherwise turn
# one refresh into sixty round trips; the modules are sorted so the cap is
# deterministic rather than "whichever ones came back first".
MAX_LAST_CHANGED = 40


class NoRepository(Exception):
    """The client has no GitHub `owner/name` configured. Not a failure to fetch."""


@dataclass
class Snapshot:
    """One client's repo as of one commit."""
    github: str = ""
    ref: str = ""
    commit_sha: str = ""
    knowledge: Knowledge = field(default_factory=Knowledge)
    scenarios: list[Scenario] = field(default_factory=list)
    modules: dict[str, str] = field(default_factory=dict)
    last_changed: dict[str, tuple[datetime | None, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    fetched_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return bool(self.commit_sha) and not self.error

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:8]

    @property
    def scenario_errors(self) -> list[Scenario]:
        return [s for s in self.scenarios if s.errors]


def fetch(client: Client, *, token: str = "", api_root: str = github.API_ROOT,
          with_history: bool = True) -> Snapshot:
    """Read the client's repo at the head of its base branch.

    Failures are returned on the snapshot rather than raised, for the same
    reason the audit does it: the pages that call this render a client, and one
    unreachable repo must not be a 500.
    """
    if not client.github:
        raise NoRepository(
            f"{client.slug} has no GitHub owner/name set. Add it on the client "
            "page — it is the join key for everything in this phase.")

    ref = client.base_branch or "main"
    snap = Snapshot(github=client.github, ref=ref,
                    fetched_at=datetime.now(timezone.utc))
    gh = GitHub(token, api_root=api_root)

    try:
        snap.commit_sha = gh.head_sha(client.github, ref)
        entries = gh.tree(client.github, snap.commit_sha)
    except NotFound as exc:
        snap.error = (f"{exc} Looked for {client.github}@{ref}; check the "
                      "owner/name and the base branch on the client page.")
        return snap
    except GitHubError as exc:
        snap.error = str(exc)
        return snap

    snap.modules = github.module_dirs(entries)
    knowledge_entry, scenario_entries = github.qa_files(entries)

    if knowledge_entry is None:
        snap.warnings.append(
            f"No {knowledge_mod.PATH} in this repo. The curated overlay is the only "
            "knowledge layer a human has to write, and without it the gate has no "
            "business invariants, danger zones, or unused-app list for this client.")
    else:
        try:
            snap.knowledge = knowledge_mod.parse(
                gh.blob_text(client.github, knowledge_entry.sha))
        except GitHubError as exc:
            snap.warnings.append(f"Could not read {knowledge_mod.PATH}: {exc}")

    if len(scenario_entries) > github.MAX_SCENARIO_FILES:
        snap.warnings.append(
            f"{len(scenario_entries)} scenario files found; only the first "
            f"{github.MAX_SCENARIO_FILES} were indexed. Coverage below is partial.")
        scenario_entries = scenario_entries[: github.MAX_SCENARIO_FILES]

    files: list[tuple[str, str]] = []
    for entry in scenario_entries:
        try:
            files.append((entry.path, gh.blob_text(client.github, entry.sha)))
        except GitHubError as exc:
            snap.warnings.append(f"Could not read {entry.path}: {exc}")
    snap.scenarios = scenarios_mod.index(files)

    if with_history and snap.modules:
        snap.last_changed = _history(gh, client.github, ref, snap.modules, snap.warnings)
    return snap


def _history(gh: GitHub, repo: str, ref: str, modules: dict[str, str],
             warnings: list[str]) -> dict[str, tuple[datetime | None, str]]:
    names = sorted(modules)
    if len(names) > MAX_LAST_CHANGED:
        warnings.append(
            f"{len(names)} modules in the repo; 'last changed' was looked up for the "
            f"first {MAX_LAST_CHANGED} alphabetically. The rest read as unknown.")
        names = names[:MAX_LAST_CHANGED]
    out: dict[str, tuple[datetime | None, str]] = {}
    for name in names:
        try:
            commit = gh.last_commit(repo, ref, modules[name])
        except GitHubError as exc:
            log.info("%s: no history for %s (%s)", repo, name, exc)
            continue
        if commit:
            out[name] = (commit.date, commit.author)
    return out


# ---- the cache -------------------------------------------------------------

def save(client_id: int, snap: Snapshot, *, fetched_by: int | None = None) -> None:
    import json

    db.execute(
        """
        INSERT INTO client_repo_cache
            (client_id, github, ref, commit_sha, knowledge, scenarios, modules,
             last_changed, warnings, error, fetched_at, fetched_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT (client_id) DO UPDATE SET
            github = EXCLUDED.github, ref = EXCLUDED.ref,
            commit_sha = EXCLUDED.commit_sha, knowledge = EXCLUDED.knowledge,
            scenarios = EXCLUDED.scenarios, modules = EXCLUDED.modules,
            last_changed = EXCLUDED.last_changed, warnings = EXCLUDED.warnings,
            error = EXCLUDED.error, fetched_at = now(),
            fetched_by = EXCLUDED.fetched_by
        """,
        (client_id, snap.github, snap.ref, snap.commit_sha,
         json.dumps(snap.knowledge.as_payload()),
         json.dumps([s.as_payload() for s in snap.scenarios]),
         json.dumps(snap.modules),
         json.dumps({k: {"date": d.isoformat() if d else None, "author": a}
                     for k, (d, a) in snap.last_changed.items()}),
         json.dumps(snap.warnings), snap.error, fetched_by),
    )


def load(client_id: int) -> Snapshot | None:
    row = db.query_one("SELECT * FROM client_repo_cache WHERE client_id = %s",
                       (client_id,))
    if not row:
        return None
    snap = Snapshot(
        github=row["github"], ref=row["ref"], commit_sha=row["commit_sha"],
        knowledge=Knowledge.from_payload(row["knowledge"] or {}),
        scenarios=[Scenario.from_payload(s) for s in (row["scenarios"] or [])],
        modules=dict(row["modules"] or {}),
        warnings=list(row["warnings"] or []),
        error=row["error"] or "",
        fetched_at=row["fetched_at"],
    )
    for name, info in (row["last_changed"] or {}).items():
        raw = (info or {}).get("date")
        snap.last_changed[name] = (
            datetime.fromisoformat(raw) if raw else None, (info or {}).get("author", ""))
    return snap


def sync(client: Client, *, token: str = "", api_root: str = github.API_ROOT,
         fetched_by: int | None = None) -> Snapshot:
    snap = fetch(client, token=token, api_root=api_root)
    save(client.id, snap, fetched_by=fetched_by)
    return snap


def stale(client: Client, snap: Snapshot | None, *, token: str = "",
          api_root: str = github.API_ROOT) -> str | None:
    """Whether the branch has moved since the cache was built.

    One cheap request, and it reports rather than repairs — the same choice the
    prior art made about never auto-pulling. A knowledge base that updated itself
    between somebody reading it and a run using it would make the two disagree
    with no way to tell.
    """
    if not snap or not snap.commit_sha or not client.github:
        return None
    try:
        head = GitHub(token, api_root=api_root).head_sha(
            client.github, client.base_branch or "main")
    except GitHubError:
        return None
    if head != snap.commit_sha:
        return (f"{client.base_branch or 'main'} has moved to {head[:8]} since this "
                f"was read at {snap.short_sha}. Refresh to pick it up.")
    return None
