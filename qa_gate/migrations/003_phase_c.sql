-- Phase C: the parsed read-model cache for a client's repository.
--
-- The curated knowledge base does NOT live in the database. It lives in the
-- client's own repo at qa/knowledge.yml, changes by pull request, and is read
-- from GitHub. This table is a cache of the *parsed* form, stamped with the
-- commit sha it came from, which is what makes it safe: a cache that knows
-- which commit it is can be discarded and rebuilt, and can say out loud when
-- the branch has moved on. A copy that did not know would be a second source of
-- truth, which §9 names as the failure mode to avoid.
--
-- One row per client. History is in git, where history belongs.
CREATE TABLE client_repo_cache (
    client_id    bigint      PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
    github       text        NOT NULL DEFAULT '',
    ref          text        NOT NULL DEFAULT '',
    commit_sha   text        NOT NULL DEFAULT '',
    -- Parsed qa/knowledge.yml, in the shape knowledge.Knowledge.as_payload
    -- produces. jsonb rather than relational tables because nothing joins on an
    -- invariant: it is read whole, per client, and its schema is the client's
    -- to change through a pull request we do not gate.
    knowledge    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    scenarios    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    -- name -> repo path, for every directory holding an __manifest__.py.
    modules      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- name -> {date, author} for the last commit touching each module.
    last_changed jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- Anything that went wrong that did not stop the sync: a scenario that does
    -- not parse, a cap that bit. Recorded so a thin index announces itself.
    warnings     jsonb       NOT NULL DEFAULT '[]'::jsonb,
    error        text        NOT NULL DEFAULT '',
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    fetched_by   bigint      REFERENCES users(id) ON DELETE SET NULL
);
