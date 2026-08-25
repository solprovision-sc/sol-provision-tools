"""Membership applications from the public /join page.

Mirror image of shared/org_status.py: there the tools app writes and the portal
reads; here the PORTAL is the only writer (the public join form) and the tools
app reads for officer review. Separate database file on purpose — one writer per
file, so neither app ever needs a second write connection to a database the
other owns.

Fields mirror the Squarespace form this replaces, whose responses land in
"Sol Provision Membership Intake.xlsx". Column order and option lists were taken
from the live form at beyondtheversehq.com/join (captured 2026-08-25) so nothing
is lost in the migration.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "applications.db"

MIN_SQLITE = (3, 35, 0)
SCHEMA_VERSION = 1

# ── Form vocabulary ──────────────────────────────────────────────────────────
# Allowlists. Anything not in these is rejected rather than stored, so a crafted
# POST can't smuggle arbitrary text into a field the UI presents as a dropdown.
TIME_ZONES = (
    "US Eastern (ET)",
    "US Central (CT)",
    "US Mountain (MT)",
    "US Pacific (PT)",
    "Alaska / Hawaii",
    "Europe / UK",
    "Australia / Oceania",
    "Asia-Pacific",
    "Other International",
)

PLAY_WINDOWS = (
    "Weekday Evenings",
    "Weekday Daytime",
    "Weekends",
    "Late Night / Flexible",
    "Variable / Rotating",
)

# NOTE: the live Squarespace form reads "Acquisition Divsion" — a typo that has
# been collecting responses for months. Corrected here; the migration note in
# the officer view should mention it if old rows are ever imported.
DIVISIONS = (
    "Acquisition Division",
    "Combat Division",
    "Logistics Division",
    "Science Division",
    "Open to Placement",
)

AGE_CONFIRMATION = (
    "I confirm that I am at least 18 years old and understand that "
    "Sol Provision is an adult-only (18+) community."
)

# Officer review states. The spreadsheet's "Proposal" column held this, with
# every retained row set to "Accepted".
STATUSES = ("new", "reviewing", "accepted", "declined", "withdrawn")
DEFAULT_STATUS = "new"

# Generous but bounded. The longest real answer in the exported sheet was 2,549
# characters, so 8k leaves headroom without letting a bot post a novel.
MAX_LEN = {
    "rsi_username": 64,
    "discord_username": 64,
    "email": 254,
    "heard_about": 500,
    "referred_by": 120,
    "motivation": 8000,
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

_MIGRATIONS = [
    # 1 ─────────────────────────────────────────────────────────────────────
    """
    CREATE TABLE applications (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        submitted_at      TEXT NOT NULL,
        age_confirmed     INTEGER NOT NULL DEFAULT 0,
        rsi_username      TEXT NOT NULL,
        discord_username  TEXT NOT NULL,
        email             TEXT NOT NULL,
        time_zone         TEXT NOT NULL,
        play_window       TEXT,
        division_interest TEXT NOT NULL,
        heard_about       TEXT NOT NULL,
        motivation        TEXT NOT NULL,
        referred_by       TEXT,
        status            TEXT NOT NULL DEFAULT 'new',
        reviewed_by       TEXT,
        reviewed_at       TEXT,
        review_notes      TEXT,
        source_ip         TEXT,
        user_agent        TEXT
    );

    CREATE INDEX idx_applications_status    ON applications (status, submitted_at DESC);
    CREATE INDEX idx_applications_submitted ON applications (submitted_at DESC);
    CREATE INDEX idx_applications_discord   ON applications (discord_username);
    """,
]


class ValidationError(ValueError):
    """One or more submitted fields were unusable. `errors` maps field -> message."""

    def __init__(self, errors: dict):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def utc_now() -> str:
    """ISO-8601 UTC with an explicit offset, matching shared/org_status.py."""
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    return Path(os.environ.get("APPLICATIONS_DB") or DEFAULT_DB_PATH)


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    """The only place this database is opened."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required; "
            f"found {sqlite3.sqlite_version}"
        )
    path = db_path()
    if read_only:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        # Pragmas before any DDL — journal_mode is a no-op inside a transaction.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    # Public form writing while officers read: without busy_timeout, WAL fails
    # as an immediate "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or connect()
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"applications.db is at schema v{current}, newer than this code "
                f"(v{SCHEMA_VERSION}). Refusing to run."
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            log.info("applying applications migration %d", version)
            conn.executescript(_MIGRATIONS[version - 1])
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        return SCHEMA_VERSION
    finally:
        if own:
            conn.close()


def _clean(value, limit: int) -> str:
    """Trim, collapse interior whitespace runs that include newlines, and cap."""
    text = ("" if value is None else str(value)).strip()
    return text[:limit]


def validate(payload: dict) -> dict:
    """Validate a raw form payload into storable values.

    Raises ValidationError with a per-field map so the page can mark exactly
    what needs fixing rather than showing one generic message.
    """
    errors: dict[str, str] = {}

    rsi = _clean(payload.get("rsi_username"), MAX_LEN["rsi_username"])
    discord = _clean(payload.get("discord_username"), MAX_LEN["discord_username"])
    email = _clean(payload.get("email"), MAX_LEN["email"])
    heard = _clean(payload.get("heard_about"), MAX_LEN["heard_about"])
    motivation = _clean(payload.get("motivation"), MAX_LEN["motivation"])
    referred = _clean(payload.get("referred_by"), MAX_LEN["referred_by"])
    tz = _clean(payload.get("time_zone"), 64)
    window = _clean(payload.get("play_window"), 64)
    division = _clean(payload.get("division_interest"), 64)

    age_raw = payload.get("age_confirmed")
    age_confirmed = str(age_raw).lower() in ("1", "true", "on", "yes")

    if not age_confirmed:
        # Required here even though the Squarespace field was optional — this is
        # an adult-only community and the confirmation is the whole point.
        errors["age_confirmed"] = "You must confirm you are 18 or older."
    if not rsi:
        errors["rsi_username"] = "RSI username is required."
    if not discord:
        errors["discord_username"] = "Discord username is required."
    if not email:
        errors["email"] = "Email is required."
    elif not _EMAIL_RE.match(email):
        errors["email"] = "That doesn't look like a valid email address."
    if tz not in TIME_ZONES:
        errors["time_zone"] = "Choose your primary operating time zone."
    if window and window not in PLAY_WINDOWS:
        errors["play_window"] = "Choose a valid play window."
    if division not in DIVISIONS:
        errors["division_interest"] = "Choose a division interest."
    if not heard:
        errors["heard_about"] = "Let us know how you heard about us."
    if not motivation:
        errors["motivation"] = "Tell us what draws you to Sol Provision."

    if errors:
        raise ValidationError(errors)

    return {
        "age_confirmed": 1,
        "rsi_username": rsi,
        "discord_username": discord,
        "email": email,
        "time_zone": tz,
        "play_window": window or None,
        "division_interest": division,
        "heard_about": heard,
        "motivation": motivation,
        "referred_by": referred or None,
    }


def recent_duplicate(conn: sqlite3.Connection, discord_username: str,
                     within_hours: int = 24) -> bool:
    """Has this Discord user already applied recently?

    Cheap double-submit guard. Deliberately not a UNIQUE constraint: people
    legitimately reapply after being declined, and a hard constraint would turn
    that into a confusing error months later.
    """
    # Cutoff computed in Python, NOT with SQLite's datetime('now', ...):
    # that returns "YYYY-MM-DD HH:MM:SS" while submitted_at is ISO-8601 with a
    # "T" separator. Comparing the two as strings is wrong — "T" (0x54) sorts
    # above " " (0x20), so same-day rows compare greater regardless of time.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(within_hours))).isoformat()
    row = conn.execute(
        """SELECT 1 FROM applications
           WHERE discord_username = ? COLLATE NOCASE
             AND submitted_at >= ?
           LIMIT 1""",
        (discord_username, cutoff),
    ).fetchone()
    return row is not None


def create(conn: sqlite3.Connection, fields: dict, *,
           source_ip: str | None = None, user_agent: str | None = None) -> int:
    """Insert a validated application. Returns the new row id."""
    now = utc_now()
    cur = conn.execute(
        """INSERT INTO applications
               (submitted_at, age_confirmed, rsi_username, discord_username, email,
                time_zone, play_window, division_interest, heard_about, motivation,
                referred_by, status, source_ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, fields["age_confirmed"], fields["rsi_username"],
         fields["discord_username"], fields["email"], fields["time_zone"],
         fields["play_window"], fields["division_interest"], fields["heard_about"],
         fields["motivation"], fields["referred_by"], DEFAULT_STATUS,
         source_ip, (user_agent or "")[:300] or None),
    )
    conn.commit()
    return cur.lastrowid


def list_applications(conn: sqlite3.Connection, status: str | None = None,
                      limit: int = 100) -> list[dict]:
    """Applications for officer review, newest first."""
    if status:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        rows = conn.execute(
            "SELECT * FROM applications WHERE status = ? "
            "ORDER BY submitted_at DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM applications ORDER BY submitted_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def counts_by_status(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM applications GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
