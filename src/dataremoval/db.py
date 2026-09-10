"""SQLite storage. One file, no ORM, no migrations framework beyond a version pragma."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 3

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS broker (
    key           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    tier          INTEGER NOT NULL DEFAULT 2,
    method        TEXT NOT NULL DEFAULT 'form',
    optout_url    TEXT,
    email         TEXT,
    requires      TEXT NOT NULL DEFAULT '',   -- comma separated
    recheck_days  INTEGER NOT NULL DEFAULT 180,
    feeds         TEXT NOT NULL DEFAULT '',
    notes         TEXT,
    url_verified  INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    verify_status TEXT,                        -- ok|moved|notfound|botwall|soft404|error
    verify_at     TEXT,
    verify_url    TEXT,                        -- URL actually landed on after redirects
    verify_note   TEXT
);

CREATE TABLE IF NOT EXISTS request (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_key    TEXT NOT NULL REFERENCES broker(key) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'pending',
    law           TEXT,                        -- ccpa | gdpr | state | none
    channel       TEXT,                        -- form | email | account | mail
    opened_at     TEXT,
    sent_at       TEXT,
    due_at        TEXT,                        -- statutory deadline
    closed_at     TEXT,
    confirmation  TEXT,                        -- ticket id / ref number
    profile_urls  TEXT NOT NULL DEFAULT '',    -- newline separated listing URLs
    recheck_at    TEXT
);

CREATE TABLE IF NOT EXISTS event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    INTEGER REFERENCES request(id) ON DELETE CASCADE,
    broker_key    TEXT,                        -- set when the event has no request
    at            TEXT NOT NULL,               -- ISO datetime; older rows are dates
    kind          TEXT NOT NULL,               -- status|note|escalation|recheck|verify
    detail        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_request_broker ON request(broker_key);
CREATE INDEX IF NOT EXISTS idx_request_status ON request(status);
CREATE INDEX IF NOT EXISTS idx_event_request  ON event(request_id);
CREATE INDEX IF NOT EXISTS idx_event_at       ON event(at);
CREATE INDEX IF NOT EXISTS idx_event_broker   ON event(broker_key);
"""

# Kept for callers that want the whole thing at once (tests, docs).
SCHEMA = SCHEMA_TABLES + SCHEMA_INDEXES

# A request is open until it reaches one of these.
TERMINAL = {"completed", "rejected", "not_found"}

STATUSES = [
    "pending",              # queued, nothing sent
    "sent",                 # request submitted
    "acknowledged",         # broker confirmed receipt
    "verification_required",# waiting on you: email/SMS/ID
    "completed",            # removed, confirmed
    "rejected",             # refused - candidate for escalation
    "not_found",            # no record of you
    "reappeared",           # was removed, showed up again
    "blocked",              # captcha/ID wall you chose not to clear
]


# version -> statements that take a DB from (version-1) to version.
MIGRATIONS = {
    2: [
        "ALTER TABLE broker ADD COLUMN verify_status TEXT",
        "ALTER TABLE broker ADD COLUMN verify_at TEXT",
        "ALTER TABLE broker ADD COLUMN verify_url TEXT",
        "ALTER TABLE broker ADD COLUMN verify_note TEXT",
    ],
    3: [
        "ALTER TABLE event ADD COLUMN broker_key TEXT",
    ],
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Bring an existing DB up to SCHEMA_VERSION. Fresh DBs get everything from
    SCHEMA and just have their version stamped."""
    row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
    current = int(row["v"]) if row else 0
    applied = 0
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        for stmt in MIGRATIONS[version]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                # CREATE TABLE IF NOT EXISTS already gave a fresh DB these columns.
                if "duplicate column name" not in str(e):
                    raise
        applied += 1
        current = version
    if applied:
        # Stamp it here, not in init(): migrate() is also called on its own by
        # commands that must not care whether init() ran this session.
        conn.execute(
            "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (str(current),),
        )
    conn.commit()
    return applied


def init(conn: sqlite3.Connection) -> None:
    # Order matters: tables, then column migrations, then indexes. An index on a
    # column that a migration is about to add cannot be created before it.
    conn.executescript(SCHEMA_TABLES)
    migrate(conn)
    conn.executescript(SCHEMA_INDEXES)
    conn.execute(
        "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )
    conn.commit()
