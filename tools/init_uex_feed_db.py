"""
init_uex_feed_db.py — Initialize the uex_feed.db schema.

Creates (or migrates) uex_feed.db, the dedicated SQLite store for the live UEX
commodity-price feed (see docs/uex_live_feed.md). Idempotent — safe to run
repeatedly. Add new columns to custom_migrations() as the schema evolves.

Tables:
  price_snapshots — append-only price history; one row per (commodity, terminal,
                    observation). De-dup is automatic via the UNIQUE constraint +
                    INSERT OR IGNORE in the pull script.
  commodities     — reference table (id → name/code/slug); upserted each pull.
  terminals       — reference table (id → name/code/slug); upserted each pull.
  pull_log        — one row per pull run, for observability.

Default DB path: <repo root>/uex_feed.db
Override with --db or UEX_FEED_DB env var.

Run from repo root:
    python tools/init_uex_feed_db.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "uex_feed.db"


# ── Schema ────────────────────────────────────────────────────────────────────
# Each statement is idempotent (CREATE ... IF NOT EXISTS). To evolve the schema,
# add add_column_if_missing() calls to custom_migrations() rather than ALTER
# TABLE directly, so the run stays idempotent against any prior schema version.

SCHEMA = """
-- Append-only price history. One row per (commodity, terminal, observation).
-- We insert every poll but only genuinely new observations land (see UNIQUE).
CREATE TABLE IF NOT EXISTS price_snapshots (
    uex_id             INTEGER,           -- UEX's own price record id; stable handle back to their row (nullable, non-unique)
    id_commodity       INTEGER NOT NULL,
    id_terminal        INTEGER NOT NULL,
    price_buy          REAL,
    price_buy_avg      REAL,
    price_sell         REAL,
    price_sell_avg     REAL,
    scu_buy            REAL,
    scu_buy_avg        REAL,
    scu_sell_stock     REAL,
    scu_sell_stock_avg REAL,
    scu_sell           REAL,
    scu_sell_avg       REAL,
    status_buy         INTEGER,
    status_sell        INTEGER,
    quality            INTEGER,
    date_modified      INTEGER NOT NULL,  -- UEX source timestamp (change-detection key; coalesced on ingest)
    pulled_at          INTEGER NOT NULL,  -- our ingest unix time
    UNIQUE (id_commodity, id_terminal, date_modified)  -- auto de-dup via INSERT OR IGNORE
);

-- Reference tables (names/codes change rarely). The pull script upserts any
-- newly-seen id on the fly from the denormalized names in the bulk payload, so
-- a fresh commodity/terminal never renders as a null name between daily refreshes.
CREATE TABLE IF NOT EXISTS commodities (
    id_commodity INTEGER PRIMARY KEY,
    name TEXT, code TEXT, slug TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS terminals (
    id_terminal INTEGER PRIMARY KEY,
    name TEXT, code TEXT, slug TEXT,
    updated_at INTEGER
);

-- Pull log for observability. Autoincrement PK so two runs in the same second
-- (e.g. a manual backfill alongside the cron run) never collide on pulled_at.
CREATE TABLE IF NOT EXISTS pull_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pulled_at     INTEGER NOT NULL,
    rows_fetched  INTEGER,
    rows_inserted INTEGER,
    http_status   TEXT,
    note          TEXT
);
"""

# Indexes created after the tables exist.
INDEXES = """
-- Powers the homepage "latest row per (commodity, terminal)" query.
CREATE INDEX IF NOT EXISTS idx_snap_latest    ON price_snapshots (id_commodity, id_terminal, date_modified DESC);
-- Single-axis trend views.
CREATE INDEX IF NOT EXISTS idx_snap_commodity ON price_snapshots (id_commodity, date_modified);
CREATE INDEX IF NOT EXISTS idx_snap_terminal  ON price_snapshots (id_terminal, date_modified);
"""


# Location/faction metadata for terminals, sourced from the dedicated /terminals
# endpoint (the bulk price feed carries only terminal_name). Powers the Trade
# detail panel's location column + system/planet/faction filters.
TERMINAL_META_COLS = [
    ("id_star_system", "INTEGER"), ("star_system_name", "TEXT"),
    ("id_planet", "INTEGER"),      ("planet_name", "TEXT"),
    ("id_orbit", "INTEGER"),       ("orbit_name", "TEXT"),
    ("id_moon", "INTEGER"),        ("moon_name", "TEXT"),
    ("space_station_name", "TEXT"),
    ("city_name", "TEXT"),
    ("outpost_name", "TEXT"),
    ("id_faction", "INTEGER"),     ("faction_name", "TEXT"),
    ("max_container_size", "INTEGER"),
]


def custom_migrations(conn: sqlite3.Connection) -> list[str]:
    """Return human-readable descriptions of any migrations that ran.

    Runs AFTER the CREATE-IF-NOT-EXISTS SCHEMA above, so it only handles tables
    that already exist in an older shape (which IF NOT EXISTS can't fix). Add
    add_column_if_missing() calls here as the schema grows.
    """
    applied: list[str] = []

    # Terminal location/faction enrichment (Trade detail panel).
    for col, sqltype in TERMINAL_META_COLS:
        if add_column_if_missing(conn, "terminals", col, sqltype):
            applied.append(f"terminals.{col} added")

    # Per-row container sizes from the price feed (e.g. "1,2,4,8,16,24,32").
    if add_column_if_missing(conn, "price_snapshots", "container_sizes", "TEXT"):
        applied.append("price_snapshots.container_sizes added")

    return applied


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, sqltype: str) -> bool:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqltype}")
    return True


def ensure_schema(conn: sqlite3.Connection) -> list[str]:
    """Bring a connection's uex_feed schema fully up to date, in order:
    create tables (IF NOT EXISTS) → heal old-shape tables → create indexes.
    Idempotent. Shared by init_db() and the pull script's lazy schema-ensure so
    there is one source of truth. Returns the list of migrations that ran."""
    conn.executescript(SCHEMA)
    applied = custom_migrations(conn)
    conn.executescript(INDEXES)
    conn.commit()
    return applied


def init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        # WAL is a persistent, db-level setting: the 15-min writer never blocks
        # page reads. Set once here; the pull script and Flask reader inherit it.
        conn.execute("PRAGMA journal_mode=WAL")
        applied = ensure_schema(conn)
        print(f"  [ok] Schema ensured at {db_path}")
        for line in applied:
            print(f"    migrated: {line}")
        for tbl in ("price_snapshots", "commodities", "terminals", "pull_log"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"    {tbl:<16} {n:>8} rows")
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Initialize / migrate uex_feed.db")
    p.add_argument(
        "--db",
        default=os.environ.get("UEX_FEED_DB", str(DEFAULT_DB_PATH)),
        help=f"Path to uex_feed.db (default: {DEFAULT_DB_PATH})",
    )
    args = p.parse_args()
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
