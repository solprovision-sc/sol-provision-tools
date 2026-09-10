#!/usr/bin/env python3
"""
Generate the starmap ore-concentration heat-map dataset from the org mining
spreadsheet.

Reads tools/solprovision-mining-tool.xlsx (the LEDGER sheet, which is the source
the HEAT_MAP pivot is built on) and writes a static JS module the starmap loads:

    app/static/starmap/data/ore_heatmap.js

This mirrors the HEAT_MAP pivot: rows = Location, columns = Found Ore, value =
% of that location's finds that were each ore (COUNTA of Found Ore as % of row).

Each Location ("SYSTEM - PARENT - POI") is resolved here to a render *anchor*
(a body name, a Lagrange code, an asteroid belt, or "none" when it can't be
placed) so the runtime only has to look the anchor up against the live scene —
no fragile string parsing in the browser. This is a TRIAL data drop; the eventual
production path swaps the spreadsheet read for a live DB query but keeps the same
output shape.

Run:  python tools/gen_ore_heatmap.py
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "tools" / "solprovision-mining-tool.xlsx"
OUT = ROOT / "app" / "static" / "starmap" / "data" / "ore_heatmap.js"

# LEDGER column indices (1-based): C = Location, D = Found Ore.
# Found Ore moved G -> D in the Sept 2026 workbook (the sheet gained LOCATIONS /
# ORES vocabulary tabs and the ledger was tightened to Timestamp | Pilot |
# Location | Found Ore). Reading the old G silently yields zero usable rows, so
# the header is asserted below rather than trusted.
COL_LOCATION = 3
COL_FOUND_ORE = 4


def norm_ws(s: str) -> str:
    """Collapse runs of whitespace so 'GLACIEAN BELT  - Belt ' compares cleanly."""
    return " ".join(str(s).split())


# Location ("SYSTEM - PARENT - POI", whitespace-normalised) -> render anchor.
#   body     : matched by name against world.bodyIndex (planets/moons + belts)
#   lagrange : matched by "ARC-L1" style code against the live Lagrange POIs
#   belt     : an asteroid belt body (disk dropped at the belt's ring position)
#   helio    : exact per-system heliocentric km (star at origin), fed straight
#              through world.helioToScene — the same frame the lagrange path
#              already uses. Coordinates come from dataforge.db `nav_points`.
#   none     : can't be geolocated from current data -> kept in data, not drawn
#
# Lookup is CASE-INSENSITIVE (see anchor_for) because the ledger's casing drifts
# between snapshots — 'AARON HALO - Belt' became 'AARON HALO - BELT' in Sept 2026
# and silently unmatched every row.
#
# A few intentional approximations (no distinct body exists for them yet):
#   'Yela Belt'      -> Yela moon          (Crusader's belt hugs Yela)
#   'GLACIEAN BELT'  -> Glaciem Ring belt  (spelling variant in the ledger)
#   'LEVSKI'         -> Delamar            (Levski's own nav_points row carries
#                                           local coords, ~71 km from the star)
#   Pyro 'Select Site' rows -> their parent planet
ANCHORS: dict[str, dict] = {
    # ── STANTON ──
    "STANTON - AARON HALO - Belt":    {"kind": "belt", "name": "Aaron Halo"},
    "STANTON - ARCCORP - Arc-L1":     {"kind": "lagrange", "code": "ARC-L1"},
    "STANTON - ARCCORP - Arc-L5":     {"kind": "lagrange", "code": "ARC-L5"},
    "STANTON - ARCCORP - Wala":       {"kind": "body", "name": "Wala"},
    "STANTON - CRUSADER - CRU-L1":    {"kind": "lagrange", "code": "CRU-L1"},
    "STANTON - CRUSADER - CRU-L4":    {"kind": "lagrange", "code": "CRU-L4"},
    "STANTON - CRUSADER - Cellin":    {"kind": "body", "name": "Cellin"},
    "STANTON - CRUSADER - Daymar":    {"kind": "body", "name": "Daymar"},
    "STANTON - CRUSADER - Yela":      {"kind": "body", "name": "Yela"},
    "STANTON - CRUSADER - Yela Belt": {"kind": "body", "name": "Yela"},
    "STANTON - HURSTON - Aberdeen":   {"kind": "body", "name": "Aberdeen"},
    "STANTON - HURSTON - HUR-L3":     {"kind": "lagrange", "code": "HUR-L3"},
    "STANTON - HURSTON - HUR-L4":     {"kind": "lagrange", "code": "HUR-L4"},
    "STANTON - MICROTECH - Euterpe":  {"kind": "body", "name": "Euterpe"},
    "STANTON - MICROTECH - MIC-L1":   {"kind": "lagrange", "code": "MIC-L1"},
    "STANTON - MICROTECH - MIC-L4":   {"kind": "lagrange", "code": "MIC-L4"},
    "STANTON - MICROTECH - Mic":      {"kind": "body", "name": "microTech"},
    "STANTON - HURSTON - Magda":      {"kind": "body", "name": "Magda"},
    "STANTON - MICROTECH - MINING BASE #R7J-WJ7":
        {"kind": "helio", "x": 22475091.3, "y": 37162895.7},
    # ── NYX ──
    "NYX - GLACIEAN BELT - Belt":     {"kind": "belt", "name": "Glaciem Ring"},
    "NYX - GLACIEAM BELT - BELT":     {"kind": "belt", "name": "Glaciem Ring"},
    "NYX - GLACIEM BELT - BELT":      {"kind": "belt", "name": "Glaciem Ring"},
    # Same belt, ledger row is missing the space before the dash.
    "NYX - GLACIEM BELT- BELT":       {"kind": "belt", "name": "Glaciem Ring"},
    "NYX - GLACIEM BELT - LEVSKI":    {"kind": "body", "name": "Delamar"},
    "NYX - KEEGER BELT - Belt":       {"kind": "belt", "name": "Keeger Belt"},
    # The Keeger sites all sit on the same ring (r = 0.321 AU) at different
    # angles, so exact coords keep them from stacking on one belt point.
    "NYX - KEEGER BELT - PSS ALPHA":  {"kind": "helio", "x": -45804303.1, "y": -14351319.0},
    "NYX - KEEGER BELT - PSS DELTA":  {"kind": "helio", "x": -6432457.1,  "y": -47567013.3},
    "NYX - KEEGER BELT - PSS Theta":  {"kind": "helio", "x": 40472701.1,  "y": -25806231.9},
    "NYX - KEEGER BELT - QV BRK-204": {"kind": "helio", "x": -45105001.8, "y": 16416858.0},
    "NYX - KEEGER BELT - QV BRK-320": {"kind": "helio", "x": -27531183.2, "y": 39318601.2},
    # ── PYRO ──
    "PYRO - MINING - RAB-KNAP":         {"kind": "none"},
    "PYRO - MINING BASE - Select Site": {"kind": "none"},
    "PYRO - PYRO 2 - Select Site":      {"kind": "body", "name": "Monox"},
    "PYRO - PYRO 4 - PY4":              {"kind": "body", "name": "Pyro IV"},
    "PYRO - PYRO 4 - Select Site":      {"kind": "body", "name": "Pyro IV"},
    "PYRO - PYRO 5 - Fuego":            {"kind": "body", "name": "Fuego"},
    "PYRO - PYRO 5 - VUUR":             {"kind": "body", "name": "Vuur"},
    # The mining bases are free-floating asteroid clusters orbiting the Pyro
    # star directly — no parent planet exists to anchor them to, which is why
    # the ledger records no body for them. Exact coords from nav_points.
    "PYRO - MINING BASE - RAB-ALPHA":       {"kind": "helio", "x": 7042819.0,  "y": 2993307.5},
    "PYRO - MINING BASE - RAB-KILO":        {"kind": "helio", "x": 14907563.8, "y": -9617700.1},
    "PYRO - MINING BASE - RAB-TUNG":        {"kind": "helio", "x": 8380397.4,  "y": 3880161.2},
    "PYRO - MINING BASE - RMB-EVEN":        {"kind": "helio", "x": 6869973.3,  "y": -10689136.5},
    "PYRO - MINING BASE - RMB-LAZO":        {"kind": "helio", "x": 7439603.7,  "y": -10249427.2},
    "PYRO - MINING BASE - RMB-NAIN":        {"kind": "helio", "x": 7883125.1,  "y": -8919159.3},
    "PYRO - MINING BASE - RMB-NIGH":        {"kind": "helio", "x": 5792164.1,  "y": -6347703.0},
    "PYRO - MINING BASE - RMB-ZARF":        {"kind": "helio", "x": 5030736.2,  "y": -6916005.1},
    "PYRO - MINING BASE - CLUSTER NBD-102": {"kind": "helio", "x": 16481986.1, "y": -12482403.2},
}

# Case-insensitive view of ANCHORS, built once. The ledger is hand-typed, so
# casing is not stable between snapshots.
_ANCHORS_CI = {k.upper(): v for k, v in ANCHORS.items()}


def anchor_for(location: str) -> dict | None:
    """Resolve a ledger location to its render anchor, ignoring case."""
    return _ANCHORS_CI.get(norm_ws(location).upper())


# Typo variants that are the SAME physical location, merged before counting.
# This is not cosmetic: percentages are per-location, so leaving the variants
# split computes three separate sample sets for one belt (and stacks three
# disks on one point). Merge at the source and the belt gets one honest figure.
# Fix these in the ledger and the aliases become no-ops.
LOCATION_ALIASES = {
    "NYX - GLACIEAM BELT - BELT": "NYX - GLACIEM BELT - BELT",   # GLACIEAM typo
    "NYX - GLACIEM BELT- BELT":   "NYX - GLACIEM BELT - BELT",   # missing space
}
_ALIASES_CI = {k.upper(): v for k, v in LOCATION_ALIASES.items()}


def canonical_location(location: str) -> str:
    """Fold known typo variants onto one canonical location string."""
    loc = norm_ws(location)
    return _ALIASES_CI.get(loc.upper(), loc)

SYSTEM_OF = {"STANTON": "stanton", "PYRO": "pyro", "NYX": "nyx"}


def main() -> None:
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["LEDGER"]

    # Fail loudly if the columns move again. Reading the wrong column produces a
    # valid-looking file with zero rows, which is far worse than a crash.
    hdr_loc = norm_ws(ws.cell(1, COL_LOCATION).value or "").lower()
    hdr_ore = norm_ws(ws.cell(1, COL_FOUND_ORE).value or "").lower()
    if hdr_loc != "location" or hdr_ore != "found ore":
        raise SystemExit(
            f"LEDGER header mismatch — expected col {COL_LOCATION}='Location' and "
            f"col {COL_FOUND_ORE}='Found Ore', got {hdr_loc!r} and {hdr_ore!r}.\n"
            "The workbook layout changed; update COL_LOCATION / COL_FOUND_ORE."
        )

    counts: dict[str, Counter] = defaultdict(Counter)
    for r in range(2, ws.max_row + 1):
        loc = ws.cell(r, COL_LOCATION).value
        ore = ws.cell(r, COL_FOUND_ORE).value
        if not loc or not ore:
            continue
        counts[canonical_location(loc)][str(ore).strip()] += 1

    all_ores = sorted({o for c in counts.values() for o in c})
    locations = []
    max_pct = 0.0
    unmapped = []

    for loc in sorted(counts):
        anchor = anchor_for(loc)
        if anchor is None:
            unmapped.append(loc)
            anchor = {"kind": "none"}
        system = SYSTEM_OF.get(loc.split(" - ", 1)[0], None)
        total = sum(counts[loc].values())
        ores = {ore: round(100 * n / total, 1) for ore, n in counts[loc].items()}
        max_pct = max(max_pct, *ores.values())
        # Strip the leading "SYSTEM - " for a tidier on-map label.
        label = re.sub(r"^[A-Z]+ - ", "", loc)
        locations.append({
            "system": system,
            "label": label,
            "anchor": anchor,
            "samples": total,
            "ores": ores,
        })

    payload = {
        "meta": {
            "ores": all_ores,
            "maxPct": round(max_pct, 1),
            "source": "solprovision-mining-tool.xlsx :: LEDGER (HEAT_MAP pivot)",
        },
        "locations": locations,
    }

    banner = (
        "// ═══════════════════════════════════════════════════════════════════\n"
        "//  ORE CONCENTRATION HEAT-MAP DATA  (auto-generated — do not edit)\n"
        "//  Source: tools/solprovision-mining-tool.xlsx (LEDGER / HEAT_MAP pivot)\n"
        "//  Regenerate: python tools/gen_ore_heatmap.py\n"
        "//\n"
        "//  Per location: % of that location's recorded finds that were each ore\n"
        "//  (matches the workbook's COUNTA-of-Found-Ore as %-of-row). `anchor`\n"
        "//  tells the renderer where to drop the heat disk in the live scene.\n"
        "// ═══════════════════════════════════════════════════════════════════\n\n"
    )
    OUT.write_text(
        banner + "export const ORE_HEATMAP = " + json.dumps(payload, indent=2) + ";\n",
        encoding="utf-8",
    )

    total_samples = sum(l["samples"] for l in locations)
    drawn = [l for l in locations if l["anchor"]["kind"] != "none"]
    drawn_samples = sum(l["samples"] for l in drawn)

    print(f"Wrote {OUT.relative_to(ROOT)}")
    print(f"  {len(locations)} locations, {len(all_ores)} ores, max {max_pct}%")
    # Coverage is the number that matters: an unanchored location still lands in
    # the file but is invisible on the map, so a silent anchor drift shows up
    # here as a coverage drop rather than as an error.
    print(f"  drawn: {len(drawn)}/{len(locations)} locations, "
          f"{drawn_samples}/{total_samples} samples "
          f"({100 * drawn_samples / total_samples:.1f}%)")
    if unmapped:
        print(f"  NOTE: {len(unmapped)} location(s) have no anchor (kept in data, NOT drawn):")
        for u in unmapped:
            n = next(l["samples"] for l in locations if l["label"] in u or u.endswith(l["label"]))
            print(f"   - {u}  ({n} samples)")


if __name__ == "__main__":
    main()
