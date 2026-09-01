"""Reading a client repository over the GitHub API.

Phase C needs three things out of a client's repo and nothing else: the list of
Odoo modules it contains, `qa/knowledge.yml`, and `qa/scenarios/**/*.yml`. So
this fetches files, not a working tree.

**Why not a local clone.** The runner clones; the control plane must not. It may
be deployed on Railway where the filesystem is ephemeral, forty client repos of
working trees is disk we would have to manage, and keeping them current means a
poller — the prior art is explicit that it never auto-pulls, and for good
reason. Three small files over HTTPS has none of those problems. When phase D's
AST source map needs whole modules, that work belongs on the runner, which
already has checkouts.

**Why not PyGithub.** The house rule is `subprocess` for git/gh and plain HTTP
over wrapper libraries. Four endpoints do not justify a dependency that has to be
kept current with an API we use a corner of.

**The join key is `owner/name`**, the GitHub remote, exactly as it is everywhere
else in this codebase. Never a config-file id: two independently-edited files
drift and produce 404s that tell you to go and edit YAML.
"""
from __future__ import annotations

import base64
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
TIMEOUT = 30.0

# A scenario file is a page of YAML. Anything above this is not one, and
# fetching it would only be a way to spend a request.
MAX_BLOB_BYTES = 512 * 1024
# Caps exist so a repo with a generated qa/ directory cannot turn one page load
# into a thousand API calls. When a cap bites it is reported, never silent — a
# truncated scenario index reads as "this module has no coverage".
MAX_SCENARIO_FILES = 300


class GitHubError(Exception):
    """Anything that went wrong reaching GitHub. Message is safe to show."""


class NotFound(GitHubError):
    """The repo, ref, or path does not exist. Distinct because it usually means
    a typo in the client's `owner/name`, not an outage."""


@dataclass(frozen=True)
class TreeEntry:
    path: str
    sha: str
    size: int


@dataclass(frozen=True)
class Commit:
    sha: str
    date: datetime | None
    subject: str
    author: str


def resolve_token(configured: str = "") -> str:
    """A token from config, the environment, or `gh`.

    `gh auth token` is consulted last and deliberately: on a developer laptop it
    is already there and asking someone to paste a PAT into a config file they
    do not need is friction for nothing. On a server `gh` is absent and the
    environment variable is the answer. Neither is required — public repos work
    unauthenticated, at a much lower rate limit.
    """
    import os

    if configured:
        return configured
    for var in ("QA_GATE_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


class GitHub:
    """One token, many repos.

    `api_root` is injectable so the tests can point at a fake server. Faking at
    the HTTP boundary rather than mocking this class is what makes the tests
    exercise the pagination, the base64 decoding, and the error mapping instead
    of asserting that a mock was called.
    """

    def __init__(self, token: str = "", *, api_root: str = API_ROOT,
                 timeout: float = TIMEOUT) -> None:
        self.token = token
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = httpx.get(f"{self.api_root}{path}", headers=headers,
                             params=params, timeout=self.timeout,
                             follow_redirects=True)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Could not reach GitHub: {exc}") from exc

        if resp.status_code == 404:
            raise NotFound(f"GitHub has no {path.lstrip('/')} "
                           f"{'(or the token cannot see it)' if self.token else '(is it private? no token is configured)'}.")
        if resp.status_code in (401, 403):
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise GitHubError(
                    "GitHub rate limit exhausted. Configure a token to raise it.")
            raise GitHubError(
                "GitHub refused the request. The token is missing, expired, or "
                "lacks access to this repository.")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub returned {resp.status_code} for {path}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubError(f"GitHub returned something that is not JSON for {path}.") from exc

    # ---- the four things we ask for ---------------------------------------

    def head_sha(self, repo: str, ref: str) -> str:
        """The commit a branch currently points at.

        Everything else is fetched by that sha rather than by branch name, so a
        push landing halfway through a sync cannot produce a knowledge file from
        one commit and a scenario index from another.
        """
        data = self._get(f"/repos/{repo}/commits/{ref}")
        sha = (data or {}).get("sha") if isinstance(data, dict) else None
        if not sha:
            raise GitHubError(f"No commit found for {repo}@{ref}.")
        return str(sha)

    def tree(self, repo: str, sha: str) -> list[TreeEntry]:
        """The whole tree at one commit, in one request.

        Recursive rather than walking directories: a repo with fifteen modules
        is fifteen requests the other way, and the truncation flag tells us
        honestly when the single request was not enough.
        """
        data = self._get(f"/repos/{repo}/git/trees/{sha}", {"recursive": "1"})
        if not isinstance(data, dict):
            raise GitHubError("Unexpected tree response from GitHub.")
        if data.get("truncated"):
            log.warning("%s: git tree at %s was truncated by GitHub", repo, sha[:8])
        return [
            TreeEntry(path=e["path"], sha=e["sha"], size=int(e.get("size") or 0))
            for e in data.get("tree", []) if e.get("type") == "blob"
        ]

    def blob_text(self, repo: str, sha: str) -> str:
        data = self._get(f"/repos/{repo}/git/blobs/{sha}")
        if not isinstance(data, dict):
            raise GitHubError("Unexpected blob response from GitHub.")
        if data.get("encoding") == "base64":
            raw = base64.b64decode(data.get("content") or "")
        else:
            raw = (data.get("content") or "").encode()
        # Someone's editor wrote latin-1 once; a knowledge file that fails to
        # decode should degrade to a mangled character, not to no knowledge.
        return raw.decode("utf-8", errors="replace")

    def last_commit(self, repo: str, ref: str, path: str) -> Commit | None:
        """The most recent commit touching a path. One request per path.

        Used only for the coverage map's "last changed" column, and only for
        modules we hold source for, because it is the one call here that scales
        with the number of modules rather than being a constant.
        """
        data = self._get(f"/repos/{repo}/commits",
                         {"sha": ref, "path": path, "per_page": 1})
        if not isinstance(data, list) or not data:
            return None
        c = data[0]
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        return Commit(
            sha=str(c.get("sha") or ""),
            date=_parse_date(author.get("date")),
            subject=(commit.get("message") or "").splitlines()[0][:120],
            author=author.get("name") or "",
        )


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- shapes derived from a tree --------------------------------------------

def module_dirs(entries: list[TreeEntry]) -> dict[str, str]:
    """Odoo modules in the repo: name -> directory path.

    A directory holding `__manifest__.py` is a module. That is Odoo's own
    definition and it needs no per-client configuration about where addons live,
    which matters because clients keep them in `addons/`, in `custom/`, in the
    repo root, and in all three at once.
    """
    out: dict[str, str] = {}
    for e in entries:
        if not e.path.endswith("/__manifest__.py"):
            continue
        directory = e.path[: -len("/__manifest__.py")]
        out[directory.rsplit("/", 1)[-1]] = directory
    return out


def qa_files(entries: list[TreeEntry]) -> tuple[TreeEntry | None, list[TreeEntry]]:
    """(knowledge.yml, scenario files) under `qa/`.

    Scenario files may be nested — the plan's own example is
    `qa/scenarios/sale/line_discount.yml` — so this matches on prefix rather
    than on a single directory listing.
    """
    knowledge = None
    scenarios: list[TreeEntry] = []
    for e in entries:
        if e.path == "qa/knowledge.yml":
            knowledge = e
        elif e.path.startswith("qa/scenarios/") and e.path.endswith((".yml", ".yaml")):
            if e.size <= MAX_BLOB_BYTES:
                scenarios.append(e)
    return knowledge, sorted(scenarios, key=lambda e: e.path)
