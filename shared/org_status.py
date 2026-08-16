"""Org status shared between the tools app and the portal.

Officers set division readiness in the HQ dashboard (tools app, the only WRITER);
the portal reads it READ-ONLY to render the Division Readiness Matrix. The two
apps never call each other — this database file is the entire interface.

Path resolution and the connection factory live here on purpose. When two
processes each resolve a database path their own way, setting the env var on one
side silently points them at different files, and the failure is invisible.
Import from here; never call sqlite3.connect on this database directly.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "org_status.db"

# ON CONFLICT ... DO UPDATE, used by set_readiness.
MIN_SQLITE = (3, 35, 0)

SCHEMA_VERSION = 1

# ── Domain constants ─────────────────────────────────────────────────────────
# Four fixed divisions. `name` matches the `division` values in the SPARQy
# roster (discord_members.division) so the two can be cross-referenced; `code`
# matches the labels the existing Squarespace portal shows.
DIVISIONS = (
    {"code": "ACQ", "name": "Acquisitions", "sort_order": 1},
    {"code": "CMB", "name": "Combat",       "sort_order": 2},
    {"code": "LOG", "name": "Logistics",    "sort_order": 3},
    {"code": "SCI", "name": "Science",      "sort_order": 4},
)
DIVISION_CODES = tuple(d["code"] for d in DIVISIONS)

# Legend text carried over verbatim from the existing portal.
STATUSES = (
    {"code": "operational",     "label": "Operational",
     "description": "Division fully mission capable."},
    {"code": "heightened",      "label": "Heightened",
     "description": "Division operational with reduced efficiency or elevated "
                    "preparation requirements."},
    {"code": "critical",        "label": "Critical",
     "description": "Division capability significantly degraded. Immediate "
                    "corrective action required."},
    {"code": "reconstitution",  "label": "Reconstitution",
     "description": "Division non-operational or rebuilding prior to future "
                    "deployment."},
)
STATUS_CODES = tuple(s["code"] for s in STATUSES)

DEFAULT_STATUS = "reconstitution"

# Current values as displayed on the Squarespace portal when this was built
# (captured 2026-08-14). Seeded once by migration 1 so the new portal renders
# real posture from day one rather than placeholder rows.
_SEED = {
    "ACQ": ("operational", "Shubin reputation"),
    "CMB": ("heightened",  "Fleet Doctrine Adherence"),
    "LOG": ("operational", "Readiness for Org Night"),
    "SCI": ("operational", "Readiness for Org Night - 3 medics"),
}

_MIGRATIONS = [
    # 1 ─────────────────────────────────────────────────────────────────────
    """
    CREATE TABLE division_readiness (
        division_code   TEXT PRIMARY KEY,
        status          TEXT NOT NULL,
        posture         TEXT NOT NULL DEFAULT '',
        updated_by      TEXT,
        updated_by_name TEXT,
        updated_at      TEXT NOT NULL
    );

    -- Append-only. Answers "who set Combat to Critical, and when".
    CREATE TABLE division_readiness_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        division_code   TEXT NOT NULL,
        status          TEXT NOT NULL,
        posture         TEXT NOT NULL DEFAULT '',
        updated_by      TEXT,
        updated_by_name TEXT,
        updated_at      TEXT NOT NULL
    );

    CREATE INDEX idx_readiness_log_division
        ON division_readiness_log (division_code, updated_at DESC);
    """,
]


def utc_now() -> str:
    """ISO-8601 UTC with an explicit offset. Never CURRENT_TIMESTAMP — that
    yields 'YYYY-MM-DD HH:MM:SS', which sorts and parses differently from the
    ISO values written everywhere else."""
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    """ORG_STATUS_DB wins, else a path anchored to this file.

    Anchored, never a bare relative name: a relative default resolves against
    the working directory and silently creates an empty database wherever the
    process happened to start.
    """
    return Path(os.environ.get("ORG_STATUS_DB") or DEFAULT_DB_PATH)


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    """The only place this database is opened.

    Readers pass read_only=True and get it enforced at the connection level.
    Never falls back to a writable connection — that would defeat the point.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required for upsert "
            f"idempotency; found {sqlite3.sqlite_version}"
        )
    path = db_path()
    if read_only:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        # Pragmas BEFORE any DDL: executescript commits and opens a
        # transaction, and journal_mode is a no-op inside one.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    # The single most important pragma here: the tools app writes while the
    # portal reads, and WAL without busy_timeout fails as an immediate
    # "database is locked" with nothing to fall back on.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection | None = None) -> int:
    """Apply pending migrations. Returns the resulting schema version.

    Tracked with PRAGMA user_version rather than CREATE TABLE IF NOT EXISTS, so
    an older database can never sit at an old shape and fail at query time with
    no signal.
    """
    own = conn is None
    conn = conn or connect()
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"org_status.db is at schema v{current}, newer than this code "
                f"(v{SCHEMA_VERSION}). Refusing to run."
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            log.info("applying org_status migration %d", version)
            conn.executescript(_MIGRATIONS[version - 1])
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
            if version == 1:
                _seed_initial(conn)
        return SCHEMA_VERSION
    finally:
        if own:
            conn.close()


def _seed_initial(conn: sqlite3.Connection) -> None:
    """Seed the four divisions with the posture the Squarespace portal showed."""
    now = utc_now()
    rows = [
        (code, *_SEED.get(code, (DEFAULT_STATUS, "")), None,
         "seed (imported from Squarespace portal)", now)
        for code in DIVISION_CODES
    ]
    conn.executemany(
        """INSERT INTO division_readiness
               (division_code, status, posture, updated_by, updated_by_name, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    log.info("seeded %d divisions", len(rows))


def get_readiness(conn: sqlite3.Connection) -> list[dict]:
    """Current readiness for all four divisions, in display order.

    Divisions with no row yet come back at DEFAULT_STATUS rather than being
    omitted, so the portal always renders a complete matrix.
    """
    stored = {
        r["division_code"]: r
        for r in conn.execute("SELECT * FROM division_readiness")
    }
    out = []
    for d in sorted(DIVISIONS, key=lambda x: x["sort_order"]):
        row = stored.get(d["code"])
        out.append({
            "code": d["code"],
            "name": d["name"],
            "status": row["status"] if row else DEFAULT_STATUS,
            "posture": (row["posture"] if row else "") or "",
            "updated_by": row["updated_by"] if row else None,
            "updated_by_name": row["updated_by_name"] if row else None,
            "updated_at": row["updated_at"] if row else None,
        })
    return out


def set_readiness(conn: sqlite3.Connection, division_code: str, status: str,
                  posture: str, actor_id: str | None,
                  actor_name: str | None) -> dict:
    """Write one division's readiness and append to the change log.

    Raises ValueError on an unknown division or status — validated against the
    allowlists above rather than trusted from the request body.
    """
    if division_code not in DIVISION_CODES:
        raise ValueError(f"unknown division: {division_code!r}")
    if status not in STATUS_CODES:
        raise ValueError(f"unknown status: {status!r}")

    posture = (posture or "").strip()
    now = utc_now()

    # BEGIN IMMEDIATE: the upsert and the log append are one unit, and a reader
    # must never observe the current row updated without its log entry.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Bare excluded.* rather than COALESCE: this is an officer submitting a
        # form, so they are authoritative — clearing the posture box must
        # actually clear it, not silently keep the old text.
        conn.execute(
            """INSERT INTO division_readiness
                   (division_code, status, posture, updated_by, updated_by_name, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(division_code) DO UPDATE SET
                   status          = excluded.status,
                   posture         = excluded.posture,
                   updated_by      = excluded.updated_by,
                   updated_by_name = excluded.updated_by_name,
                   updated_at      = excluded.updated_at""",
            (division_code, status, posture, actor_id, actor_name, now),
        )
        conn.execute(
            """INSERT INTO division_readiness_log
                   (division_code, status, posture, updated_by, updated_by_name, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (division_code, status, posture, actor_id, actor_name, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "code": division_code,
        "status": status,
        "posture": posture,
        "updated_by": actor_id,
        "updated_by_name": actor_name,
        "updated_at": now,
    }


def recent_changes(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Most recent readiness changes across all divisions, newest first."""
    rows = conn.execute(
        """SELECT division_code, status, posture, updated_by_name, updated_at
           FROM division_readiness_log
           ORDER BY updated_at DESC, id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
