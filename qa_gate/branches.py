"""Which branches this application may write to. The answer is: very few.

The rule, stated once so it can be enforced everywhere:

    **We read any branch. We only ever create, commit to, or push branches we
    own, and those live under `staging`. Anything that looks like a mainline or
    a production branch is refused, in any casing, always.**

This is a hard guard rather than a convention, because the failure it prevents
is pushing to a client's production branch — an incident that no amount of "the
caller should have checked" makes acceptable. Every git write in this codebase
must go through `assert_writable`, and there is a test that fails if a new
protected-looking name slips past it.

Two design notes worth keeping:

* **Refusal is by pattern, not by list membership alone.** `Main`, `MAIN`,
  `production`, `prod`, `refs/heads/master`, `origin/live` and `release/2024`
  all refuse. A list of exact lowercase strings would let `Production` through,
  and the one time that matters is the one time it is catastrophic.
* **Allowing is by prefix, not by exclusion.** A branch is writable because it
  is ours, not because it failed to look protected. That way an unfamiliar name
  — a branch someone created by hand, a vendor's release train — is refused by
  default rather than accepted by omission.
"""
from __future__ import annotations

import re

#: Names that are never writable, matched case-insensitively against the branch
#: with any `refs/heads/` or remote prefix stripped.
PROTECTED_NAMES = frozenset({
    "main", "master", "prod", "production", "live", "release",
    "default", "trunk", "stable", "develop", "development",
})

#: Anything under these prefixes is protected too, so `release/2024.1` and
#: `production/eu` refuse without needing to be enumerated.
PROTECTED_PREFIXES = ("release/", "production/", "prod/", "live/", "hotfix/")

#: The only namespace we create and manage. `staging` itself, plus anything
#: beneath it such as `staging/task-4471`.
MANAGED_ROOT = "staging"

_REMOTE_PREFIX = re.compile(r"^(refs/heads/|refs/remotes/[^/]+/|origin/)+", re.I)


class ProtectedBranch(Exception):
    """Raised instead of touching a branch this app must never write to."""


def normalize(branch: str) -> str:
    """Bare branch name: no ref path, no remote, no surrounding whitespace."""
    return _REMOTE_PREFIX.sub("", (branch or "").strip()).strip("/")


def is_protected(branch: str) -> bool:
    """True when the branch must never be written to.

    An empty or unparseable name counts as protected. Refusing to act on a
    branch we cannot identify is the only safe reading of ambiguity here.
    """
    name = normalize(branch)
    if not name:
        return True
    lowered = name.casefold()
    if lowered in PROTECTED_NAMES:
        return True
    return any(lowered.startswith(p) for p in PROTECTED_PREFIXES)


def is_managed(branch: str) -> bool:
    """True when the branch is one of ours, under the staging namespace."""
    lowered = normalize(branch).casefold()
    return lowered == MANAGED_ROOT or lowered.startswith(MANAGED_ROOT + "/")


def assert_writable(branch: str, *, action: str = "write to") -> str:
    """Return the normalized branch, or raise ProtectedBranch.

    Call this immediately before any operation that could modify a remote. It
    checks both directions on purpose: a branch must not be protected *and* must
    be one we manage. A name that is merely "not protected" is still refused,
    because this app has no business committing to somebody else's feature
    branch either.
    """
    name = normalize(branch)
    if is_protected(name):
        raise ProtectedBranch(
            f"Refusing to {action} {name or branch!r}. This app never modifies "
            f"mainline or production branches — it only creates and manages "
            f"branches under {MANAGED_ROOT!r}."
        )
    if not is_managed(name):
        raise ProtectedBranch(
            f"Refusing to {action} {name!r}: it is outside the {MANAGED_ROOT!r} "
            f"namespace. Use {MANAGED_ROOT}/<something> for branches this app owns."
        )
    return name


def branch_for_task(task_id: int, slug: str = "") -> str:
    """The branch name this app would create for a task.

    Always inside the managed namespace, so it is writable by construction
    rather than by a later check that somebody might forget.
    """
    suffix = re.sub(r"[^a-z0-9]+", "-", (slug or "").casefold()).strip("-")
    return f"{MANAGED_ROOT}/task-{int(task_id)}" + (f"-{suffix}" if suffix else "")


def describe_policy() -> dict:
    """For rendering the policy in the UI, so the rule is visible not folklore."""
    return {
        "protected": sorted(PROTECTED_NAMES),
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "managed_root": MANAGED_ROOT,
    }
