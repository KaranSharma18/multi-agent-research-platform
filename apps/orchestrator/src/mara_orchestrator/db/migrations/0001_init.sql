-- Orchestrator schema. Applied by the orchestrator at startup (namespace "orchestrator").
-- LangGraph's checkpoint tables are created separately by AsyncPostgresSaver.setup().

create table if not exists runs (
    id              text primary key,
    user_sub        text not null,
    username        text not null,
    question        text not null,               -- personal data already masked
    as_of           date not null,
    status          text not null,               -- queued | running | awaiting_approval | executing | completed
                                                 -- | failed | canceled | needs_clarification | refused
    stop_reason     text,
    options         jsonb not null default '{}',
    plan            jsonb,
    final           jsonb,
    cost_usd        numeric(12, 6) not null default 0,
    model_calls     integer not null default 0,
    prompt_versions jsonb not null default '{}',
    replay_of       text references runs (id) on delete set null,
    error           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    finished_at     timestamptz
);
create index if not exists runs_user_created on runs (user_sub, created_at desc);
create index if not exists runs_status on runs (status);

-- Everything a client needs to follow a run. Ids are global and increasing,
-- so SSE clients resume with Last-Event-ID.
create table if not exists run_events (
    id         bigserial primary key,
    run_id     text not null references runs (id) on delete cascade,
    type       text not null,
    data       jsonb not null default '{}',
    created_at timestamptz not null default now()
);
create index if not exists run_events_run on run_events (run_id, id);

-- Every tool call a worker made, with its (masked) result. Replay reads this.
create table if not exists tool_calls (
    id          bigserial primary key,
    run_id      text not null references runs (id) on delete cascade,
    task_id     text not null,
    seq         integer not null,
    server      text not null,
    tool        text not null,
    args        jsonb not null,
    args_sha256 text not null,
    result      jsonb,
    is_error    boolean not null default false,
    latency_ms  integer,
    created_at  timestamptz not null default now()
);
create index if not exists tool_calls_run on tool_calls (run_id, id);

-- Proposed write actions and their whole life: policy check, human decision, execution.
create table if not exists actions (
    id              text primary key,
    run_id          text not null references runs (id) on delete cascade,
    tool            text not null,
    args            jsonb not null,
    args_sha256     text not null,
    preview         text not null,
    risk            text not null,
    reason          text,
    status          text not null,               -- rejected_by_policy | pending_approval | approved | rejected
                                                 -- | expired | executing | executed | failed
    policy_reason   text,
    requested_by    text not null,
    approver        text,
    decided_at      timestamptz,
    decision_note   text,
    approval_token  text,                        -- short-lived; cleared once used
    idempotency_key text not null,
    attempts        integer not null default 0,
    result          jsonb,
    error           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    executed_at     timestamptz
);
create index if not exists actions_status on actions (status, updated_at);
create index if not exists actions_run on actions (run_id);
create index if not exists actions_requester on actions (requested_by, created_at);

create table if not exists audit_log (
    id         bigserial primary key,
    event      text not null,
    actor      text not null,
    run_id     text,
    data       jsonb not null default '{}',
    prev_hash  text not null,
    entry_hash text not null,
    created_at timestamptz not null default now()
);

-- Wake SSE streams (in any orchestrator replica) when an event lands.
create or replace function mara_notify_run_event() returns trigger language plpgsql as $$
begin
    perform pg_notify('mara_run_events', new.run_id || ':' || new.id::text);
    return new;
end $$;
drop trigger if exists run_events_notify on run_events;
create trigger run_events_notify after insert on run_events
    for each row execute function mara_notify_run_event();

-- Wake the executor when an action is approved.
create or replace function mara_notify_action() returns trigger language plpgsql as $$
begin
    if new.status = 'approved' and (tg_op = 'INSERT' or old.status is distinct from new.status) then
        perform pg_notify('mara_actions', new.id);
    end if;
    return new;
end $$;
drop trigger if exists actions_notify on actions;
create trigger actions_notify after insert or update of status on actions
    for each row execute function mara_notify_action();
