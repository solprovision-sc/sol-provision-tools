"""
One-shot probe: fetch RSI Ship Matrix, attempt to join to ships in dataforge.db,
report match coverage and unmatched rows.

Not wired into anything. Throwaway diagnostic so we can size the join problem
and validate matcher tuning before changing the extractor.

Run from repo root:
    python tools/rsi_join_probe.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

RSI_URL = "https://robertsspaceindustries.com/ship-matrix/index"
DB_PATH = Path(__file__).resolve().parents[1] / "dataforge.db"

MFR_PREFIX_WORDS = {
    "aegis", "anvil", "argo", "banu", "consolidated", "outland", "c.o.",
    "crusader", "drake", "esperia", "gama", "greycat", "grin", "kruger",
    "misc", "mirai", "origin", "rsi", "tumbril", "vanduul", "xian",
    "starlifter",  # CRUS Hercules sub-brand
}


def normalize(text: str) -> set:
    """Lower, strip punctuation, split on whitespace, drop mfr/brand noise."""
    if not text:
        return set()
    cleaned = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return {t for t in cleaned.split() if t and t not in MFR_PREFIX_WORDS}


def slug_tokens(url: str) -> set:
    if not url:
        return set()
    cleaned = url.replace("/pledge/ships/", "").replace("/", " ").replace("-", " ")
    return normalize(cleaned)


def best_match(game_tokens: set, mfr_code: str, rsi_by_mfr: dict):
    """Score = coverage*0.7 + jaccard*0.3 over (rsi.name + url slug) tokens."""
    best, best_score = None, 0.0
    for r in rsi_by_mfr.get(mfr_code, []):
        rsi_tokens = normalize(r["name"]) | slug_tokens(r["url"])
        if not rsi_tokens or not game_tokens:
            continue
        inter = game_tokens & rsi_tokens
        if not inter:
            continue
        coverage = len(inter) / len(game_tokens)
        jaccard = len(inter) / len(game_tokens | rsi_tokens)
        score = coverage * 0.7 + jaccard * 0.3
        if score > best_score:
            best, best_score = r, score
    return best, best_score


def main() -> int:
    print("Fetching RSI ship matrix...")
    req = urllib.request.Request(RSI_URL, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        rsi_data = json.loads(resp.read().decode("utf-8"))["data"]
    rsi_by_mfr = defaultdict(list)
    for r in rsi_data:
        rsi_by_mfr[(r["manufacturer"] or {}).get("code", "")].append(r)
    print(f"RSI ships: {len(rsi_data)} across {len(rsi_by_mfr)} manufacturers")

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    latest = conn.execute("SELECT MAX(patch_version) FROM ships").fetchone()[0]
    rows = conn.execute(
        """
        SELECT DISTINCT entity_name, display_name
          FROM ships
         WHERE patch_version = ?
           AND entity_name NOT LIKE '%\\_ai\\_%' ESCAPE '\\'
           AND entity_name NOT LIKE '%\\_pu\\_%' ESCAPE '\\'
           AND entity_name NOT LIKE '%\\_unmanned%' ESCAPE '\\'
           AND entity_name NOT LIKE '%\\_mission\\_%' ESCAPE '\\'
           AND entity_name NOT LIKE '%\\_template%' ESCAPE '\\'
           AND entity_name NOT LIKE '%\\_ea\\_ai%' ESCAPE '\\'
         ORDER BY entity_name
        """,
        (latest,),
    ).fetchall()
    conn.close()

    matched, weak, unmatched = [], [], []
    seen_rsi_ids = set()

    mfr_remap = {
        "AEGS": "AEGS", "ANVL": "ANVL", "CRUS": "CRUS", "DRAK": "DRAK",
        "MISC": "MISC", "RSI": "RSI", "CNOU": "CNOU", "TMBL": "TMBL",
        "MRAS": "ARGO", "ARGO": "ARGO", "BANU": "BANU", "ESPR": "ESPR",
        "GAMA": "GAMA", "GREY": "GREY", "GRIN": "GRIN", "KRIG": "KRIG",
        "MRAI": "MRAI", "ORIG": "ORIG", "VNCL": "VNCL", "XNAA": "XNAA",
    }

    for row in rows:
        ent = row["entity_name"]
        disp = row["display_name"] or ""
        mfr_prefix = ent.split("_", 1)[0].upper()
        mfr_code = mfr_remap.get(mfr_prefix, mfr_prefix)

        game_tokens = normalize(disp) - {mfr_prefix.lower()}
        match, score = best_match(game_tokens, mfr_code, rsi_by_mfr)

        if match and score >= 0.6:
            matched.append((ent, disp, mfr_code, match["name"], score))
            seen_rsi_ids.add(match["id"])
        elif match and score >= 0.3:
            weak.append((ent, disp, mfr_code, match["name"], score))
        else:
            unmatched.append((ent, disp, mfr_code,
                              match["name"] if match else "(none)", score))

    print()
    print(f"Strong matches (>=0.6): {len(matched)}")
    print(f"Weak matches (0.3-0.6): {len(weak)}")
    print(f"Unmatched (<0.3):       {len(unmatched)}")
    print(f"Game ships total:       {len(rows)}")
    print(f"Distinct RSI ships hit: {len(seen_rsi_ids)} / {len(rsi_data)}")

    print("\n=== WEAK matches (review these) ===")
    for ent, disp, mfr, rsi_name, score in weak:
        print(f"  [{score:.2f}] {ent:<45} ({disp}) -> {mfr} / {rsi_name}")

    print("\n=== UNMATCHED (first 40) ===")
    for ent, disp, mfr, rsi_name, score in unmatched[:40]:
        print(f"  [{score:.2f}] {ent:<45} ({disp}) -> closest: {mfr} / {rsi_name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
