-- Phase A: identity, client registry, and encrypted instance credentials.
--
-- Identity comes from Odoo (see qa_gate/odoo_client.py). There is no signup and
-- no password column: a person exists here because they exist in Odoo, and
-- removing them there removes their access here. `odoo_uid` is the join key.

CREATE TABLE users (
    id            bigserial PRIMARY KEY,
    odoo_uid      integer     NOT NULL UNIQUE,
    login         text        NOT NULL,
    name          text        NOT NULL DEFAULT '',
    email         text        NOT NULL DEFAULT '',
    is_admin      boolean     NOT NULL DEFAULT false,
    active        boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz
);

-- Server-side sessions. The cookie carries a random token; only its SHA-256
-- lands here, so a database leak does not hand over live sessions. There is no
-- signed-cookie payload because there is nothing worth putting in one.
CREATE TABLE sessions (
    token_hash   bytea       PRIMARY KEY,
    user_id      bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token   text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    ip           inet,
    user_agent   text
);
CREATE INDEX sessions_user_idx    ON sessions (user_id);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

-- A client and its single managed staging instance. There is exactly one
-- staging database per client and no way to make another (plan rev 3, §3), so
-- this is deliberately 1:1 rather than a client having many environments.
CREATE TABLE clients (
    id                bigserial PRIMARY KEY,
    slug              text        NOT NULL UNIQUE,
    name              text        NOT NULL,
    -- GitHub `owner/name`. The join key for finding a local checkout, chosen
    -- over a config id because it is the same string on every machine.
    github            text        NOT NULL DEFAULT '',
    odoo_version      text        NOT NULL DEFAULT '17.0',
    hosting_platform  text        NOT NULL DEFAULT 'other',  -- odoo_sh|cloudpepper|self|other
    staging_url       text        NOT NULL DEFAULT '',
    staging_db        text        NOT NULL DEFAULT '',
    -- Belt to the sentinel's braces (§3): the database name must match this
    -- before the gate will touch the instance, e.g. `%_staging`.
    db_name_pattern   text        NOT NULL DEFAULT '%_staging',
    -- 'rpc' and 'browser' are assumed for every client. 'shell' is a
    -- convenience. There is deliberately no 'db' capability in revision 3.
    capabilities      text[]      NOT NULL DEFAULT ARRAY['rpc','browser'],
    branch_mode       text        NOT NULL DEFAULT 'per_task', -- per_task|shared
    base_branch       text        NOT NULL DEFAULT 'main',
    active            boolean     NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    created_by        bigint      REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX clients_active_idx ON clients (active);

-- Scoping, not a security boundary: staff-only access means this decides what
-- the UI shows by default rather than enforcing tenant isolation.
CREATE TABLE user_clients (
    user_id    bigint NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    client_id  bigint NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    access     text   NOT NULL DEFAULT 'member',  -- member|owner
    PRIMARY KEY (user_id, client_id)
);

-- Credentials for reaching a client's staging instance. Separate table so it
-- can be granted separately and never joined into anything a browser session
-- reads (plan §21). Values are Fernet tokens under config.secret_key.
--
-- Two credential kinds, because Odoo requires both (verified in res_users.py):
--   rpc_api_key  - the probe/census plane. Required when the user has 2FA.
--   Persona passwords for the browser plane arrive in phase B; an API key
--   cannot open a web session, so they must be real passwords.
CREATE TABLE instance_secrets (
    client_id        bigint      PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
    rpc_login        text        NOT NULL DEFAULT '',
    rpc_api_key_enc  text        NOT NULL DEFAULT '',
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       bigint      REFERENCES users(id) ON DELETE SET NULL
);
