"""
pull_uex_prices.py — Fetch live UEX commodity prices into uex_feed.db.

One run = one call to the UEX bulk endpoint `commodities_prices_all`, then an
append-only INSERT OR IGNORE of every row into price_snapshots. Because the
UNIQUE(id_commodity, id_terminal, date_modified) constraint de-dups silently,
only genuinely changed observations land — so we can poll every 15 min even
though UEX refreshes the bulk feed only ~hourly. See docs/uex_live_feed.md.

Designed to run unattended from cron. On any failure (network, non-200,
status != 'ok', empty data) it logs a pull_log row, inserts NO partial batch,
and exits non-zero so cron mail flags it.

Config (env vars):
  UEX_API_TOKEN       (required)  Bearer token from a UEX "My Apps" registration.
  UEX_FEED_DB         (optional)  Path to uex_feed.db (default: <repo root>/uex_feed.db).
  UEX_CLIENT_VERSION  (optional)  Sent as X-Client-Version if set.

Run from repo root:
    UEX_API_TOKEN=... python tools/pull_uex_prices.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Reuse the schema as the single source of truth so a fresh/stale db self-heals.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_uex_feed_db import DEFAULT_DB_PATH, TERMINAL_META_COLS, ensure_schema  # noqa: E402

BASE_URL = "https://api.uexcorp.uk/2.0/"
PRICES_ENDPOINT = "commodities_prices_all"
HTTP_TIMEOUT = 60  # seconds; the bulk payload is large but served from cache
# UEX sits behind Cloudflare, which 403s the default "Python-urllib" UA with
# error 1010. Identify ourselves with a real User-Agent so the request is served.
USER_AGENT = "sol-provision-tools/1.0 (+https://github.com/solprovision-sc; UEX live feed)"

# Snapshot columns in INSERT order. Mirrors price_snapshots minus the implicit
# rowid. pulled_at is appended by the script; the rest map 1:1 to API fields
# (date_modified is coalesced first — see row_to_snapshot()).
SNAPSHOT_COLS = (
    "uex_id", "id_commodity", "id_terminal",
    "price_buy", "price_buy_avg", "price_sell", "price_sell_avg",
    "scu_buy", "scu_buy_avg", "scu_sell_stock", "scu_sell_stock_avg",
    "scu_sell", "scu_sell_avg",
    "status_buy", "status_sell", "quality",
    "container_sizes",
    "date_modified", "pulled_at",
)


def _get_json(endpoint: str, token: str, client_version: str | None) -> tuple[str, list]:
    """GET BASE_URL/<endpoint>/ and return (http_status, data list).

    Raises on transport/protocol failure or a non-ok API status."""
    req = urllib.request.Request(f"{BASE_URL}{endpoint}/")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Authorization", f"Bearer {token}")
    if client_version:
        req.add_header("X-Client-Version", client_version)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            http_status = f"{resp.status} {resp.reason}"
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"fetch failed: {e}") from e

    if payload.get("status") != "ok":
        raise RuntimeError(f"API status != ok: {payload.get('status')!r}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("API returned non-list data")
    return http_status, data


def fetch(token: str, client_version: str | None) -> tuple[str, list[dict]]:
    """Call the bulk price endpoint. Returns (http_status, data_rows).

    Raises (incl. on empty data) so the caller logs the failure and exits
    without writing a partial batch."""
    http_status, data = _get_json(PRICES_ENDPOINT, token, client_version)
    if not data:
        raise RuntimeError("API returned empty data")
    return http_status, data


def _num(v):
    """API sends some numerics as strings/null; coerce to float or None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def row_to_snapshot(row: dict, pulled_at: int) -> tuple | None:
    """Map one API row to a price_snapshots tuple, or None to skip it.

    date_modified is the de-dup key and NOT NULL. Crowdsourced rows occasionally
    omit it; coalesce date_modified → date_added → pulled_at so one bad row
    neither aborts the batch nor (because every SQL NULL is distinct under the
    UNIQUE index) spams duplicate history. id_commodity/id_terminal are required."""
    id_commodity = _int(row.get("id_commodity"))
    id_terminal = _int(row.get("id_terminal"))
    if id_commodity is None or id_terminal is None:
        return None

    date_modified = _int(row.get("date_modified")) or _int(row.get("date_added")) or pulled_at

    return (
        _int(row.get("id")),
        id_commodity, id_terminal,
        _num(row.get("price_buy")), _num(row.get("price_buy_avg")),
        _num(row.get("price_sell")), _num(row.get("price_sell_avg")),
        _num(row.get("scu_buy")), _num(row.get("scu_buy_avg")),
        _num(row.get("scu_sell_stock")), _num(row.get("scu_sell_stock_avg")),
        _num(row.get("scu_sell")), _num(row.get("scu_sell_avg")),
        _int(row.get("status_buy")), _int(row.get("status_sell")),
        _int(row.get("quality")),
        row.get("container_sizes"),
        date_modified, pulled_at,
    )


def upsert_reference(conn: sqlite3.Connection, data: list[dict], pulled_at: int) -> None:
    """Refresh commodities/terminals from the denormalized names in the payload.

    INSERT ... ON CONFLICT DO UPDATE keeps names/codes current and registers any
    newly-seen id in the same run, so joins never miss a label."""
    commodities, terminals = {}, {}
    for row in data:
        cid = _int(row.get("id_commodity"))
        if cid is not None and cid not in commodities:
            commodities[cid] = (cid, row.get("commodity_name"), row.get("commodity_code"),
                                row.get("commodity_slug"), pulled_at)
        tid = _int(row.get("id_terminal"))
        if tid is not None and tid not in terminals:
            terminals[tid] = (tid, row.get("terminal_name"), row.get("terminal_code"),
                              row.get("terminal_slug"), pulled_at)

    # COALESCE(excluded.x, x): refresh the label when the payload has one, but
    # never overwrite a previously-good name/code/slug with a null from a sparse row.
    conn.executemany(
        """INSERT INTO commodities (id_commodity, name, code, slug, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id_commodity) DO UPDATE SET
               name=COALESCE(excluded.name, name), code=COALESCE(excluded.code, code),
               slug=COALESCE(excluded.slug, slug), updated_at=excluded.updated_at""",
        list(commodities.values()),
    )
    conn.executemany(
        """INSERT INTO terminals (id_terminal, name, code, slug, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id_terminal) DO UPDATE SET
               name=COALESCE(excluded.name, name), code=COALESCE(excluded.code, code),
               slug=COALESCE(excluded.slug, slug), updated_at=excluded.updated_at""",
        list(terminals.values()),
    )


def _upsert_commodities_ref(conn: sqlite3.Connection, rows: list[dict], pulled_at: int) -> None:
    """Upsert id/name/code/slug for commodities (the bulk feed omits the code)."""
    payload = [(_int(r.get("id")), r.get("name"), r.get("code"), r.get("slug"), pulled_at)
               for r in rows if _int(r.get("id")) is not None]
    conn.executemany(
        """INSERT INTO commodities (id_commodity, name, code, slug, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id_commodity) DO UPDATE SET
               name=COALESCE(excluded.name, name), code=COALESCE(excluded.code, code),
               slug=COALESCE(excluded.slug, slug), updated_at=excluded.updated_at""",
        payload,
    )


def _upsert_terminals_ref(conn: sqlite3.Connection, rows: list[dict], pulled_at: int) -> None:
    """Upsert terminals with the real code PLUS location/faction metadata
    (star system, planet, orbit, moon, station/city/outpost, faction, container
    cap). The meta column names match the API field names 1:1, so the value map
    is derived straight from TERMINAL_META_COLS."""
    meta_cols = [c for c, _ in TERMINAL_META_COLS]
    all_cols = ["id_terminal", "name", "code", "slug", "updated_at"] + meta_cols
    placeholders = ",".join("?" * len(all_cols))
    # COALESCE(excluded.x, x): refresh when the payload has a value, never null
    # out a previously-good one from a sparse row.
    set_clause = ", ".join(f"{c}=COALESCE(excluded.{c}, {c})"
                           for c in all_cols if c != "id_terminal")

    payload = []
    for r in rows:
        rid = _int(r.get("id"))
        if rid is None:
            continue
        vals = [rid, r.get("name"), r.get("code"), r.get("slug"), pulled_at]
        for c in meta_cols:
            v = r.get(c)
            vals.append(_int(v) if (c.startswith("id_") or c == "max_container_size") else v)
        payload.append(vals)

    conn.executemany(
        f"INSERT INTO terminals ({','.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id_terminal) DO UPDATE SET {set_clause}",
        payload,
    )


def refresh_reference_tables(conn: sqlite3.Connection, token: str,
                             client_version: str | None, pulled_at: int) -> str | None:
    """Best-effort: pull the dedicated /commodities & /terminals endpoints. The
    bulk price feed denormalizes only names, so real codes (and all terminal
    location/faction metadata) live here.

    Never raises — a reference hiccup must not sink the price pull. Returns a
    short note describing any failure (folded into pull_log), else None."""
    note = None
    for endpoint, upsert in (("commodities", _upsert_commodities_ref),
                             ("terminals", _upsert_terminals_ref)):
        try:
            _, rows = _get_json(endpoint, token, client_version)
            upsert(conn, rows, pulled_at)
        except (RuntimeError, sqlite3.Error) as e:
            note = (f"{note}; " if note else "") + f"{endpoint} refresh failed: {e}"
    return note


def log_pull(conn: sqlite3.Connection, pulled_at: int, rows_fetched: int,
             rows_inserted: int, http_status: str, note: str | None) -> None:
    conn.execute(
        """INSERT INTO pull_log (pulled_at, rows_fetched, rows_inserted, http_status, note)
           VALUES (?, ?, ?, ?, ?)""",
        (pulled_at, rows_fetched, rows_inserted, http_status, note),
    )
    conn.commit()


def run(db_path: Path, token: str, client_version: str | None, pulled_at: int) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")  # ride out a reader's lock
        ensure_schema(conn)  # self-heal a fresh or stale db

        # --- fetch (a failure here writes a log row and exits non-zero) ---
        try:
            http_status, data = fetch(token, client_version)
        except RuntimeError as e:
            log_pull(conn, pulled_at, 0, 0, "ERROR", str(e))
            print(f"  [error] {e}", file=sys.stderr)
            return 1

        # --- map rows; track skips for the log note ---
        snapshots, skipped = [], 0
        for row in data:
            snap = row_to_snapshot(row, pulled_at)
            if snap is None:
                skipped += 1
            else:
                snapshots.append(snap)

        # --- insert: total_changes delta gives the genuinely-new count, since
        #     INSERT OR IGNORE reports no per-row rowcount for ignored dupes ---
        placeholders = ",".join("?" * len(SNAPSHOT_COLS))
        before = conn.total_changes
        conn.executemany(
            f"INSERT OR IGNORE INTO price_snapshots ({','.join(SNAPSHOT_COLS)}) "
            f"VALUES ({placeholders})",
            snapshots,
        )
        rows_inserted = conn.total_changes - before

        upsert_reference(conn, data, pulled_at)
        ref_note = refresh_reference_tables(conn, token, client_version, pulled_at)
        conn.commit()

        note = "; ".join(n for n in (
            f"skipped {skipped} rows missing ids" if skipped else None,
            ref_note,
        ) if n) or None
        log_pull(conn, pulled_at, len(data), rows_inserted, http_status, note)
        print(f"  [ok] {http_status} - fetched {len(data)}, inserted {rows_inserted}"
              + (f", skipped {skipped}" if skipped else ""))
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Pull live UEX commodity prices into uex_feed.db")
    p.add_argument("--db", default=os.environ.get("UEX_FEED_DB", str(DEFAULT_DB_PATH)),
                   help=f"Path to uex_feed.db (default: {DEFAULT_DB_PATH})")
    p.add_argument("--token", default=os.environ.get("UEX_API_TOKEN"),
                   help="UEX Bearer token (default: UEX_API_TOKEN env var)")
    args = p.parse_args()

    if not args.token:
        print("ERROR: no UEX token. Set UEX_API_TOKEN or pass --token.", file=sys.stderr)
        return 2

    client_version = os.environ.get("UEX_CLIENT_VERSION")
    pulled_at = int(time.time())
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return run(db_path, args.token, client_version, pulled_at)


if __name__ == "__main__":
    sys.exit(main())
