-- Bootstrap for a containerised PostgreSQL, run once on first start.
--
-- Two roles, deliberately:
--
--   stacos_migrator  owns the schema and runs migrations.
--   stacos_app       is the runtime role. NOSUPERUSER and NOBYPASSRLS, so
--                    row-level security genuinely applies to it.
--
-- Running the application as the table owner would silently defeat RLS, which
-- is the second line of defence behind the application's scoped query manager.
-- The split is the whole reason RLS is worth having here.

CREATE ROLE stacos_migrator LOGIN PASSWORD 'stacos_migrator_dev' CREATEDB;
CREATE ROLE stacos_app LOGIN PASSWORD 'stacos_app_dev' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

CREATE DATABASE stacos OWNER stacos_migrator;

\connect stacos

CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram search for the command palette
CREATE EXTENSION IF NOT EXISTS unaccent;    -- accent-insensitive search
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- exclusion constraints over date ranges
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- random bytes for token material

GRANT CONNECT ON DATABASE stacos TO stacos_app;
GRANT USAGE ON SCHEMA public TO stacos_app;

-- The app role reads and writes data but never owns or alters schema.
ALTER DEFAULT PRIVILEGES FOR ROLE stacos_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stacos_app;
ALTER DEFAULT PRIVILEGES FOR ROLE stacos_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO stacos_app;
