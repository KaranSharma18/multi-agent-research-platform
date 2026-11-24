-- Creates the roles and databases MARA needs. Safe to run more than once.
-- Docker runs this automatically on first start; without Docker, run it once as a superuser:
--   uv run mara db init --superuser-url postgresql://postgres:<password>@localhost:5432/postgres
--
-- Dev passwords only. In production use your secret manager and separate clusters.

do $$
begin
    if not exists (select from pg_roles where rolname = 'mara') then
        create role mara login password 'mara';
    end if;
    if not exists (select from pg_roles where rolname = 'warehouse') then
        create role warehouse login password 'warehouse';
    end if;
    if not exists (select from pg_roles where rolname = 'agent_ro') then
        create role agent_ro login password 'agent_ro';
    end if;
end $$;

-- The agent's role can never write, and no query runs longer than five seconds.
alter role agent_ro set default_transaction_read_only = on;
alter role agent_ro set statement_timeout = '5s';

select 'create database mara owner mara'
where not exists (select from pg_database where datname = 'mara') \gexec
select 'create database warehouse owner warehouse'
where not exists (select from pg_database where datname = 'warehouse') \gexec

revoke all on database warehouse from public;
grant connect on database warehouse to agent_ro;
revoke all on database mara from public;
grant connect on database mara to mara;
