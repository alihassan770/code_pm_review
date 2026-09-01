-- Phase B: the pre-flight hygiene audit (UC-16) and the instance fingerprint.
--
-- The census itself is deliberately NOT stored. Plan §9 is explicit that layers
-- 1 and 2 are re-derived every run and never persisted, because a cached census
-- is a census that lies the moment somebody installs an app through the hosting
-- panel. What IS stored is the audit result (an auditable record of a decision
-- we made about a live client instance) and the fingerprint (plan §8, which the
-- storage model already places in Postgres).

-- One row per audit attempt against one client staging instance.
--
-- Every check is recorded, passing ones included, because §3 requires a pass to
-- be auditable after the fact and not merely a silence. `checks` is jsonb rather
-- than a child table: the check set changes as the gate grows, and a schema
-- migration per new check would be friction on exactly the thing we want people
-- adding freely. Nothing joins on an individual check.
CREATE TABLE instance_audits (
    id           bigserial   PRIMARY KEY,
    client_id    bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    -- pass    = every check passed or was a warning; the gate may run
    -- refuse  = at least one check failed; the gate must not run
    -- error   = the instance could not be reached or answered, verdict unknown.
    --           Deliberately distinct from `refuse`: "unsafe" and "unknown" are
    --           different answers and collapsing them hides outages.
    verdict      text        NOT NULL,
    checks       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    -- What we were actually talking to, captured so an old audit can be read
    -- without assuming the client row still says the same thing.
    server_version text      NOT NULL DEFAULT '',
    staging_url    text      NOT NULL DEFAULT '',
    staging_db     text      NOT NULL DEFAULT '',
    error        text        NOT NULL DEFAULT '',
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    duration_ms  integer     NOT NULL DEFAULT 0,
    run_by       bigint      REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX instance_audits_client_idx ON instance_audits (client_id, started_at DESC);

-- The §8 fingerprint. Cheap to compute over RPC and the thing that lets a stale
-- baseline declare itself rather than lying.
--
-- `payload` holds the module inventory the modules hash was computed from. That
-- is not the census: there is no field graph, no view inheritance chain, no ACL
-- set here — only the identity summary the hashes cover, which is what makes a
-- drift report able to say *which* module appeared rather than only that the
-- hash moved. A hash you cannot explain sends someone diffing by hand.
CREATE TABLE instance_fingerprints (
    id             bigserial   PRIMARY KEY,
    client_id      bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    audit_id       bigint      REFERENCES instance_audits(id) ON DELETE SET NULL,
    modules_sha    text        NOT NULL,
    config_sha     text        NOT NULL,
    view_count     integer     NOT NULL DEFAULT 0,
    view_max_write timestamptz,
    sentinel       text        NOT NULL DEFAULT '',
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    taken_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX instance_fingerprints_client_idx
    ON instance_fingerprints (client_id, taken_at DESC);
