"""
init_cargo_planner_db.py — Initialize the cargo_planner.db schema.

Creates (or migrates) cargo_planner.db with tables for user-saved hauling
plans and activity tracking. Idempotent — safe to run repeatedly. Add new
tables/columns to MIGRATIONS as the schema evolves.

Tables:
  users           — one row per Firebase-authenticated user (cached fields)
  mission_stacks  — top-level container for a planning session
  missions        — one cargo contract within a stack (1+ legs)
  legs            — one pickup→dropoff with one commodity within a mission
  activity_log    — feature-usage events for the future stats dashboard

Default DB path: <repo root>/cargo_planner.db
Override with --db or DATAFORGE_CARGO_PLANNER_DB env var.

Run from repo root:
    python tools/init_cargo_planner_db.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "cargo_planner.db"


# ── Schema ────────────────────────────────────────────────────────────────────
# Each statement is idempotent (CREATE ... IF NOT EXISTS). To evolve the
# schema, add ALTER TABLE statements to ALTER_MIGRATIONS below and bump them
# as needed; init_db() runs ALTERs only when the target column is missing.

SCHEMA = """
-- User key is discord_id (the app's canonical identity — Firebase is just the
-- login provider that yields the Discord ID; mirrors blueprint_ownership).
CREATE TABLE IF NOT EXISTS users (
    discord_id      TEXT PRIMARY KEY,
    first_seen_utc  TEXT NOT NULL,
    last_seen_utc   TEXT NOT NULL,
    username        TEXT,
    display_name    TEXT
);

CREATE TABLE IF NOT EXISTS mission_stacks (
    stack_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id            TEXT NOT NULL,
    name                  TEXT,                       -- null = 'Untitled'
    ship_uuid             TEXT,                       -- → dataforge.ships.uuid
    quantum_drive_uuid    TEXT,                       -- → dataforge.item_quantum_drives.uuid
    current_system        TEXT,                       -- 'STANTON' | 'PYRO' | 'NYX'
    current_location_uuid TEXT,                       -- → dataforge.nav_points.location_uuid
    is_active_draft       INTEGER NOT NULL DEFAULT 0, -- the auto-saved working plan
    created_utc           TEXT NOT NULL,
    updated_utc           TEXT NOT NULL,
    is_archived           INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (discord_id) REFERENCES users(discord_id)
);

CREATE TABLE IF NOT EXISTS missions (
    mission_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    stack_id    INTEGER NOT NULL,
    seq         INTEGER NOT NULL,                     -- order within stack
    notes       TEXT,
    FOREIGN KEY (stack_id) REFERENCES mission_stacks(stack_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS legs (
    leg_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id              INTEGER NOT NULL,
    seq                     INTEGER NOT NULL,         -- order within mission
    pickup_location_uuid    TEXT,                     -- → dataforge.nav_points.location_uuid
    dropoff_location_uuid   TEXT,
    commodity               TEXT,                     -- free-text in v0
    quantity_scu            REAL,
    pickup_same_as_current  INTEGER NOT NULL DEFAULT 0,
    pickup_same_as_prev     INTEGER NOT NULL DEFAULT 0,
    dropoff_same_as_prev    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS activity_log (
    log_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id     TEXT,                              -- nullable for anon
    event_type     TEXT NOT NULL,                     -- 'plan_saved' | 'plan_loaded' | 'page_visit' | ...
    event_details  TEXT,                              -- JSON
    timestamp_utc  TEXT NOT NULL,
    session_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_stacks_user      ON mission_stacks(discord_id, updated_utc DESC);
CREATE INDEX IF NOT EXISTS idx_stacks_active    ON mission_stacks(discord_id, is_active_draft, is_archived);
CREATE INDEX IF NOT EXISTS idx_missions_stack   ON missions(stack_id, seq);
CREATE INDEX IF NOT EXISTS idx_legs_mission     ON legs(mission_id, seq);
CREATE INDEX IF NOT EXISTS idx_activity_user_t  ON activity_log(discord_id, timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_activity_event   ON activity_log(event_type, timestamp_utc DESC);
"""


# Future-proof migration hook. When a new column is added to one of the
# tables above, append an entry here and re-run init. Use add_column_if_missing()
# in custom_migrations() rather than ALTER TABLE directly so the run stays
# idempotent against any prior schema version.
def custom_migrations(conn: sqlite3.Connection) -> list[str]:
    """Return a list of human-readable migration descriptions that ran."""
    applied = []
    # Example for future migrations:
    # if add_column_if_missing(conn, 'legs', 'estimated_payout_uec', 'INTEGER'):
    #     applied.append("legs.estimated_payout_uec added")
    return applied


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, sqltype: str) -> bool:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqltype}")
    return True


def init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        applied = custom_migrations(conn)
        conn.commit()
        print(f"  ✓ Schema ensured at {db_path}")
        for line in applied:
            print(f"    migrated: {line}")
        # Quick sanity report
        for tbl in ("users", "mission_stacks", "missions", "legs", "activity_log"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"    {tbl:<18} {n:>6} rows")
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Initialize / migrate cargo_planner.db")
    p.add_argument(
        "--db",
        default=os.environ.get("DATAFORGE_CARGO_PLANNER_DB", str(DEFAULT_DB_PATH)),
        help=f"Path to cargo_planner.db (default: {DEFAULT_DB_PATH})",
    )
    args = p.parse_args()
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
