-- Every line of one client's addons, at one commit.
--
-- The agent's knowledge is the source itself, not prose about it. A generated
-- summary is strictly lossier than the thing it summarises, costs an API call to
-- produce, and drifts from the code the moment anyone commits — so for a repo
-- that fits in the context window (and at ~17k tokens against a 1M window, these
-- fit many times over) there is no reason to send anything else.
--
-- One row per (client, commit). Per client because a review must never see
-- another client's code: the bundle is selected by client_id and there is no
-- query in the codebase that spans two. Per commit because source read at one
-- sha describes code that may not exist at the next, and a bundle that silently
-- described the wrong revision would be worse than no bundle at all.
--
-- `files` is an ordered array of {path, text}. Ordered, and sorted by path when
-- written, because the prompt is assembled in array order and DeepSeek caches on
-- an exact prefix match — a bundle whose file order wobbled between calls would
-- turn every cache hit into a cache miss and multiply the cost by thirty.

CREATE TABLE client_source_bundle (
    client_id    bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    commit_sha   text        NOT NULL,
    ref          text        NOT NULL DEFAULT '',

    files        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    file_count   integer     NOT NULL DEFAULT 0,
    byte_count   integer     NOT NULL DEFAULT 0,
    -- Estimated, not measured: there is no local tokeniser for DeepSeek and one
    -- API round trip per bundle to count exactly is not worth it. Used for
    -- showing cost and for deciding whether a repo still fits in context.
    est_tokens   integer     NOT NULL DEFAULT 0,

    -- True when the whole repo did not fit the byte budget. Surfaced rather than
    -- silent: an agent answering from a partial bundle is answering about code
    -- it was never shown, and it has no way to know that unless we say so.
    truncated    boolean     NOT NULL DEFAULT false,
    skipped      jsonb       NOT NULL DEFAULT '[]'::jsonb,

    error        text        NOT NULL DEFAULT '',
    built_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (client_id, commit_sha)
);

-- The common read is "the newest bundle for this client", which without this is
-- a sort over every commit we have ever bundled for them.
CREATE INDEX client_source_bundle_recent
    ON client_source_bundle (client_id, built_at DESC);
