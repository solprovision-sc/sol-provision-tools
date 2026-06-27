"""
pull_warehouse_inventory.py — Mirror the warehouse ore manifest from a shared
Google Sheet into warehouse_inventory.db.

One run = one fetch of the sheet as CSV (via Google's gviz export, which works on
any sheet shared "Anyone with the link can view"), then an atomic replace-all of
the warehouse_inventory table. The website's /api/officers/warehouse endpoint
reads that table, so the Officer Dashboard's Ore Inventory section stays live.

Replace-all (not append) is deliberate: the sheet is the single source of truth,
so a lot that's been sold/removed in the sheet must vanish here too.

Designed to run unattended from cron. On any failure (network, non-200, empty,
unparseable) it leaves the previous good snapshot in place, stamps
warehouse_meta.status = 'error' with a note, and exits non-zero so cron mail
flags it.

Config (env vars):
  WAREHOUSE_SHEET_ID    (required)  The spreadsheet id — the long token in the
                                    sheet URL: docs.google.com/spreadsheets/d/<ID>/edit
  WAREHOUSE_SHEET_GID   (optional)  Numeric gid of the specific tab (from #gid=… in
                                    the URL). Use this OR WAREHOUSE_SHEET_NAME.
  WAREHOUSE_SHEET_NAME  (optional)  Tab name, if you prefer it over a gid.
  WAREHOUSE_DB          (optional)  Path to warehouse_inventory.db
                                    (default: <repo>/app/warehouse_inventory.db).

Run from repo root:
    WAREHOUSE_SHEET_ID=1AbC... WAREHOUSE_SHEET_GID=0 python tools/pull_warehouse_inventory.py
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Reuse the schema + replace helper as the single source of truth so a fresh or
# stale db self-heals on every run.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_warehouse_db import DEFAULT_DB_PATH, ensure_schema, replace_inventory  # noqa: E402

HTTP_TIMEOUT = 30  # seconds
# A real User-Agent avoids the occasional bot-block on the docs.google.com host.
USER_AGENT = "sol-provision-tools/1.0 (+https://github.com/solprovision-sc; warehouse manifest)"

# Header synonyms → canonical field. The live sheet's header names may not match
# the Discord display exactly, so we map case-insensitively and fall back to
# positional order (material, qty, quality, location) if headers don't match.
HEADER_SYNONYMS = {
    "material": {"material", "mat", "commodity", "ore", "item", "name", "resource"},
    "qty":      {"qty", "quantity", "scu", "amount", "stock", "count"},
    "quality":  {"quality", "grade", "qual", "purity"},
    "location": {"location", "loc", "where", "site", "warehouse", "storage"},
}


def build_csv_url(sheet_id: str, gid: str | None, sheet_name: str | None) -> str:
    """Google Visualization API CSV export. Works on link-shared (view) sheets
    without credentials. gid targets a specific tab; sheet_name is the named
    alternative."""
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
    params = {"tqx": "out:csv"}
    if gid:
        params["gid"] = gid
    elif sheet_name:
        params["sheet"] = sheet_name
    return base + "?" + urllib.parse.urlencode(params)


def fetch_csv(url: str) -> str:
    """GET the sheet as CSV text. Raises RuntimeError on any transport/HTTP
    failure so the caller logs it and exits without touching the good snapshot."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} {resp.reason}")
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"fetch failed: {e}") from e


def _num(v):
    """Parse a qty cell to float, tolerating blanks, commas and stray units."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    # Keep digits, sign and decimal point (drops a trailing 'SCU', '%', etc.).
    cleaned = "".join(c for c in s if c.isdigit() or c in ".-")
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    except ValueError:
        return None


def _resolve_columns(header: list[str]) -> dict[str, int]:
    """Map canonical field → column index using HEADER_SYNONYMS, or fall back to
    positional order when the header row doesn't look like ours."""
    norm = [h.strip().lower() for h in header]
    cols: dict[str, int] = {}
    for field, names in HEADER_SYNONYMS.items():
        for i, h in enumerate(norm):
            if h in names:
                cols[field] = i
                break
    if "material" not in cols:
        # Header didn't match — assume the manifest's natural column order.
        cols = {"material": 0, "qty": 1, "quality": 2, "location": 3}
    return cols


def parse_rows(csv_text: str) -> list[tuple]:
    """Turn CSV text into (material, qty, quality, location) tuples. Skips the
    header, blank lines, and rows with no material. Raises if there's no data."""
    reader = csv.reader(io.StringIO(csv_text))
    all_rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not all_rows:
        raise RuntimeError("sheet returned no rows")

    cols = _resolve_columns(all_rows[0])
    out: list[tuple] = []
    for r in all_rows[1:]:
        def cell(field):
            i = cols.get(field)
            return r[i].strip() if i is not None and i < len(r) else ""
        material = cell("material")
        if not material:
            continue
        out.append((material, _num(cell("qty")), cell("quality") or None,
                    cell("location") or None))
    if not out:
        raise RuntimeError("no inventory rows after the header")
    return out


def stamp_error(db_path: Path, pulled_at: int, note: str) -> None:
    """Record a failed pull without disturbing the last good snapshot."""
    try:
        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        conn.executemany(
            "INSERT INTO warehouse_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [("status", "error"), ("error_at", str(pulled_at)), ("note", note)],
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # logging the error must never itself crash the cron job


def run(db_path: Path, url: str, pulled_at: int) -> int:
    try:
        csv_text = fetch_csv(url)
        rows = parse_rows(csv_text)
    except RuntimeError as e:
        stamp_error(db_path, pulled_at, str(e))
        print(f"  [error] {e}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")  # ride out a reader's lock
        ensure_schema(conn)
        replace_inventory(conn, rows, pulled_at, source="gviz")
        print(f"  [ok] mirrored {len(rows)} rows from sheet")
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Mirror the warehouse Google Sheet into warehouse_inventory.db")
    p.add_argument("--db", default=os.environ.get("WAREHOUSE_DB", str(DEFAULT_DB_PATH)),
                   help=f"Path to warehouse_inventory.db (default: {DEFAULT_DB_PATH})")
    p.add_argument("--sheet-id", default=os.environ.get("WAREHOUSE_SHEET_ID"),
                   help="Google Sheet id (default: WAREHOUSE_SHEET_ID env var)")
    p.add_argument("--gid", default=os.environ.get("WAREHOUSE_SHEET_GID"),
                   help="Numeric gid of the tab (default: WAREHOUSE_SHEET_GID env var)")
    p.add_argument("--sheet-name", default=os.environ.get("WAREHOUSE_SHEET_NAME"),
                   help="Tab name, alternative to --gid (default: WAREHOUSE_SHEET_NAME env var)")
    args = p.parse_args()

    if not args.sheet_id:
        print("ERROR: no sheet id. Set WAREHOUSE_SHEET_ID or pass --sheet-id.", file=sys.stderr)
        return 2

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = build_csv_url(args.sheet_id, args.gid, args.sheet_name)
    return run(db_path, url, int(time.time()))


if __name__ == "__main__":
    sys.exit(main())
