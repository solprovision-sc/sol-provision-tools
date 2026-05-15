"""
delete_patch.py — Delete all records for a given patch_version
==============================================================
Walks every table in the database, identifies which ones have a
`patch_version` column, and deletes rows matching the specified version.

Usage:
    python delete_patch.py <patch_version> --db <db_path>
    python delete_patch.py 4.7.0-live.11518367 --db dataforge.db
    python delete_patch.py 4.7.0-live.11518367 --db dataforge.db --dry-run

Options:
    --dry-run    Show what would be deleted without actually deleting
    --yes        Skip confirmation prompt (for automation)
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path


def get_tables_with_patch_version(conn):
    """Return list of table names that have a 'patch_version' column."""
    tables = []
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    for (table_name,) in rows:
        cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        col_names = [c[1] for c in cols]
        if "patch_version" in col_names:
            tables.append(table_name)
    return tables


def get_row_count(conn, table, patch_version):
    """Count rows in table matching the patch_version."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE patch_version = ?",
        (patch_version,)
    ).fetchone()
    return row[0] if row else 0


def main():
    parser = argparse.ArgumentParser(
        description="Delete all records for a given patch_version across all tables"
    )
    parser.add_argument(
        "patch_version",
        help="Full patch version string e.g. 4.7.0-live.11518367"
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the SQLite database"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))

    # ── Discover tables ───────────────────────────────────────────────────
    tables = get_tables_with_patch_version(conn)
    if not tables:
        print("No tables with a 'patch_version' column found.")
        conn.close()
        return

    print(f"\n{'='*60}")
    print(f"  Patch Cleanup")
    print(f"{'='*60}")
    print(f"  DB:     {db_path}")
    print(f"  Patch:  {args.patch_version}")
    print(f"  Mode:   {'DRY RUN' if args.dry_run else 'DELETE'}")
    print(f"{'='*60}\n")

    # ── Preview row counts ────────────────────────────────────────────────
    print(f"  Scanning {len(tables)} tables...\n")
    total_rows = 0
    table_counts = []
    for table in tables:
        count = get_row_count(conn, table, args.patch_version)
        total_rows += count
        table_counts.append((table, count))

    # Sort by count descending so largest tables are visible first
    table_counts.sort(key=lambda x: x[1], reverse=True)

    if total_rows == 0:
        print(f"  No rows found for patch_version = {args.patch_version}")
        conn.close()
        return

    for table, count in table_counts:
        if count > 0:
            print(f"    {table:<35} {count:>12,} rows")

    print(f"\n  {'TOTAL':<35} {total_rows:>12,} rows")
    print(f"{'='*60}\n")

    if args.dry_run:
        print(f"  (Dry run — no deletions performed)")
        conn.close()
        return

    # ── Confirmation ──────────────────────────────────────────────────────
    if not args.yes:
        response = input(
            f"  Delete {total_rows:,} rows across {len(tables)} tables? "
            f"[y/N]: "
        ).strip().lower()
        if response != "y":
            print("  Cancelled.")
            conn.close()
            return

    # ── Delete ────────────────────────────────────────────────────────────
    print(f"\n  Deleting...\n")
    t_start = time.time()
    deleted_total = 0

    for table, count in table_counts:
        if count == 0:
            continue
        cur = conn.execute(
            f"DELETE FROM {table} WHERE patch_version = ?",
            (args.patch_version,)
        )
        deleted = cur.rowcount
        deleted_total += deleted
        print(f"    {table:<35} {deleted:>12,} rows deleted")

    conn.commit()

    # ── Vacuum to reclaim space ───────────────────────────────────────────
    print(f"\n  Running VACUUM to reclaim disk space...")
    conn.execute("VACUUM")

    conn.close()

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"  Deleted {deleted_total:,} rows in {elapsed:.1f}s")
    print(f"  Database file size: {db_path.stat().st_size / (1024**2):.1f} MB")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()