#!/usr/bin/env python3
"""Import historical Squarespace membership responses into applications.db.

Source: "Sol Provision Membership Intake.xlsx" — the Google Sheet the old
Squarespace form fed. Run once to carry history across; safe to re-run, since
each row is keyed on legacy_key with ON CONFLICT DO NOTHING.

    python tools/import_applications_xlsx.py --dry-run
    python tools/import_applications_xlsx.py

Mapping notes (the interesting part):

  "Proposal"            -> status. Held the officer's decision, and every
                           retained row said "Accepted".
  "age requirement 18"  -> age_confirmed (1 if the confirmation text is present).
                           Blank on 4 of 38 rows; those import as 0 rather than
                           being rejected — refusing real history over a checkbox
                           the old form left optional would be worse.
  "Acquisition Divsion" -> "Acquisition Division" (typo in the source form).
  "Submitted On"        -> submitted_at, converted to ISO-8601 UTC. The sheet
                           stores naive local time; --source-tz controls what
                           that means (default America/New_York, the timezone
                           the org's other jobs run in).

Anything that fails validation is REPORTED and SKIPPED, not silently dropped —
a partial import you know about beats a clean-looking one that lost rows.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from shared import applications as A  # noqa: E402

DEFAULT_XLSX = REPO_ROOT / "Sol Provision Membership Intake.xlsx"

# Sheet header -> our field name. Headers are matched case-insensitively and
# with surrounding whitespace stripped, because spreadsheet headers drift.
COLUMN_MAP = {
    "submitted on": "submitted_at",
    "age requirement 18": "age_confirmed",
    "rsi username": "rsi_username",
    "discord username": "discord_username",
    "email": "email",
    "primary operating time zone": "time_zone",
    "typical play window": "play_window",
    "preferred division interest": "division_interest",
    "what draws you to sol provision": "motivation",
    "how did you hear about sol provision": "heard_about",
    "referred by": "referred_by",
    "proposal": "status",
}

# Values in the sheet that don't match our allowlists.
VALUE_FIXES = {
    "division_interest": {"Acquisition Divsion": "Acquisition Division"},
}

STATUS_MAP = {
    "accepted": "accepted",
    "declined": "declined",
    "rejected": "declined",
    "withdrawn": "withdrawn",
    "reviewing": "reviewing",
    "": A.DEFAULT_STATUS,
}


def parse_submitted(raw, tz: ZoneInfo) -> str:
    """Sheet timestamp -> ISO-8601 UTC."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unparseable timestamp: {text!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC")).isoformat()


def read_rows(xlsx: Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb.worksheets[0]
    raw = list(ws.iter_rows(values_only=True))
    if not raw:
        return []

    headers = [str(h).strip().lower() if h is not None else "" for h in raw[0]]
    unknown = [h for h in headers if h and h not in COLUMN_MAP]
    if unknown:
        print(f"  note: ignoring unmapped column(s): {unknown}")

    out = []
    for r in raw[1:]:
        if not any(v is not None and str(v).strip() for v in r):
            continue
        row = {}
        for i, h in enumerate(headers):
            field = COLUMN_MAP.get(h)
            if field and i < len(r):
                row[field] = r[i]
        out.append(row)
    return out


def build(row: dict, tz: ZoneInfo) -> tuple[str, str, dict, str]:
    """-> (legacy_key, submitted_at_iso, validated_fields, status)"""
    submitted_at = parse_submitted(row.get("submitted_at"), tz)

    payload = {
        "rsi_username": row.get("rsi_username"),
        "discord_username": row.get("discord_username"),
        "email": row.get("email"),
        "time_zone": row.get("time_zone"),
        "play_window": row.get("play_window"),
        "division_interest": row.get("division_interest"),
        "heard_about": row.get("heard_about"),
        "motivation": row.get("motivation"),
        "referred_by": row.get("referred_by"),
        # validate() requires this; historical age state is restored below.
        "age_confirmed": True,
    }
    for field, fixes in VALUE_FIXES.items():
        current = (str(payload.get(field) or "")).strip()
        if current in fixes:
            payload[field] = fixes[current]

    fields = A.validate(payload)

    # Restore what the sheet actually recorded, now that validation has passed.
    age_raw = (str(row.get("age_confirmed") or "")).strip()
    fields["age_confirmed"] = 1 if age_raw else 0

    status = STATUS_MAP.get((str(row.get("status") or "")).strip().lower())
    if status is None:
        status = A.DEFAULT_STATUS

    legacy_key = f"xlsx:{submitted_at}:{fields['discord_username'].lower()}"
    return legacy_key, submitted_at, fields, status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--source-tz", default="America/New_York",
                    help="timezone the sheet's naive timestamps are in")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and validate, write nothing")
    args = ap.parse_args()

    if not args.xlsx.exists():
        print(f"ERROR: {args.xlsx} not found")
        return 1

    tz = ZoneInfo(args.source_tz)
    print(f"source : {args.xlsx.name}")
    print(f"target : {A.db_path()}")
    print(f"tz     : {args.source_tz} -> UTC")
    print(f"mode   : {'DRY RUN' if args.dry_run else 'WRITE'}\n")

    rows = read_rows(args.xlsx)
    print(f"  {len(rows)} data row(s) found\n")

    built, skipped = [], []
    for n, row in enumerate(rows, start=2):          # +2: header is row 1
        try:
            built.append(build(row, tz))
        except A.ValidationError as exc:
            skipped.append((n, exc.errors))
        except Exception as exc:
            skipped.append((n, {"row": str(exc)}))

    inserted = duplicate = 0
    if not args.dry_run and built:
        conn = A.connect()
        A.migrate(conn)
        try:
            for legacy_key, submitted_at, fields, status in built:
                if A.import_legacy(conn, legacy_key=legacy_key,
                                   submitted_at=submitted_at, fields=fields,
                                   status=status):
                    inserted += 1
                else:
                    duplicate += 1
            conn.commit()
        finally:
            conn.close()

    print(f"  parsed OK : {len(built)}")
    print(f"  skipped   : {len(skipped)}")
    if not args.dry_run:
        print(f"  inserted  : {inserted}")
        print(f"  already in: {duplicate}")

    if skipped:
        print("\n  SKIPPED ROWS (sheet row number -> problem):")
        for n, errs in skipped:
            print(f"    row {n}: {errs}")

    if built:
        print("\n  status breakdown:")
        counts: dict[str, int] = {}
        for *_, status in built:
            counts[status] = counts.get(status, 0) + 1
        for k, v in sorted(counts.items()):
            print(f"    {k:12s} {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
