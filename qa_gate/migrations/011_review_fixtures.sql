-- Every record a review created on a client's staging instance.
--
-- Most assertions cannot be checked against data that already exists. "A
-- statement line with a matching Payment ID reconciles" needs a statement line
-- with a matching Payment ID, and no client's staging happens to contain one.
-- So the gate has to create its own — which is the first time it writes to
-- somebody else's database, and that deserves a ledger rather than trust.
--
-- The ledger is written BEFORE the cleanup, not after, and persisted rather
-- than held in memory. If the process dies between creating a record and
-- removing it, the row is what lets the next run find the orphan. An in-memory
-- list would leave litter in a client's database with nothing pointing at it.
--
-- `removed_at` rather than deleting the row: "we created this and cleaned it
-- up" and "we never created it" are different facts, and only one of them is
-- reassuring.

CREATE TABLE review_fixtures (
    id           bigserial   PRIMARY KEY,
    run_id       bigint      NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    -- The scenario-local name the plan gave it, so an assertion can say "the
    -- invoice I made" without knowing the id Odoo will assign.
    ref          text        NOT NULL DEFAULT '',
    model        text        NOT NULL,
    res_id       integer     NOT NULL,

    created_at   timestamptz NOT NULL DEFAULT now(),
    removed_at   timestamptz,
    -- Set when removal was attempted and refused — usually because Odoo will
    -- not unlink a posted accounting entry. Surfaced rather than swallowed: a
    -- record we could not remove is something a human has to know about.
    remove_error text        NOT NULL DEFAULT ''
);

-- Cleanup reads "everything this run made that is still there", and the orphan
-- sweep reads the same across runs.
CREATE INDEX review_fixtures_live
    ON review_fixtures (run_id) WHERE removed_at IS NULL;
