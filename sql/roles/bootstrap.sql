-- Cluster-level role and grant bootstrap (data/04 §3, data/05 §3).
--
-- This is a psql script, not an Alembic migration: roles are cluster-level and
-- are not captured by pg_dump. Environment provisioning runs it before the
-- lineage. Callers may override the variables with `psql -v name=value`; the
-- defaults are the disposable local-environment values.

\set ON_ERROR_STOP on

\if :{?migration_role}
\else
  \set migration_role bluelab
\endif
\if :{?app_role}
\else
  \set app_role bluelab_app
\endif
\if :{?role_password}
\else
  \set role_password bluelab
\endif
\if :{?database_name}
\else
  \set database_name bluelab
\endif

-- POSTGRES_USER is the cluster bootstrap identity. These two identities are
-- created separately so neither application traffic nor SECURITY DEFINER helper
-- ownership inherits superuser power from the container entrypoint.
select format(
  'create role %I login password %L bypassrls createrole',
  :'migration_role',
  :'role_password'
)
where not exists (
  select 1 from pg_catalog.pg_roles where rolname = :'migration_role'
)
\gexec

select format(
  'create role %I login password %L',
  :'app_role',
  :'role_password'
)
where not exists (
  select 1 from pg_catalog.pg_roles where rolname = :'app_role'
)
\gexec

-- Converge old/local clusters too: creation guards alone would preserve an
-- earlier over-grant forever.
select format(
  'alter role %I login password %L nosuperuser bypassrls createrole',
  :'migration_role',
  :'role_password'
)
\gexec

select format(
  'alter role %I login password %L nosuperuser nobypassrls nocreaterole',
  :'app_role',
  :'role_password'
)
\gexec

select format(
  'alter database %I owner to %I',
  :'database_name',
  :'migration_role'
)
\gexec

-- SECURITY DEFINER helpers set an empty search_path and fully qualify names.
-- This is the second belt: the application role cannot create a shadow object in
-- the public schema and then inherit a helper owner's BYPASSRLS privilege.
revoke all on schema public from public;

select format('revoke create on schema public from %I', :'app_role')
\gexec

select format('grant usage on schema public to %I', :'app_role')
\gexec

select format(
  'alter default privileges for role %I in schema public grant select, insert, update, delete on tables to %I',
  :'migration_role',
  :'app_role'
)
\gexec

select format(
  'alter default privileges for role %I in schema public grant execute on functions to %I',
  :'migration_role',
  :'app_role'
)
\gexec
