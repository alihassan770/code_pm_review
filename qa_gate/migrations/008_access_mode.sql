-- How the gate reaches one client's staging instance.
--
-- Until now a client was expected to have both a browser sign-in (a real
-- password, for Playwright screenshots) and an API key (for JSON-RPC reads).
-- That turns out to be one credential too many.
--
-- Verified against a live Odoo 18 instance: a session opened with a real
-- password can call `/web/dataset/call_kw` — the same endpoint the web client
-- uses — and answer `search_count`, `fields_get` and `context_get` with only
-- the session cookie. An API key cannot do the reverse: Odoo's
-- `_check_credentials` puts the key branch behind `if not interactive:`, so a
-- key authenticates RPC and can never open a web session.
--
-- The browser sign-in is therefore a strict superset. Asking for both made the
-- weaker credential look mandatory and left people entering an API key they did
-- not need — so this column makes the choice explicit and the form shows one at
-- a time.
--
-- Default 'browser' because it is the one that can do everything, and because a
-- client configured only far enough to take screenshots should not be told it is
-- missing something.

ALTER TABLE clients
    ADD COLUMN access_mode text NOT NULL DEFAULT 'browser';

-- Existing rows that already have an API key keep using it: they were set up
-- under the old assumption and silently switching them to a persona that may
-- not exist would break a working client to tidy a column.
UPDATE clients c SET access_mode = 'api_key'
 WHERE EXISTS (
     SELECT 1 FROM instance_secrets s
      WHERE s.client_id = c.id
        AND s.rpc_api_key_enc IS NOT NULL
        AND s.rpc_api_key_enc <> ''
 );

ALTER TABLE clients
    ADD CONSTRAINT clients_access_mode_known
    CHECK (access_mode IN ('browser', 'api_key'));
