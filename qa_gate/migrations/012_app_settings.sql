-- Application settings that are not secrets.
--
-- `app_secrets` already holds the encrypted credentials, and the chosen AI
-- provider is not one: it is a plain string that decides which of several
-- stored keys gets used. Putting it there would have meant either encrypting a
-- value with nothing to hide, or adding a nullable secret column to a table
-- whose whole point is that the secret is always present.
--
-- Deliberately a key/value table rather than a settings row with one column per
-- setting. Everything here is chosen by an administrator in a form, read once
-- at the point of use, and never joined on.

CREATE TABLE app_settings (
    key        text PRIMARY KEY,
    value      text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by integer REFERENCES users(id) ON DELETE SET NULL
);

-- Existing installs already have a DeepSeek key stored under the old
-- single-provider name. Carry it forward so an upgrade does not silently look
-- like "no AI provider configured" and lose a working key.
INSERT INTO app_secrets (key, login, secret_enc, updated_by, updated_at)
SELECT 'ai_key_deepseek', login, secret_enc, updated_by, updated_at
FROM app_secrets WHERE key = 'deepseek_key'
ON CONFLICT (key) DO NOTHING;

INSERT INTO app_settings (key, value)
SELECT 'ai_provider', 'deepseek'
WHERE EXISTS (SELECT 1 FROM app_secrets WHERE key = 'deepseek_key' AND secret_enc <> '')
ON CONFLICT (key) DO NOTHING;
