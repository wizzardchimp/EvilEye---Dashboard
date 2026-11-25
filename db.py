"""PostgreSQL access for users and the activity log.

If DATABASE_URL is not set the app runs in legacy single-password mode (no user management).
"""
import os

import psycopg
from psycopg.rows import dict_row

URL = os.environ.get("DATABASE_URL", "")

SCHEMA = """
select pg_advisory_xact_lock(7203541);      -- two instances starting together (a deploy overlap) take turns instead of racing the seed rows
create table if not exists users (
    id            serial primary key,
    username      text not null,
    password_hash text not null,
    role          text not null default 'user' check (role in ('admin', 'user')),
    region        text not null default '',
    active        boolean not null default true,
    created_at    timestamptz not null default now(),
    created_by    text not null default '',
    last_login    timestamptz
);
create unique index if not exists users_username_lower on users (lower(username));

create table if not exists activity (
    id        bigserial primary key,
    ts        timestamptz not null default now(),
    user_id   integer references users (id) on delete set null,
    username  text not null default '',
    event     text not null,
    detail    text not null default '',
    ip        text not null default ''
);
create index if not exists activity_ts on activity (ts desc);
create index if not exists activity_user on activity (lower(username), ts desc);
create index if not exists activity_event on activity (event, ts desc);

create table if not exists placement_overrides (
    key       text primary key,
    victim    text not null,
    rocu      text not null,
    note      text not null default '',
    set_by    text not null default '',
    set_at    timestamptz not null default now()
);

alter table users add column if not exists last_visit timestamptz;
alter table users add column if not exists prev_visit timestamptz;
alter table users add column if not exists onboarded_at timestamptz;
alter table users add column if not exists must_change boolean not null default false;
alter table placement_overrides add column if not exists town text not null default '';

-- false-positive flags (any user) and "I am working on this" allocations
create table if not exists victim_flags (
    key        text primary key,
    victim     text not null,
    kind       text not null check (kind in ('not_uk', 'not_region')),
    rocu       text not null default '',
    note       text not null default '',
    flagged_by text not null default '',
    flagged_at timestamptz not null default now()
);
create table if not exists victim_alloc (
    key          text primary key,
    victim       text not null,
    initials     text not null,
    allocated_by text not null default '',
    allocated_at timestamptz not null default now()
);
alter table users add column if not exists initials text not null default '';
alter table users add column if not exists theme text not null default '';

-- groups: named sets of people (admin-made). A person can be in several; deleting a group or a user removes the links, never the other side.
create table if not exists user_groups (
    id          serial primary key,
    name        text not null,
    created_at  timestamptz not null default now(),
    created_by  text not null default ''
);
create unique index if not exists user_groups_name_lower on user_groups (lower(name));
create table if not exists user_group_members (
    user_id   integer not null references users (id) on delete cascade,
    group_id  integer not null references user_groups (id) on delete cascade,
    added_at  timestamptz not null default now(),
    added_by  text not null default '',
    primary key (user_id, group_id)
);
create index if not exists user_group_members_group on user_group_members (group_id);
-- "last active" and the event counts read the newest / all rows for one person: keep that quick as the log grows
create index if not exists activity_user_id_ts on activity (user_id, ts desc);

-- small key/value settings kept by the app (what has been migrated, whether alert emails are on, ...)
create table if not exists app_settings (
    key    text primary key,
    value  text not null default ''
);

-- markers: "Task" (built in) and any custom ones an admin makes. A marker on a breach carries a person's initials and when it was
-- allocated, started and completed; until it is completed it is "live" and shows in the Overview banner and the lists.
create table if not exists marker_types (
    id          serial primary key,
    name        text not null,
    colour      text not null default '#44c593',
    builtin     boolean not null default false,
    active      boolean not null default true,
    created_at  timestamptz not null default now(),
    created_by  text not null default ''
);
create unique index if not exists marker_types_name_lower on marker_types (lower(name));
insert into marker_types (name, colour, builtin, created_by)
    select 'Task', '#44c593', true, 'system' where not exists (select 1 from marker_types where builtin);
create table if not exists victim_markers (
    id            serial primary key,
    key           text not null,
    victim        text not null,
    type_id       integer not null references marker_types (id),
    initials      text not null,
    allocated_by  text not null default '',
    allocated_at  timestamptz not null default now(),
    started_at    timestamptz,
    completed_at  timestamptz,
    updated_by    text not null default '',
    updated_at    timestamptz not null default now(),
    unique (key, type_id)
);
create index if not exists victim_markers_type on victim_markers (type_id);
-- allocations made before markers existed become Task markers, once (victim_alloc is left in place, no longer read)
do $$ begin
  if not exists (select 1 from app_settings where key = 'markers_migrated') then
    insert into victim_markers (key, victim, type_id, initials, allocated_by, allocated_at)
      select a.key, a.victim, (select id from marker_types where builtin limit 1), a.initials, a.allocated_by, a.allocated_at from victim_alloc a
      on conflict do nothing;
    insert into app_settings (key, value) values ('markers_migrated', now()::text);
  end if;
end $$;

-- email alerts: an address per person (set by an admin; optional), and a switch for each person to stop their own
alter table users add column if not exists email text;
alter table users add column if not exists email_alerts boolean not null default true;
-- which regions a group is emailed about
create table if not exists group_alerts (
    group_id          integer primary key references user_groups (id) on delete cascade,
    enabled           boolean not null default false,
    regions           text[] not null default '{}',
    include_unplaced  boolean not null default false,
    updated_by        text not null default '',
    updated_at        timestamptz not null default now()
);
-- which breaches each group has already been told about (or was deliberately not, when alerts were switched on: 'baseline');
-- 'pending' is a claim held while an email is being sent, so two instances never send the same one twice
create table if not exists alert_sent (
    victim_key  text not null,
    group_id    integer not null references user_groups (id) on delete cascade,
    victim      text not null default '',
    kind        text not null default 'sent',
    sent_at     timestamptz not null default now(),
    primary key (victim_key, group_id)
);
-- one line per alert run or test email: when, to which groups, how many people, and how it went (never an address)
create table if not exists email_log (
    id          bigserial primary key,
    ts          timestamptz not null default now(),
    kind        text not null,
    group_name  text not null default '',
    to_count    integer not null default 0,
    subject     text not null default '',
    ok          boolean not null,
    detail      text not null default ''
);
create index if not exists email_log_ts on email_log (ts desc);
"""


def enabled():
    return bool(URL)


def connect():
    return psycopg.connect(URL, row_factory=dict_row, connect_timeout=8, autocommit=True)


def init():
    with connect() as c:
        c.execute(SCHEMA)


def one(sql, params=()):
    with connect() as c:
        return c.execute(sql, params).fetchone()


def many(sql, params=()):
    with connect() as c:
        return c.execute(sql, params).fetchall()


Duplicate = psycopg.errors.UniqueViolation


def txn(steps):
    """Run several (sql, params) statements as one all-or-nothing transaction."""
    with connect() as c:
        with c.transaction():
            for sql, params in steps:
                c.execute(sql, params)


def run(sql, params=()):
    """Execute a statement; returns the first row if it has a RETURNING clause, else None."""
    with connect() as c:
        cur = c.execute(sql, params)
        return cur.fetchone() if cur.description else None
