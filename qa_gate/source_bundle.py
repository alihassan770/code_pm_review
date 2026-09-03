"""Every line of one client's addons, ready to put in a prompt.

This is the agent's knowledge base. Not a summary of the code — the code.

## Why the source and not a digest

A generated description of a module is a lossy copy of something we already
have. It costs an API call to produce, it goes stale the moment anyone commits,
and it becomes a second source of truth that wins in the prompt and loses in
reality. The only reason to accept all that is if the source does not fit in the
context window, and at these sizes it is not close: a real client repo measured
~17,000 tokens against a 1,000,000-token window.

So the agent gets the source, and questions about line 40 of a method have a
correct answer instead of a plausible one.

## Why the whole repo in one request

GitHub will hand over a whole commit as a tarball from a single endpoint. The
alternative — walk the tree, fetch each blob — is one request per file, which
for a forty-module repo is hundreds of API calls against a 5,000/hour budget
just to answer one question. The tarball is one request and about two seconds.

## Per client, always

`load` and `context_for` take a `client_id` and there is no query here that
spans two clients. A review that could see another client's source would be a
confidentiality failure, so the separation is in the key rather than in the
caller's discipline.

## Stable ordering is load-bearing

DeepSeek caches prompt prefixes automatically and matches them exactly. Files are
sorted by path on the way in and emitted in that order, so the prefix is
byte-identical between calls and repeat reads bill at roughly a thirtieth of the
first. Shuffle the order and every call is a cache miss.
"""
from __future__ import annotations

import io
import json
import logging
import tarfile
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from . import db, github
from .clients import Client

log = logging.getLogger(__name__)

#: What counts as source. Behaviour and configuration, not assets: a PNG or a
#: minified bundle would spend context without telling the agent anything.
SOURCE_SUFFIXES = (".py", ".xml", ".csv", ".yml", ".yaml", ".js", ".xsl", ".sql")

#: Directories that are never the client's own work.
SKIP_DIRS = ("/node_modules/", "/.git/", "/static/lib/", "/__pycache__/",
             "/.venv/", "/venv/", "/dist/", "/build/", "/.idea/")

#: One file. Above this it is generated, vendored, or data — none of which a
#: question about behaviour is improved by, and all of which are expensive.
MAX_FILE_BYTES = 400_000

#: The whole bundle. Roughly 700k tokens at 3.6 chars/token, which leaves room
#: in a 1M window for the system prompt, the task, and the answer. A repo that
#: exceeds this is reported as truncated rather than quietly clipped.
MAX_TOTAL_BYTES = 2_500_000

#: Rough, and only used for display and for the fits-in-context check. Code
#: tokenises denser than prose; 3.6 is measured against this project's own repo.
CHARS_PER_TOKEN = 3.6

TARBALL_TIMEOUT = httpx.Timeout(180.0, connect=15.0)


class BundleError(Exception):
    """Could not build a bundle. Message is safe to show."""


@dataclass
class Bundle:
    client_id: int = 0
    commit_sha: str = ""
    ref: str = ""
    #: [(path, text)], sorted by path. Order is part of the contract.
    files: list[tuple[str, str]] = field(default_factory=list)
    byte_count: int = 0
    est_tokens: int = 0
    truncated: bool = False
    #: Paths left out, with why. Never silent.
    skipped: list[str] = field(default_factory=list)
    error: str = ""
    built_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.error

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:8]

    def as_prompt(self) -> str:
        """The block to paste into a request, in stable order."""
        head = (
            f"# Source of every addon for this client\n"
            f"# commit {self.commit_sha} on {self.ref}\n"
            f"# {self.file_count} files, {self.byte_count:,} bytes\n"
        )
        if self.truncated:
            head += ("# WARNING: truncated. Some files are NOT included; say so "
                     "rather than guessing about code you were not shown.\n")
        parts = [head]
        for path, text in self.files:
            parts.append(f"\n===== {path} =====\n{text}\n")
        return "".join(parts)


def _keep(path: str, size: int) -> tuple[bool, str]:
    lowered = "/" + path.lower()
    if any(d in lowered for d in SKIP_DIRS):
        return False, "vendored or generated directory"
    if not path.endswith(SOURCE_SUFFIXES):
        return False, "not a source file"
    if size > MAX_FILE_BYTES:
        return False, f"{size:,} bytes, over the {MAX_FILE_BYTES:,} per-file limit"
    return True, ""


def build(client: Client, *, commit_sha: str, ref: str = "", token: str = "",
          api_root: str = github.API_ROOT) -> Bundle:
    """Download the commit as a tarball and keep every source file in it."""
    bundle = Bundle(client_id=client.id, commit_sha=commit_sha,
                    ref=ref or client.base_branch or "main")
    if not client.github:
        bundle.error = f"{client.slug} has no GitHub owner/name set."
        return bundle

    url = f"{api_root.rstrip('/')}/repos/{client.github}/tarball/{commit_sha}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(url, headers=headers, follow_redirects=True,
                         timeout=TARBALL_TIMEOUT)
    except httpx.HTTPError as exc:
        bundle.error = f"Could not download {client.github}@{commit_sha[:8]}: {exc}"
        return bundle
    if resp.status_code == 404:
        bundle.error = (
            f"GitHub returned 404 for {client.github}@{commit_sha[:8]}. For a "
            "private repository that is also what an unauthorised token gets, so "
            "check the token under Settings before assuming the commit is gone.")
        return bundle
    if resp.status_code != 200:
        bundle.error = f"GitHub returned {resp.status_code} for the tarball."
        return bundle

    try:
        archive = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz")
    except tarfile.TarError as exc:
        bundle.error = f"Could not read the archive GitHub sent: {exc}"
        return bundle

    # GitHub wraps everything in one top directory named owner-repo-sha. Strip it
    # so paths read as they do in the repo and stay stable across commits — with
    # the prefix left on, every path contains the sha and no prompt prefix could
    # ever be reused between two commits.
    members = [m for m in archive.getmembers() if m.isfile()]
    prefix = ""
    if members:
        first = members[0].name.split("/", 1)
        if len(first) == 2:
            prefix = first[0] + "/"

    kept: list[tuple[str, str]] = []
    total = 0
    for member in members:
        path = member.name[len(prefix):] if prefix and member.name.startswith(prefix) \
            else member.name
        if not path:
            continue
        keep, why = _keep(path, member.size)
        if not keep:
            if why != "not a source file":
                bundle.skipped.append(f"{path} — {why}")
            continue
        if total + member.size > MAX_TOTAL_BYTES:
            bundle.truncated = True
            bundle.skipped.append(f"{path} — bundle size limit reached")
            continue
        handle = archive.extractfile(member)
        if handle is None:
            continue
        # A file somebody's editor wrote as latin-1 should degrade to a mangled
        # character, not take down the whole bundle.
        kept.append((path, handle.read().decode("utf-8", errors="replace")))
        total += member.size

    kept.sort(key=lambda pair: pair[0])
    bundle.files = kept
    bundle.byte_count = total
    bundle.est_tokens = int(total / CHARS_PER_TOKEN)
    if not kept:
        bundle.error = ("No source files in this commit. If the repository is not "
                        "empty, the base branch may be pointing somewhere unexpected.")
    return bundle


# ---- the cache -------------------------------------------------------------

def save(bundle: Bundle) -> None:
    db.execute(
        """
        INSERT INTO client_source_bundle
            (client_id, commit_sha, ref, files, file_count, byte_count,
             est_tokens, truncated, skipped, error, built_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (client_id, commit_sha) DO UPDATE SET
            ref = EXCLUDED.ref, files = EXCLUDED.files,
            file_count = EXCLUDED.file_count, byte_count = EXCLUDED.byte_count,
            est_tokens = EXCLUDED.est_tokens, truncated = EXCLUDED.truncated,
            skipped = EXCLUDED.skipped, error = EXCLUDED.error, built_at = now()
        """,
        (bundle.client_id, bundle.commit_sha, bundle.ref,
         json.dumps([{"path": p, "text": t} for p, t in bundle.files]),
         bundle.file_count, bundle.byte_count, bundle.est_tokens,
         bundle.truncated, json.dumps(bundle.skipped), bundle.error),
    )


def _from_row(row: dict) -> Bundle:
    bundle = Bundle(
        client_id=row["client_id"], commit_sha=row["commit_sha"], ref=row["ref"],
        files=[(f["path"], f["text"]) for f in (row["files"] or [])],
        byte_count=row["byte_count"], est_tokens=row["est_tokens"],
        truncated=row["truncated"], skipped=list(row["skipped"] or []),
        error=row["error"] or "",
    )
    bundle.built_at = row["built_at"]
    return bundle


def load(client_id: int, commit_sha: str) -> Bundle | None:
    row = db.query_one(
        "SELECT * FROM client_source_bundle WHERE client_id = %s AND commit_sha = %s",
        (client_id, commit_sha))
    return _from_row(row) if row else None


def latest(client_id: int) -> Bundle | None:
    """The newest bundle for one client, whatever commit it was built at."""
    row = db.query_one(
        "SELECT * FROM client_source_bundle WHERE client_id = %s "
        "ORDER BY built_at DESC LIMIT 1", (client_id,))
    return _from_row(row) if row else None


def summary(client_id: int, commit_sha: str) -> dict | None:
    """Counts without dragging the whole source out of the database.

    The knowledge page wants to say "12 files, 17k tokens" and nothing more;
    selecting `files` to count them would move megabytes to render one line.
    """
    return db.query_one(
        "SELECT file_count, byte_count, est_tokens, truncated, error, built_at "
        "FROM client_source_bundle WHERE client_id = %s AND commit_sha = %s",
        (client_id, commit_sha))


def context_for(client_id: int, commit_sha: str) -> str:
    """The prompt block for one client at one commit.

    Raises rather than returning an empty string: an agent silently reasoning
    with no source at all is the failure this whole module exists to prevent.
    """
    bundle = load(client_id, commit_sha)
    if bundle is None:
        raise BundleError(
            "No source bundle for this client at this commit. Refresh the "
            "knowledge base first — the agent has nothing to read otherwise.")
    if not bundle.ok:
        raise BundleError(bundle.error or "The stored source bundle is empty.")
    return bundle.as_prompt()
