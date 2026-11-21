"""PostgreSQL access for users and the activity log.

If DATABASE_URL is not set the app runs in legacy single-password mode (no user management).
"""
import os

import psycopg
from psycopg.rows import dict_row

URL = os.environ.get("DATABASE_URL", "")

SCHEMA = """
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


def run(sql, params=()):
    """Execute a statement; returns the first row if it has a RETURNING clause, else None."""
    with connect() as c:
        cur = c.execute(sql, params)
        return cur.fetchone() if cur.description else None
