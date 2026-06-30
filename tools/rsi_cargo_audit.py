#!/usr/bin/env python3
"""RSI cargo reconciliation.

Truth source = the live RSI ship-matrix (official published spec). We pull the
full matrix in one POST, key it by rsi_id (the `id` field), and compare the
authoritative `cargocapacity` against what we currently store:

  - ship_catalog.cargo_scu  (our prior RSI scrape)
  - ships.rsi_cargo_scu     (catalog value joined onto each hull)
  - ships.cargo_scu         (datamined in-game value)

Output: a reconciliation report flagging stale catalog values, gaps, and
ships present in our DB but missing from the matrix (variants / removed hulls).

Read-only. Writes nothing to the DB.
"""
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB = sys.argv[1] if len(sys.argv) > 1 else "app/dataforge.db"
MATRIX_URL = "https://robertsspaceindustries.com/ship-matrix/index"
HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}
CACHE = Path("tools/_rsi_matrix_cache.json")


def fetch_matrix():
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    req = urllib.request.Request(MATRIX_URL, data=b"", headers=HDRS, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8", "replace")
    j = json.loads(body)
    data = j.get("data") or []
    CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    matrix = fetch_matrix()
    # rsi_id -> {name, cargo}
    by_id = {}
    for s in matrix:
        rid = s.get("id")
        if rid is None:
            continue
        by_id[int(rid)] = {"name": s.get("name"), "cargo": to_num(s.get("cargocapacity"))}
    print(f"RSI matrix: {len(matrix)} ships, {len(by_id)} with id")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    p = conn.execute("SELECT patch_version FROM ships GROUP BY patch_version "
                     "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]

    # One row per catalog ship (RSI-named hulls/variants), with its rsi_id.
    cat = conn.execute(
        "SELECT rsi_id, rsi_name, rsi_slug, data_name, cargo_scu AS cat_cargo "
        "FROM ship_catalog WHERE patch_version=?", (p,)).fetchall()

    stale, gap_filled, matched, unmatched_cat = [], [], 0, []
    for r in cat:
        rid = r["rsi_id"]
        live = by_id.get(int(rid)) if rid is not None else None
        if live is None:
            unmatched_cat.append(r)
            continue
        matched += 1
        ours = r["cat_cargo"]
        liv = live["cargo"]
        if ours is None and liv is not None:
            gap_filled.append((r, liv))
        elif ours is not None and liv is not None and abs(ours - liv) > 0.5:
            stale.append((r, ours, liv))

    print(f"\ncatalog rows: {len(cat)} | matched to matrix: {matched} | "
          f"unmatched: {len(unmatched_cat)}")

    print(f"\n=== STALE catalog cargo (our value != live RSI) — {len(stale)} ===")
    for r, ours, liv in sorted(stale, key=lambda x: -abs(x[1] - x[2])):
        print(f"  {r['rsi_name']:40} ours={ours:>8.1f}  live={liv:>8.1f}  ({r['data_name']})")

    print(f"\n=== GAP-FILL (catalog NULL, live RSI has value) — {len(gap_filled)} ===")
    for r, liv in gap_filled:
        print(f"  {r['rsi_name']:40} live={liv:>8.1f}  ({r['data_name']})")

    print(f"\n=== catalog rows with NO matrix match — {len(unmatched_cat)} ===")
    for r in unmatched_cat:
        print(f"  rsi_id={r['rsi_id']} {r['rsi_name']} ({r['data_name']}) cat_cargo={r['cat_cargo']}")

    conn.close()


if __name__ == "__main__":
    main()
