-- Several Odoo projects per client, with one shared "review me" stage.
--
-- This reverses the single-project decision in 004. A client genuinely has more
-- than one project — development alongside support, or one per site — and
-- registering the same client twice to express that splits its repositories,
-- staging instance and audit history for no benefit.
--
-- The stage stays on the client, by NAME, and that is the load-bearing detail.
-- `project.task.type` ids are per project, so "PM Review" is a different id in
-- each one. Storing an id would force a stage choice per project and make the
-- obvious request — show me everything waiting for review — the awkward case.
-- Matching by name makes it the default.

CREATE TABLE client_projects (
    id                bigserial   PRIMARY KEY,
    client_id         bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    odoo_project_id   integer     NOT NULL,
    -- Resolved from Odoo when saved, so a task list renders without a lookup per
    -- row. A convenience copy; the id is the authority.
    odoo_project_name text        NOT NULL DEFAULT '',
    position          integer     NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, odoo_project_id)
);
CREATE INDEX client_projects_client_idx ON client_projects (client_id, position);

-- Carry across whatever 004 recorded, so nothing configured is lost.
INSERT INTO client_projects (client_id, odoo_project_id, odoo_project_name)
SELECT id, odoo_project_id, odoo_project_name
FROM clients
WHERE odoo_project_id IS NOT NULL;

-- `task_stage_name` on clients stays and becomes the single source. The id
-- column is now meaningless across several projects, so stop carrying it.
ALTER TABLE clients DROP COLUMN task_stage_id;
ALTER TABLE clients DROP COLUMN odoo_project_id;
ALTER TABLE clients DROP COLUMN odoo_project_name;

-- The database allowlist pattern is genuinely optional: an empty value makes the
-- audit report that the check proves nothing, rather than silently passing. The
-- old default of `%_staging` was wrong for Odoo Online, whose databases look like
-- `company-main-1234567`, so it quietly failed the audit for every such client.
-- Clear the default rather than shipping one that is wrong more often than right.
ALTER TABLE clients ALTER COLUMN db_name_pattern SET DEFAULT '';
UPDATE clients SET db_name_pattern = ''
 WHERE db_name_pattern = '%_staging' AND staging_db NOT LIKE '%\_staging';
