# DeepSeek in Odoo PM Agent

Everything in this file was verified against the live API on 2026-09-01, not
recalled. Where a number matters, the probe that produced it is written down so
it can be re-run when this goes stale.

---

## The rule that outranks everything else in this file

**AI is not on the verdict path.** Plan §14 requires a verdict be computed
deterministically from assertion results. A model here may:

- read source and *propose* what a module does (`addon_digest`),
- later, turn a task description into checkable assertions (§11),
- later, write the summary paragraph *about* a verdict already computed (§14).

It may never decide pass or fail, and nothing it produces enters
`qa/knowledge.yml` without a human committing it by pull request. If you find
`qa_gate.ai` imported from something that returns a pass/fail, that is the bug —
not a feature to extend.

The reason is not squeamishness. The knowledge base is read by every subsequent
review; a hallucinated field name in it is wrong forever and wrong silently. A
proposal that a human rejects costs nothing.

---

## Models

Three exist. Verified with `GET https://api.deepseek.com/models`:

| Model ID | Context | Max output | Use |
|---|---|---|---|
| `deepseek-v4-pro` | 1M | 384K | **The reasoning model.** Everything that reads source and forms a view. |
| `deepseek-v4-flash` | 1M | 384K | Cheap and less careful. Mechanical work needing no deliberation. |
| `deepseek-v4-flash-vision-exp` | 1M | 384K | Vision. Not used here — we have no screenshot-reading step yet. |

`qa_gate/ai.py` exposes the first two as `MODEL_REASONING` and `MODEL_FAST`.

### Why `deepseek-v4-pro` for digests

The digest's job is to notice things a regex cannot — that a module talks to a
payment gateway, that a method posts accounting entries. That is a judgement
call, and the difference between pro-with-reasoning and flash is precisely the
difference between listing a module's imports and understanding them. The cost
difference is real but small in absolute terms (below), and a wrong digest wastes
a reviewer's afternoon, which is worth more than a cent.

### Pricing

Per 1M tokens, off-peak to peak. Peak is UTC business hours; off-peak is half.

| | Input (cache miss) | Input (cache hit) | Output |
|---|---|---|---|
| `deepseek-v4-pro` | $0.66 – $1.32 | $0.022 – $0.044 | $1.98 – $3.96 |
| `deepseek-v4-flash` | $0.22 – $0.44 | $0.007 – $0.014 | $0.66 – $1.32 |

**Measured on a real addon.** `accounting_ext` from `HSxTech/customtours@staging`:
12 files, 15,053 input tokens, 3,033 output tokens, 61 seconds. That is roughly
**1.5 cents** at peak pro pricing. Digesting forty addons costs under a dollar.

Prompt caching is automatic — there is nothing to opt into and no `cache_control`
to place. `usage.prompt_cache_hit_tokens` reports what was served from cache, at
a thirtieth of the miss price. Re-reading the same addon is nearly free.

---

## API shape

OpenAI-compatible. `POST https://api.deepseek.com/chat/completions`,
`Authorization: Bearer sk-…`. No SDK is used — the surface we touch is a few
fields of one JSON body, and `httpx` is already a project dependency. Adding a
package to save writing those fields would be a dependency that outlives its
usefulness (the same call `html_clean.py` made about an HTML sanitiser).

### Reasoning — the one that will catch you out

**Reasoning is ON by default. Omitting `thinking` does not disable it.**

Verified: a request with no `thinking` field spent 78 reasoning tokens. Only an
explicit disable turns it off.

```jsonc
// reasoning on (the default; be explicit anyway)
{"thinking": {"type": "enabled"}, "reasoning_effort": "high"}

// reasoning off — REQUIRED to actually turn it off
{"thinking": {"type": "disabled"}}
```

Reasoning text returns on `message.reasoning_content`, separate from
`message.content`. `ai.Answer` keeps both, because a proposal a human has to
ratify is far easier to ratify when its argument is visible.

### The silent failure to guard against

`max_tokens` bounds reasoning **and** output together. Run out and you get
**HTTP 200 with `content: ""`**, `finish_reason: "length"`, and every token spent
on reasoning. Verified: `max_tokens: 30` returned empty content and 30 reasoning
tokens.

That looks exactly like a model that answered with nothing, and it sends whoever
debugs it to the prompt instead of to `max_tokens`. `ai.DeepSeek.complete` checks
`finish_reason` and raises `AIError` naming the limit. Do not remove that check.

### JSON output

`{"response_format": {"type": "json_object"}}` works on both models and
guarantees the response *parses* — it guarantees nothing about its shape. The
system prompt must still describe the schema, and every field must be coerced on
the way in. `addon_digest._strings` and `._proposals` drop anything unexpected
rather than rendering it, because a half-typed entry in a proposal list reads as
a finding.

---

## Where the key lives

`app_secrets` under the key `deepseek_key`, Fernet-encrypted with the same
`secret_key` as the Odoo service credential and the GitHub token.

- **Set by administrators only.** Settings → AI provider. Every route in
  `web/routes/settings.py` passes through `_admin()`, and `base.html` hides the
  nav link from non-admins. Ordinary members use the features and never see it.
- **Verified before storing**, via `GET /models`. A key that only fails the first
  time somebody generates a digest is the worst moment to learn it was pasted
  with a trailing space.
- **Never rendered back.** Only the last four characters are stored in the clear,
  as a fingerprint for telling two keys apart while rotating. `is_configured()`
  and `login_for()` never decrypt.
- **Never in a config file, environment variable, or the repo.** There is no
  `DEEPSEEK_API_KEY` fallback on purpose: an env var is one `docker inspect` or
  one stray `env` in a log away from being public, and unlike the GitHub token it
  buys nothing — there is no CLI path that needs it before a database exists.

### Rotating

Generate a new key at `platform.deepseek.com`, paste it into Settings → AI
provider, press Replace key. The old row is overwritten in place; there is
nothing to clean up. Do this immediately if a key has ever been pasted into a
chat window, a ticket, or a commit message.

---

## The knowledge base is the source, not a summary

**`qa_gate/source_bundle.py` is what the agent reads.** One row per
`(client, commit)` in `client_source_bundle`, holding every source file in the
repo at that commit.

This replaced an earlier plan to generate a Markdown description of each addon.
The measurement killed that idea: a real client repo is **~17,000 tokens** against
a **1,000,000-token** context window — 58× headroom. A generated summary would be
strictly lossier than the source it came from, cost an API call to produce, drift
from the code on every commit, and become a second source of truth that wins in
the prompt and loses in reality. There is no reason to accept any of that when
the source itself fits many times over.

| | Tokens | Cost per call |
|---|---|---|
| First call (cache miss) | 17,293 | $0.023 |
| Every call after (cache hit) | 17,293 | $0.0008 |

Measured, not estimated: a follow-up question against the same bundle reported
**14,720 of 14,768 input tokens served from cache**.

### Two details that are load-bearing

**One tarball, not N blob fetches.** `GET /repos/{repo}/tarball/{sha}` returns the
whole commit in a single request (~2s). Walking the tree and fetching each blob is
one request per file — hundreds, against a 5,000/hour budget, to answer one
question.

**File order must be stable.** DeepSeek caches on an exact prefix match. Files are
sorted by path on write and emitted in that order, and GitHub's
`owner-repo-sha/` archive prefix is stripped — leaving it on would put the sha in
every path and guarantee a cache miss on every new commit. Shuffle the order and
you pay 30× for nothing.

**Per client, always.** `load`, `latest`, `summary` and `context_for` all take a
`client_id`; no query here spans two clients. A review that could see another
client's source is a confidentiality failure, so the separation lives in the
primary key rather than in the caller's discipline.

### Using it

```python
from qa_gate import source_bundle
context = source_bundle.context_for(client.id, snap.commit_sha)
answer = ai.client(cfg.secret_key).complete(SYSTEM + "\n\n" + context, question)
```

`context_for` raises `BundleError` rather than returning `""` — an agent silently
reasoning with no source is the exact failure this module exists to prevent.

Rebuilt automatically by `repo_sync.sync`, so pressing **Refresh from GitHub**
updates the module list and the agent's source together. A bundle failure is
recorded as a warning, never raised: losing the whole refresh to recover none of
it would be a bad trade.

---

## The addon digest (superseded)

Kept in the tree (`qa_gate/addon_digest.py`, `client_addon_digest`) but **no
longer reachable from the UI** — the source bundle above does its job better. It
remains because the prompt and the fact/opinion split below are worth having if a
client repo ever outgrows the context window, at which point a per-module summary
becomes the fallback rather than the plan.


`qa_gate/addon_digest.py`. Reads one module's source at one commit and produces a
structured reading of it.

### What is a fact and what is an opinion

This split is the design, not an implementation detail:

| Field | Source |
|---|---|
| `depends` | **Parsed** from `__manifest__.py` in Python |
| `models` | **Parsed** from `_name` / `_inherit` in the source |
| `files_read`, `truncated` | **Counted** |
| `summary`, `does`, `integrations` | **Proposed** by the model |
| `danger_zone_candidates`, `invariant_candidates` | **Proposed** by the model |

Facts are extracted deterministically and passed to the model as context. They
are never *sourced* from it, because a wrong `depends` list is silently wrong
while a wrong summary is visibly an opinion.

### Budgets

All in `addon_digest`, all reported when they bite rather than silently applied:

| Constant | Value | Why |
|---|---|---|
| `SOURCE_SUFFIXES` | `.py .xml .yml .yaml .csv` | Where behaviour lives. Not assets. |
| `MAX_FILE_BYTES` | 60 KB | Above this it is generated, vendored, or data. |
| `MAX_FILES` | 40 | A cap on requests, not on ambition. |
| `MAX_SOURCE_CHARS` | 220,000 | A **cost** ceiling, not a technical one — context is 1M. |

Files are ordered `__manifest__.py`, `__init__.py`, other `.py`, then the rest,
so that when the budget runs out it runs out on views rather than on models.
`truncated` is surfaced in the UI: a digest built from a partial read is a
different claim than one built from the whole module.

### Storage

`client_addon_digest`, keyed `(client_id, module, commit_sha)`. The commit is
part of the identity because source read at one sha describes code that may not
exist at the next — a stale digest is then visibly absent rather than quietly
describing the wrong code. Regeneration is an insert, not a destructive update.

### Running it

From the UI: client → Knowledge → the addons table → **Read this addon**.
Synchronous and roughly a minute per module, which is fine for an explicit button
and would not be for a page load. Cached against the commit, so pressing it twice
for the same code costs nothing.

---

## Re-verifying this file

When DeepSeek ships a new model or changes a default, re-run these three probes
rather than trusting the text above:

```bash
# 1. Which models exist
curl -s https://api.deepseek.com/models -H "Authorization: Bearer $KEY"

# 2. Is reasoning still on by default? (look for reasoning_tokens > 0)
curl -s https://api.deepseek.com/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"model":"deepseek-v4-flash",
  "messages":[{"role":"user","content":"hi"}],"max_tokens":300}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["usage"])'

# 3. Does a truncated response still return HTTP 200 with empty content?
curl -s https://api.deepseek.com/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"model":"deepseek-v4-flash",
  "messages":[{"role":"user","content":"Explain Odoo."}],"max_tokens":30}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["choices"][0]; print(d["finish_reason"], repr(d["message"]["content"]))'
```

Pricing changes more often than shape: check
<https://api-docs.deepseek.com/quick_start/pricing>.
