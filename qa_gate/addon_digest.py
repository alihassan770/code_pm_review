"""Reading one addon's source and proposing what it means.

Layer 2 of §9 is the source map. Until now it answered "which directories hold
an `__manifest__.py`", which is enough to list addons and join them against the
instance but tells a reviewer nothing about what any of them *does*.

This module closes that gap by reading the source and asking DeepSeek for a
structured reading of it. Three things about that are deliberate.

**It is a proposal, never a fact.** §9's knowledge base is emphatically not
LLM summaries, and that restraint is right: a hallucinated field name in the
knowledge base poisons every review that reads it. So nothing here is written to
`qa/knowledge.yml`. The digest is a separate, clearly-labelled artefact whose
danger-zone and invariant entries are *candidates* a human copies into the
overlay by pull request. The database column names say so, and so does the UI.

**The facts come from the parse, not the model.** `depends`, the module name,
the file list and the model names declared with `_name`/`_inherit` are extracted
here in Python, from the source, deterministically. They are passed to the model
as context and echoed back for display, but they are never *sourced* from it. The
model is asked only for the judgement calls — what this module is for, and what
about it looks dangerous — because those are the parts a regex cannot do and a
wrong answer is visibly an opinion rather than a silently wrong field name.

**A digest belongs to a commit.** Source read at one sha describes code that may
not exist at the next, so the commit is part of the digest's identity and a stale
one is visibly stale rather than quietly wrong.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from . import ai, db, github
from .github import GitHub, GitHubError, TreeEntry

log = logging.getLogger(__name__)

#: Source files worth sending. Everything an Odoo module keeps its behaviour in,
#: and nothing it keeps its assets in — a minified JS bundle or a PNG would blow
#: the budget without changing the reading.
SOURCE_SUFFIXES = (".py", ".xml", ".yml", ".yaml", ".csv")

#: One file. Above this it is generated, vendored, or data — none of which a
#: reading of the module's intent is improved by.
MAX_FILE_BYTES = 60_000

#: Files per module. A module with more source than this exists, but the ones we
#: skip are almost always data and views; the cap is reported rather than silent.
MAX_FILES = 40

#: Characters of source sent in one request. Both DeepSeek v4 models take a 1M
#: token context, so this is a cost ceiling rather than a technical one: the
#: whole point of a digest is that it is cheap enough to run for every addon.
MAX_SOURCE_CHARS = 220_000

#: Files that answer "what is this for" first, so that when the budget runs out
#: it runs out on the least informative files rather than the most.
_PRIORITY = ("__manifest__.py", "__init__.py")

_MODEL_NAME = re.compile(r"""^\s*_(?:name|inherit)\s*=\s*["']([\w.]+)["']""", re.M)
_MANIFEST_DEPENDS = re.compile(r"""["']depends["']\s*:\s*\[(.*?)\]""", re.S)
_QUOTED = re.compile(r"""["']([^"']+)["']""")

SYSTEM = """\
You are reading the source of a single Odoo addon so that a technical project \
manager can review changes to it safely.

Answer ONLY with a JSON object in exactly this shape:

{
  "summary": "2-3 sentences: what this module is for, in business terms.",
  "does": ["short phrase per capability the module adds"],
  "integrations": ["each external system, API, gateway or device it talks to"],
  "danger_zone_candidates": [
    {"text": "one sentence on what must never be exercised without a stub",
     "why": "the file or symbol that makes you say so"}
  ],
  "invariant_candidates": [
    {"text": "one sentence stating something that must stay true",
     "why": "the file or symbol that makes you say so"}
  ],
  "review_notes": ["anything a reviewer of this module should know"]
}

Rules:
- Ground every claim in the source you were given. Cite the file or symbol in \
"why". If you cannot ground it, leave the list empty.
- Do NOT guess at behaviour from the module name.
- "integrations" means genuinely external: payment gateways, couriers, banks, \
device sync, third-party HTTP APIs. Not other Odoo modules.
- Return [] for any list you have nothing real to put in. An empty list is a \
correct answer; an invented entry is not.
- No prose outside the JSON object."""


@dataclass
class Digest:
    module: str = ""
    path: str = ""
    commit_sha: str = ""

    # ---- extracted from source, deterministically -------------------------
    depends: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    # ---- proposed by the model -------------------------------------------
    summary: str = ""
    does: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=list)
    danger_zone_candidates: list[dict] = field(default_factory=list)
    invariant_candidates: list[dict] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
    reasoning: str = ""

    # ---- provenance -------------------------------------------------------
    model: str = ""
    files_read: int = 0
    truncated: bool = False
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    generated_at = None

    @property
    def ok(self) -> bool:
        return bool(self.summary) and not self.error

    def as_payload(self) -> dict:
        return {
            "depends": self.depends, "models": self.models,
            "summary": self.summary, "does": self.does,
            "integrations": self.integrations,
            "danger_zone_candidates": self.danger_zone_candidates,
            "invariant_candidates": self.invariant_candidates,
            "review_notes": self.review_notes, "reasoning": self.reasoning,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Digest":
        d = row.get("digest") or {}
        out = cls(
            module=row["module"], path=row["path"], commit_sha=row["commit_sha"],
            depends=list(d.get("depends") or []), models=list(d.get("models") or []),
            summary=d.get("summary") or "", does=list(d.get("does") or []),
            integrations=list(d.get("integrations") or []),
            danger_zone_candidates=list(d.get("danger_zone_candidates") or []),
            invariant_candidates=list(d.get("invariant_candidates") or []),
            review_notes=list(d.get("review_notes") or []),
            reasoning=d.get("reasoning") or "",
            model=row["model"], files_read=row["files_read"],
            truncated=row["truncated"], prompt_tokens=row["prompt_tokens"],
            output_tokens=row["output_tokens"], error=row["error"] or "",
        )
        out.generated_at = row.get("generated_at")
        return out


# ---- reading the source ----------------------------------------------------

def _pick(entries: list[TreeEntry], module_path: str) -> tuple[list[TreeEntry], bool]:
    """Source files inside `module_path`, most informative first."""
    prefix = module_path.rstrip("/") + "/"
    inside = [
        e for e in entries
        if e.path.startswith(prefix)
        and e.path.endswith(SOURCE_SUFFIXES)
        and 0 < e.size <= MAX_FILE_BYTES
    ]

    def rank(e: TreeEntry) -> tuple[int, int, str]:
        name = e.path.rsplit("/", 1)[-1]
        if name in _PRIORITY:
            return (0, _PRIORITY.index(name), e.path)
        # Behaviour before presentation: a view tells you what it looks like,
        # a model tells you what it does.
        return (1 if e.path.endswith(".py") else 2, 0, e.path)

    inside.sort(key=rank)
    return inside[:MAX_FILES], len(inside) > MAX_FILES


def _extract_depends(manifest_src: str) -> list[str]:
    m = _MANIFEST_DEPENDS.search(manifest_src)
    return _QUOTED.findall(m.group(1)) if m else []


def _extract_models(sources: list[tuple[str, str]]) -> list[str]:
    found: set[str] = set()
    for path, text in sources:
        if path.endswith(".py"):
            found.update(_MODEL_NAME.findall(text))
    return sorted(found)


def _bundle(sources: list[tuple[str, str]]) -> tuple[str, bool]:
    """Concatenate with headers, stopping at the character budget."""
    parts: list[str] = []
    used = 0
    clipped = False
    for path, text in sources:
        block = f"\n===== {path} =====\n{text}\n"
        if used + len(block) > MAX_SOURCE_CHARS:
            clipped = True
            break
        parts.append(block)
        used += len(block)
    return "".join(parts), clipped


def build(*, repo: str, module: str, module_path: str, commit_sha: str,
          entries: list[TreeEntry], gh: GitHub, client: ai.DeepSeek) -> Digest:
    """Read one module and return its digest. Never raises for content reasons."""
    digest = Digest(module=module, path=module_path, commit_sha=commit_sha)

    chosen, capped = _pick(entries, module_path)
    if not chosen:
        digest.error = (
            f"No readable source under {module_path}. Every file is either empty, "
            f"larger than {MAX_FILE_BYTES // 1000}KB, or not a kind of file we read.")
        return digest

    sources: list[tuple[str, str]] = []
    for entry in chosen:
        try:
            sources.append((entry.path, gh.blob_text(repo, entry.sha)))
        except GitHubError as exc:
            log.info("digest %s: could not read %s (%s)", module, entry.path, exc)
    if not sources:
        digest.error = "None of this module's files could be read from GitHub."
        return digest

    digest.files_read = len(sources)
    for path, text in sources:
        if path.endswith("/__manifest__.py"):
            digest.depends = _extract_depends(text)
            break
    digest.models = _extract_models(sources)

    bundle, clipped = _bundle(sources)
    digest.truncated = capped or clipped

    context = (
        f"Odoo addon: {module}\n"
        f"Repository path: {module_path}\n"
        f"Declared depends (parsed from the manifest): "
        f"{', '.join(digest.depends) or 'none'}\n"
        f"Odoo models declared or extended (parsed from source): "
        f"{', '.join(digest.models) or 'none'}\n"
        f"{'NOTE: source was truncated; some files are missing.' if digest.truncated else ''}"
        f"\n\nSource follows.\n{bundle}"
    )

    try:
        answer = client.complete(SYSTEM, context, model=client.provider.reasoning,
                                 reasoning=True, json_object=True)
        parsed = answer.as_json()
    except ai.AIError as exc:
        digest.error = str(exc)
        return digest

    digest.model = answer.model
    digest.reasoning = answer.reasoning
    digest.prompt_tokens = answer.usage.prompt_tokens
    digest.output_tokens = answer.usage.output_tokens
    digest.summary = str(parsed.get("summary") or "").strip()
    digest.does = _strings(parsed.get("does"))
    digest.integrations = _strings(parsed.get("integrations"))
    digest.review_notes = _strings(parsed.get("review_notes"))
    digest.danger_zone_candidates = _proposals(parsed.get("danger_zone_candidates"))
    digest.invariant_candidates = _proposals(parsed.get("invariant_candidates"))

    if not digest.summary:
        digest.error = "The model returned JSON with no summary in it."
    return digest


def _strings(value) -> list[str]:
    """Coerce to a list of non-empty strings, dropping anything else.

    The model is asked for a shape and usually returns it; `json_object` mode
    guarantees only that the response parses. Anything unexpected is dropped
    rather than rendered, because a half-typed entry in a proposal list reads as
    a finding.
    """
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()]


def _proposals(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            out.append({"text": text, "why": str(item.get("why") or "").strip()})
    return out


# ---- the cache -------------------------------------------------------------

def save(client_id: int, digest: Digest, *, generated_by: int | None = None) -> None:
    db.execute(
        """
        INSERT INTO client_addon_digest
            (client_id, module, commit_sha, path, digest, model,
             prompt_tokens, output_tokens, files_read, truncated, error,
             generated_at, generated_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT (client_id, module, commit_sha) DO UPDATE SET
            path = EXCLUDED.path, digest = EXCLUDED.digest,
            model = EXCLUDED.model, prompt_tokens = EXCLUDED.prompt_tokens,
            output_tokens = EXCLUDED.output_tokens,
            files_read = EXCLUDED.files_read, truncated = EXCLUDED.truncated,
            error = EXCLUDED.error, generated_at = now(),
            generated_by = EXCLUDED.generated_by
        """,
        (client_id, digest.module, digest.commit_sha, digest.path,
         json.dumps(digest.as_payload()), digest.model, digest.prompt_tokens,
         digest.output_tokens, digest.files_read, digest.truncated,
         digest.error, generated_by),
    )


def load_all(client_id: int, commit_sha: str) -> dict[str, Digest]:
    """Every digest held for one commit, by module name."""
    rows = db.query(
        "SELECT * FROM client_addon_digest WHERE client_id = %s AND commit_sha = %s",
        (client_id, commit_sha))
    return {r["module"]: Digest.from_row(r) for r in rows}


def load_one(client_id: int, module: str, commit_sha: str) -> Digest | None:
    row = db.query_one(
        "SELECT * FROM client_addon_digest "
        "WHERE client_id = %s AND module = %s AND commit_sha = %s",
        (client_id, module, commit_sha))
    return Digest.from_row(row) if row else None
