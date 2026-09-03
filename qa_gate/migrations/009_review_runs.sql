-- One review of one task, and the checkpoints inside it.
--
-- The run is a state machine rather than a function call because §7 requires
-- pausing to *free the database* and resuming to pick up where it left off. A
-- run that lived only in a request could not do either: killing the request
-- would lose the progress, and nothing would record which instance was still
-- being held.
--
-- Resume works by replaying nothing. Each phase writes its result as a step
-- row, and resuming starts at the first step that is not `done`. That is why
-- the phases are coarse (interpret, blast radius, plan, execute, verdict,
-- report) rather than one row per click: a checkpoint has to be a place where
-- stopping is safe and the work already done is still true.

CREATE TABLE review_runs (
    id            bigserial   PRIMARY KEY,
    client_id     bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    -- The Odoo task under review, in OUR Odoo. Denormalised name and stage so a
    -- run list renders without an RPC call per row, and so a finished run still
    -- reads correctly after the task is renamed or moved on.
    task_id       integer     NOT NULL,
    task_name     text        NOT NULL DEFAULT '',

    -- queued | running | paused | done | cancelled | failed
    -- `failed` is the run breaking; a *review* that finds problems is `done`
    -- with a fail verdict. Conflating them would make a broken staging instance
    -- read as a developer's bug.
    state         text        NOT NULL DEFAULT 'queued',
    -- pass | partial | fail | '' while unknown. Computed from assertion results
    -- in the verdict phase, never written by a language model (§14).
    verdict       text        NOT NULL DEFAULT '',
    phase         text        NOT NULL DEFAULT 'interpret',

    -- Which commit and which instance this run is about. A verdict is only
    -- meaningful against a known revision, and without this a run finished last
    -- week would look like it described today's code.
    commit_sha    text        NOT NULL DEFAULT '',
    persona_key   text        NOT NULL DEFAULT '',

    summary       text        NOT NULL DEFAULT '',
    error         text        NOT NULL DEFAULT '',

    started_by    bigint      REFERENCES users(id) ON DELETE SET NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    paused_at     timestamptz,
    finished_at   timestamptz,
    -- Set when the result was written back to the Odoo task, so a retry of the
    -- report phase cannot post the same summary twice.
    reported_at   timestamptz
);

-- The common reads are "runs for this client, newest first" and "is this task
-- already being reviewed", and both are a sort over everything without these.
CREATE INDEX review_runs_client_recent ON review_runs (client_id, started_at DESC);
CREATE INDEX review_runs_task ON review_runs (task_id, started_at DESC);

-- Only one run at a time may hold a client's staging instance. §7's whole point
-- is that pausing frees the database; two concurrent runs would make that
-- guarantee meaningless. Enforced here rather than in application code because
-- a race between two people pressing Start is exactly what a partial index of
-- this shape exists to prevent.
CREATE UNIQUE INDEX review_runs_one_active_per_client
    ON review_runs (client_id)
    WHERE state IN ('queued', 'running', 'paused');


CREATE TABLE review_steps (
    id            bigserial   PRIMARY KEY,
    run_id        bigint      NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    seq           integer     NOT NULL,
    phase         text        NOT NULL,
    title         text        NOT NULL DEFAULT '',

    -- pending | running | done | failed | skipped
    state         text        NOT NULL DEFAULT 'pending',
    -- Whether this step's assertions held. NULL for steps that assert nothing
    -- (interpretation produces no pass/fail); that distinction is what keeps a
    -- planning phase from counting as a passed test in the verdict.
    passed        boolean,

    -- The step's own output, shaped by its phase: requirements for interpret,
    -- impacted models for blast_radius, scenarios for plan, assertion results
    -- for execute. jsonb because nothing joins on it and each phase's shape is
    -- its own business.
    detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- What the model was asked and what it reasoned, kept so a proposal can be
    -- judged on its argument rather than only its conclusion.
    reasoning     text        NOT NULL DEFAULT '',
    note          text        NOT NULL DEFAULT '',

    started_at    timestamptz,
    finished_at   timestamptz,

    UNIQUE (run_id, seq)
);

CREATE INDEX review_steps_run ON review_steps (run_id, seq);


-- Evidence. Separate from the step so one step can carry several shots, and so
-- the bytes are not dragged out of the database every time a step is listed.
CREATE TABLE review_screenshots (
    id            bigserial   PRIMARY KEY,
    run_id        bigint      NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    step_id       bigint      REFERENCES review_steps(id) ON DELETE CASCADE,
    seq           integer     NOT NULL DEFAULT 0,

    caption       text        NOT NULL DEFAULT '',
    -- What the agent says it did and what it observed. Prose about a result,
    -- not the result itself.
    described     text        NOT NULL DEFAULT '',
    mimetype      text        NOT NULL DEFAULT 'image/png',
    -- Held here rather than on disk so a run survives a redeploy on a platform
    -- with an ephemeral filesystem, which is the deployment shape this app
    -- already assumes elsewhere.
    image         bytea,
    byte_count    integer     NOT NULL DEFAULT 0,
    captured_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX review_screenshots_run ON review_screenshots (run_id, seq);
