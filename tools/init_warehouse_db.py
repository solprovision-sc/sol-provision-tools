"""
init_warehouse_db.py — Initialize the warehouse_inventory.db schema.

Creates (or migrates) warehouse_inventory.db, the dedicated SQLite store for the
org's ore reserves manifest. The data originates in a shared Google Sheet (the
same one the Discord "Warehouse Reserves Manifest" bot reads); a cron script
(tools/pull_warehouse_inventory.py) mirrors it here on a schedule, and the
website's /api/officers/warehouse endpoint reads it. Idempotent — safe to run
repeatedly.

Tables:
  warehouse_inventory — one row per lot (material + quality band + location).
                        The pull script REPLACES this wholesale each run, since
                        the sheet is the single source of truth.
  warehouse_meta      — key/value freshness metadata (pulled_at, status, source).

Default DB path: <repo root>/app/warehouse_inventory.db   (sits next to the app's
dataforge.db, matching server.py's _resolve_warehouse_db_path). Override with
--db or the WAREHOUSE_DB env var (set this on the server to point at the shared
data dir).

Run from repo root:
    python tools/init_warehouse_db.py            # schema only
    python tools/init_warehouse_db.py --seed     # schema + a sample snapshot
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "app" / "warehouse_inventory.db"


# ── Schema ────────────────────────────────────────────────────────────────────
# Mirrors WAREHOUSE_SCHEMA in app/server.py. Kept in sync by hand (both are tiny);
# the server auto-creates the same tables on read, so this script mainly exists
# for a clean first-run + the --seed sample.

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouse_inventory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    material   TEXT NOT NULL,
    qty        REAL,
    quality    TEXT,            -- refinery quality band, e.g. '750-799'
    location   TEXT,
    row_index  INTEGER,         -- source sheet order, for stable display
    pulled_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouse_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# Sample snapshot transcribed from the Discord "Warehouse Reserves Manifest"
# screenshot. Used by --seed so the dashboard renders a realistic table before
# the live Google Sheet pull is wired up. The cron pull overwrites all of this.
SEED_ROWS = [
    # (material, qty, quality, location)
    ("Agricium",        0.5, "750-799", "Checkmate"),
    ("Aluminum",        1.0, "750-799", "Arc - L1"),
    ("Aluminum",        0.5, "850-899", "Arc - L1"),
    ("Aluminum",        1.0, "501-749", "Arc - L1"),
    ("Aluminum",        1.0, "750-799", "Arc - L1"),
    ("Aslarite",        0.4, "850-899", "Arc - L1"),
    ("Aslarite",        0.0, "501-749", "Arc - L1"),
    ("Beryl",           2.7, "850-899", "Arc - L1"),
    ("Beryl",           0.2, "900-949", "Arc - L1"),
    ("Beryl",           2.0, "501-749", "Arc - L1"),
    ("Beryl",           0.9, "850-899", "Arc - L1"),
    ("Bexalite",        0.8, "850-899", "Checkmate"),
    ("Borase",          0.8, "850-899", "Checkmate"),
    ("Copper",          1.0, "501-749", "Arc - L1"),
    ("Copper",          2.7, "850-899", "Arc - L1"),
    ("Copper",          0.2, "501-749", "Arc - L1"),
    ("Corundum",        3.1, "750-799", "Arc - L1"),
    ("Corundum",        0.4, "850-899", "Arc - L1"),
    ("Corundum",        0.6, "850-899", "Arc - L1"),
    ("Corundum",        0.3, "501-749", "Arc - L1"),
    ("Hephaestanite",   1.0, "750-799", "Checkmate"),
    ("Hephaestanite",   0.3, "900-949", "Arc - L1"),
    ("Iron",            2.5, "850-899", "Checkmate"),
    ("Iron",            1.0, "501-749", "Arc - L1"),
    ("Pressurized Ice", 0.7, "850-899", "Arc - L1"),
    ("Silicon",         2.2, "750-799", "Arc - L1"),
    ("Silicon",         0.9, "850-899", "Arc - L1"),
    ("Titanium",        2.2, "850-899", "Arc - L1"),
    ("Titanium",        2.6, "750-799", "Arc - L1"),
    ("Titanium",        1.5, "850-899", "Arc - L1"),
    ("Titanium",        0.3, "750-799", "Arc - L1"),
    ("Tungsten",        0.2, "750-799", "Checkmate"),
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the warehouse tables if missing. Idempotent. Shared by init_db()
    and the pull script's lazy schema-ensure so there is one source of truth."""
    conn.executescript(SCHEMA)
    conn.commit()


def replace_inventory(conn: sqlite3.Connection, rows, pulled_at: int,
                      source: str, status: str = "ok", note: str | None = None) -> None:
    """Atomically swap the whole inventory for a fresh snapshot and stamp the
    freshness metadata. Replace-all (not upsert) because the sheet is the single
    source of truth — a deleted sheet row should disappear here too."""
    with conn:  # transaction: readers never see a half-empty table
        conn.execute("DELETE FROM warehouse_inventory")
        conn.executemany(
            "INSERT INTO warehouse_inventory "
            "(material, qty, quality, location, row_index, pulled_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(m, q, qual, loc, i, pulled_at)
             for i, (m, q, qual, loc) in enumerate(rows)],
        )
        meta = {
            "pulled_at": str(pulled_at),
            "status":    status,
            "source":    source,
            "rows":      str(len(rows)),
        }
        if note:
            meta["note"] = note
        conn.executemany(
            "INSERT INTO warehouse_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(meta.items()),
        )


def init_db(db_path: Path, seed: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        # WAL so the cron writer never blocks the Flask reader. Persistent setting.
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_schema(conn)
        print(f"  [ok] Schema ensured at {db_path}")
        if seed:
            replace_inventory(conn, SEED_ROWS, int(time.time()), source="seed")
            print(f"    seeded {len(SEED_ROWS)} sample rows")
        for tbl in ("warehouse_inventory", "warehouse_meta"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"    {tbl:<20} {n:>5} rows")
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Initialize / migrate warehouse_inventory.db")
    p.add_argument("--db", default=os.environ.get("WAREHOUSE_DB", str(DEFAULT_DB_PATH)),
                   help=f"Path to warehouse_inventory.db (default: {DEFAULT_DB_PATH})")
    p.add_argument("--seed", action="store_true",
                   help="load a sample snapshot (the Discord manifest screenshot) for preview")
    args = p.parse_args()
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path, seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
