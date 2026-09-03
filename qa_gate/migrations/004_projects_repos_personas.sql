-- Odoo project linkage, multiple repositories per client, and browser personas.
--
-- Three things a client needs before a review can be started on one of its tasks:
--   1. which Odoo project its tasks live in, and which stage means "review me"
--   2. which repositories hold its addons, each with its own branch policy
--   3. a real username and password for the browser, because screenshot evidence
--      cannot be produced with an API key

-- ---------------------------------------------------------------- project ---
--
-- One project per client, deliberately. A client with two Odoo projects is
-- registered twice: it keeps the task query trivial, and the two halves usually
-- differ in staging instance and repositories anyway, which is most of a client
-- record already.
--
-- The name and stage id are resolved from Odoo when the form is saved and cached
-- here so a list of tasks can be rendered without a round trip per row. They are
-- a convenience copy, never the authority: the id is.
ALTER TABLE clients ADD COLUMN odoo_project_id   integer;
ALTER TABLE clients ADD COLUMN odoo_project_name text NOT NULL DEFAULT '';
-- The stage whose tasks are queued for review, e.g. "PM Review". Stored by name
-- as well as id because stage ids are per-project and a renamed stage should be
-- visible as a mismatch rather than silently matching nothing.
ALTER TABLE clients ADD COLUMN task_stage_id     integer;
ALTER TABLE clients ADD COLUMN task_stage_name   text NOT NULL DEFAULT '';

CREATE INDEX clients_project_idx ON clients (odoo_project_id);

-- ------------------------------------------------------------------ repos ---
--
-- A client's addons routinely live in more than one repository: the custom
-- addons, a theme, a fork of an OCA module. Each carries its own branch policy,
-- because a theme repo being on a shared branch while the addons repo is
-- per-task is a real and reasonable configuration.
--
-- `github` is `owner/name` and is the join key for a local checkout — never the
-- client slug. That lesson is inherited from odoo-dev-loop, where matching on a
-- config id meant two independently edited files had to agree on a slug and
-- produced 404s telling you to go edit YAML.
CREATE TABLE client_repos (
    id          bigserial   PRIMARY KEY,
    client_id   bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    github      text        NOT NULL,
    -- Defaults to `staging`, not `main`: this app only ever reads protected
    -- branches and only ever writes ones it created under `staging`.
    base_branch text        NOT NULL DEFAULT 'staging',
    branch_mode text        NOT NULL DEFAULT 'per_task',   -- per_task | shared
    label       text        NOT NULL DEFAULT '',
    active      boolean     NOT NULL DEFAULT true,
    position    integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, github)
);
CREATE INDEX client_repos_client_idx ON client_repos (client_id, position);

-- Carry the single repo each client may already have into the new table, so no
-- configuration is lost and the old column can stop being read.
INSERT INTO client_repos (client_id, github, base_branch, branch_mode)
SELECT id, github,
       CASE WHEN base_branch = '' THEN 'staging' ELSE base_branch END,
       branch_mode
FROM clients
WHERE github <> '';

-- --------------------------------------------------------------- personas ---
--
-- Browser users for tier 3 evidence. Verified in Odoo's res_users.py: the
-- API-key branch of _check_credentials sits behind `if not interactive:`, so an
-- API key authenticates RPC but CANNOT open a web session. Playwright therefore
-- needs a real password, and these accounts must not have 2FA enabled.
--
-- Separate from instance_secrets because the two are different in kind: that
-- holds one machine credential for the probe, this holds several human-shaped
-- logins whose whole purpose is to have *different* permissions from each other
-- (plan §2 decision 7 — access-rights regressions are invisible to an admin).
--
-- Use dedicated accounts, never a real employee's: their password rotates and
-- their group membership drifts, and both failures look exactly like regressions.
CREATE TABLE client_personas (
    id           bigserial   PRIMARY KEY,
    client_id    bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    -- Stable handle used by scenarios: `primary`, `sales_user`, `portal`, …
    key          text        NOT NULL,
    label        text        NOT NULL DEFAULT '',
    login        text        NOT NULL,
    password_enc text        NOT NULL DEFAULT '',
    -- When the credential last opened a real web session. Null means unproven.
    verified_at  timestamptz,
    verify_error text        NOT NULL DEFAULT '',
    active       boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   bigint      REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (client_id, key)
);
CREATE INDEX client_personas_client_idx ON client_personas (client_id);

-- ------------------------------------------------------------ app secrets ---
--
-- A service credential for our own Odoo, used to read tasks and later to write
-- chatter. It exists because a nightly run or a queued job has no user session
-- to borrow credentials from, and because storing each staff member's Odoo
-- password to make per-user RPC calls would be a far worse trade.
CREATE TABLE app_secrets (
    key        text        PRIMARY KEY,
    login      text        NOT NULL DEFAULT '',
    secret_enc text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by bigint      REFERENCES users(id) ON DELETE SET NULL
);
