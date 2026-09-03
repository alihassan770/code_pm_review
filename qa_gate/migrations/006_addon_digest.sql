-- Per-addon digests: what a module does, read out of its own source.
--
-- Layer 2 of §9 is the source map, and until now it stopped at "this directory
-- holds an __manifest__.py". That is enough to list addons and to join them
-- against the instance, but it does not tell a reviewer what the module *is* —
-- which models it touches, what it reaches out to, where the risk lives.
--
-- This table holds a reading of the source produced by a language model. It is
-- kept deliberately separate from `client_repo_cache.knowledge`, which is the
-- curated overlay a human wrote and committed. That separation is the whole
-- point: §14 requires verdicts be computed from assertions, never from prose, so
-- a generated digest must never be mistaken for a ratified invariant. Everything
-- here is a *proposal* for a human to accept into qa/knowledge.yml, and the
-- columns are named so that reading a row makes that obvious.
--
-- Keyed by (client, module, commit) rather than (client, module): a digest
-- describes the source at one commit, and once the branch moves the digest is
-- about code that is no longer there. Keeping the sha in the key means a stale
-- digest is visibly stale rather than silently wrong, and it makes regeneration
-- an insert rather than a destructive update.

CREATE TABLE client_addon_digest (
    id            bigserial   PRIMARY KEY,
    client_id     bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    module        text        NOT NULL,
    -- The commit the source was read at. Part of the identity, not metadata.
    commit_sha    text        NOT NULL,
    -- Repo path of the module directory, so a digest can be traced back to the
    -- files it was built from without re-deriving it from the module name.
    path          text        NOT NULL DEFAULT '',

    -- The reading itself, in the shape addon_digest.Digest.as_payload produces:
    -- summary, models, depends, integrations, invariant/danger-zone proposals.
    -- jsonb for the same reason the knowledge column is: read whole, per module,
    -- and its shape belongs to the prompt rather than to the schema.
    digest        jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Which model produced it, and what it cost. Recorded because a digest is an
    -- opinion, and an opinion is worth less when you cannot tell what produced
    -- it. Also the only way to notice a prompt or model change moved the output.
    model         text        NOT NULL DEFAULT '',
    prompt_tokens integer     NOT NULL DEFAULT 0,
    output_tokens integer     NOT NULL DEFAULT 0,
    -- Files actually sent to the model. A digest built from a truncated read is
    -- a different claim than one built from the whole module, and the reviewer
    -- deserves to know which they are looking at.
    files_read    integer     NOT NULL DEFAULT 0,
    truncated     boolean     NOT NULL DEFAULT false,

    error         text        NOT NULL DEFAULT '',
    generated_at  timestamptz NOT NULL DEFAULT now(),
    generated_by  bigint      REFERENCES users(id) ON DELETE SET NULL,

    UNIQUE (client_id, module, commit_sha)
);

CREATE INDEX client_addon_digest_lookup
    ON client_addon_digest (client_id, commit_sha);
