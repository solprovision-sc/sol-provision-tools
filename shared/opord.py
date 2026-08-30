"""Operation Orders (OpOrds) — storage and document shape.

Written by the tools app (the officer-only editor in HQ); the portal will read
it read-only for the Mission Board, same direction as shared/org_status.py.

STORAGE MODEL — hybrid, deliberately:

  Real columns   the things we list, sort, filter and schedule on: title,
                 mission date, commander, status, AO, muster.
  JSON `body`    the nested document — units, execution steps, schedule rows,
                 support items. Always loaded whole, edited whole, rendered
                 whole, so normalising it into six tables would buy query power
                 at the cost of a lot of join-and-reorder code.

The shape of `body` is defined in exactly one place: `normalize_body()`. Every
read and every write goes through it, so a stored document can never drift from
what the code expects, and adding a field is a single edit plus a bump of
BODY_VERSION. Do not hand-build body dicts elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "opord.db"

MIN_SQLITE = (3, 35, 0)
SCHEMA_VERSION = 1

# Bumped when the JSON body shape changes in a way normalize_body() can't
# silently absorb. Stored per row so a future migration can find old documents.
BODY_VERSION = 1

# ── Vocabulary ───────────────────────────────────────────────────────────────
# draft    — being written, never shown outside HQ
# posted   — the board OpOrd. Only one at a time; posting demotes the previous.
# archived — retired, kept for duplication and history
STATUSES = ("draft", "posted", "archived")
DEFAULT_STATUS = "draft"

SIGNAL_TYPES = ("Discord", "TeamSpeak3", "Other")
DEFAULT_SIGNAL = "Discord"

STEP_TYPES = ("unit_tasks", "custom")

# A posted OpOrd stays current until this long past muster, then the Mission
# Board falls back to its "Next OpOrd — In Work" placeholder.
CURRENT_FOR = timedelta(hours=48)

DEFAULT_MUSTER_TZ = "America/Chicago"
DEFAULT_MUSTER_TIME = "20:30"

# Short labels for display. Splitting the IANA name yields "Chicago", which is
# not what anyone calls the timezone.
TZ_LABELS = {
    "America/Chicago": "CT",
    "America/New_York": "ET",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
    "UTC": "UTC",
}


def tz_label(name: str) -> str:
    """Short label for display, falling back to the city for anything unmapped."""
    return TZ_LABELS.get(name or "", (name or "").split("/")[-1].replace("_", " "))

SIGNAL_NOTE = ("NOTE: Clean communications is essential to ensuring a successful "
               "mission. Monitor comms at all times and maintain comm discipline.")

MAX_LEN = {"title": 120, "commander": 80, "area_of_operation": 400, "text": 8000}

_MIGRATIONS = [
    # 1 ─────────────────────────────────────────────────────────────────────
    """
    CREATE TABLE opords (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        mission_date    TEXT,                       -- YYYY-MM-DD, local
        muster_time     TEXT,                       -- HH:MM 24h, local
        muster_tz       TEXT NOT NULL DEFAULT 'America/Chicago',
        muster_at_utc   TEXT,                       -- derived; drives the 48h rule
        commander       TEXT,
        area_of_operation TEXT,
        status          TEXT NOT NULL DEFAULT 'draft',
        body_version    INTEGER NOT NULL DEFAULT 1,
        body            TEXT NOT NULL,              -- JSON, shape owned by normalize_body()
        created_at      TEXT NOT NULL,
        created_by      TEXT,
        updated_at      TEXT NOT NULL,
        updated_by      TEXT,
        posted_at       TEXT,
        duplicated_from INTEGER REFERENCES opords(id) ON DELETE SET NULL
    );

    CREATE INDEX idx_opords_status  ON opords (status, muster_at_utc DESC);
    CREATE INDEX idx_opords_mission ON opords (mission_date DESC);
    """,
]


class ValidationError(ValueError):
    def __init__(self, errors: dict):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


# ── Plumbing ─────────────────────────────────────────────────────────────────
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    return Path(os.environ.get("OPORD_DB") or DEFAULT_DB_PATH)


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    """The only place this database is opened."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required; "
            f"found {sqlite3.sqlite_version}")
    path = db_path()
    if read_only:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        # Pragmas before DDL — journal_mode is a no-op inside a transaction.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
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
                f"opord.db is at schema v{current}, newer than this code "
                f"(v{SCHEMA_VERSION}). Refusing to run.")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            log.info("applying opord migration %d", version)
            conn.executescript(_MIGRATIONS[version - 1])
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        return SCHEMA_VERSION
    finally:
        if own:
            conn.close()


# ── Document shape ───────────────────────────────────────────────────────────
def _sid() -> str:
    """Short stable id for units/steps/items. Units are referenced by execution
    steps, so the id has to survive renaming and reordering."""
    return uuid.uuid4().hex[:8]


def _text(value, limit: int = MAX_LEN["text"]) -> str:
    return ("" if value is None else str(value)).strip()[:limit]


def _list(value) -> list:
    return value if isinstance(value, list) else []


def blank_body() -> dict:
    """A new, empty OpOrd document with the defaults the template calls for."""
    return normalize_body({
        "execution": {
            "time_schedule": [
                {"time": "8:30PM CST", "text": "Be in lobby to party-up and conduct pre-briefing"},
                {"time": "8:45PM CST", "text": "Mission Commander to pull party into server"},
            ]
        },
        "command_signal": {"signal_type": DEFAULT_SIGNAL},
    })


def normalize_body(raw) -> dict:
    """Coerce anything into the canonical body shape.

    THE definition of an OpOrd document. Every read and write passes through
    here, so unknown keys are dropped, missing keys get defaults, and stored
    JSON can never drift from what the templates expect. Add a field here and
    nowhere else.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    org = raw.get("task_organization") or {}
    units = []
    for u in _list(org.get("units")):
        if not isinstance(u, dict):
            continue
        units.append({
            "id": _text(u.get("id"), 32) or _sid(),
            "name": _text(u.get("name"), 80),
            "crew_size": _text(u.get("crew_size"), 40),
            "ships": _text(u.get("ships"), 200),
            "led_by": _text(u.get("led_by"), 80),
        })
    known_units = {u["id"] for u in units}

    sit = raw.get("situation") or {}
    exe = raw.get("execution") or {}

    steps = []
    for st in _list(exe.get("steps")):
        if not isinstance(st, dict):
            continue
        kind = st.get("type") if st.get("type") in STEP_TYPES else "custom"
        step = {"id": _text(st.get("id"), 32) or _sid(), "type": kind}
        if kind == "unit_tasks":
            unit_id = _text(st.get("unit_id"), 32)
            # A step pointing at a deleted unit would render as a blank heading.
            # Keep the step, drop the dangling reference; the editor shows it as
            # unassigned so the author can repoint or remove it.
            step["unit_id"] = unit_id if unit_id in known_units else ""
            step["tasks"] = [_text(t) for t in _list(st.get("tasks")) if _text(t)]
        else:
            step["title"] = _text(st.get("title"), 120)
            step["body"] = _text(st.get("body"))
        steps.append(step)

    schedule = []
    for row in _list(exe.get("time_schedule")):
        if not isinstance(row, dict):
            continue
        when, what = _text(row.get("time"), 40), _text(row.get("text"), 300)
        if when or what:
            schedule.append({"time": when, "text": what})

    sup = raw.get("service_support") or {}
    items = []
    for it in _list(sup.get("items")):
        if not isinstance(it, dict):
            continue
        title, detail = _text(it.get("title"), 120), _text(it.get("detail"))
        if title or detail:
            items.append({"id": _text(it.get("id"), 32) or _sid(),
                          "title": title, "detail": detail})

    cs = raw.get("command_signal") or {}
    notes = []
    for n in _list(cs.get("notes")):
        if not isinstance(n, dict):
            continue
        title, detail = _text(n.get("title"), 120), _text(n.get("detail"))
        if title or detail:
            notes.append({"id": _text(n.get("id"), 32) or _sid(),
                          "title": title, "detail": detail})

    signal_type = cs.get("signal_type")
    if signal_type not in SIGNAL_TYPES:
        signal_type = DEFAULT_SIGNAL

    return {
        # Area of operation lives in the `area_of_operation` COLUMN, not here:
        # it's listed and sorted on, the editor writes it as a header field, and
        # a second copy in the body only invited the two to disagree.
        "task_organization": {"units": units},
        "situation": {
            "enemy_forces": _text(sit.get("enemy_forces")),
            "friendly_forces": _text(sit.get("friendly_forces")),
            "obstacles": _text(sit.get("obstacles")),
        },
        "mission": _text(raw.get("mission")),
        "execution": {
            "intent": _text(exe.get("intent")),
            "steps": steps,
            "time_schedule": schedule,
        },
        "service_support": {
            "medical": _text(sup.get("medical")),
            "personnel": _text(sup.get("personnel")),
            "items": items,
        },
        "command_signal": {
            "command": _text(cs.get("command"), 300),
            "signal_type": signal_type,
            "signal_detail": _text(cs.get("signal_detail"), 300),
            "notes": notes,
        },
    }


def section_letters(body: dict) -> dict:
    """Letter labels for Execution, computed rather than stored.

    A is always Intent and the Time Schedule is always last, so inserting or
    deleting a step in the middle must not leave stale letters in the document.
    """
    letters = {"intent": "A"}
    idx = 1
    for step in body["execution"]["steps"]:
        letters[step["id"]] = chr(ord("A") + idx)
        idx += 1
    letters["time_schedule"] = chr(ord("A") + idx)
    return letters


# ── Muster / currency ────────────────────────────────────────────────────────
def compute_muster_utc(mission_date: str, muster_time: str, tz_name: str) -> str | None:
    """Local mission date + muster time -> ISO-8601 UTC.

    Stored alongside the local parts because the 48-hour currency rule needs a
    real instant; comparing "8:30PM CST" strings would not survive a timezone
    change or a date rollover.
    """
    if not mission_date or not muster_time:
        return None
    try:
        tz = ZoneInfo(tz_name or DEFAULT_MUSTER_TZ)
        naive = datetime.strptime(f"{mission_date} {muster_time}", "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).isoformat()
    except (ValueError, KeyError):
        return None


def is_current(row, now: datetime | None = None) -> bool:
    """Is this the OpOrd the Mission Board should be showing?

    Posted, and not yet 48 hours past muster. Computed rather than stored, so
    no scheduled job is needed to expire it — the board simply stops matching.
    """
    if row["status"] != "posted":
        return False
    muster = row["muster_at_utc"]
    if not muster:
        return True          # posted but undated: show it until dated or unposted
    try:
        when = datetime.fromisoformat(muster)
    except ValueError:
        return True
    return (now or datetime.now(timezone.utc)) < when + CURRENT_FOR


# ── CRUD ─────────────────────────────────────────────────────────────────────
def _row_to_dict(row, *, with_body: bool = True) -> dict:
    d = dict(row)
    if with_body:
        d["body"] = normalize_body(d.get("body"))
    else:
        d.pop("body", None)
    d["is_current"] = is_current(row)
    return d


def validate_header(fields: dict) -> dict:
    """Validate the scalar columns. Body shape is handled by normalize_body."""
    errors = {}
    title = _text(fields.get("title"), MAX_LEN["title"])
    if not title:
        errors["title"] = "Title is required."

    status = fields.get("status") or DEFAULT_STATUS
    if status not in STATUSES:
        errors["status"] = f"Unknown status: {status}"

    mission_date = _text(fields.get("mission_date"), 10)
    if mission_date:
        try:
            datetime.strptime(mission_date, "%Y-%m-%d")
        except ValueError:
            errors["mission_date"] = "Use YYYY-MM-DD."

    muster_time = _text(fields.get("muster_time"), 5)
    if muster_time:
        try:
            datetime.strptime(muster_time, "%H:%M")
        except ValueError:
            errors["muster_time"] = "Use HH:MM (24-hour)."

    tz_name = _text(fields.get("muster_tz"), 64) or DEFAULT_MUSTER_TZ
    try:
        ZoneInfo(tz_name)
    except Exception:
        errors["muster_tz"] = f"Unknown timezone: {tz_name}"

    if errors:
        raise ValidationError(errors)

    return {
        "title": title,
        "mission_date": mission_date or None,
        "muster_time": muster_time or None,
        "muster_tz": tz_name,
        "muster_at_utc": compute_muster_utc(mission_date, muster_time, tz_name),
        "commander": _text(fields.get("commander"), MAX_LEN["commander"]) or None,
        "area_of_operation": _text(fields.get("area_of_operation"),
                                   MAX_LEN["area_of_operation"]) or None,
        "status": status,
    }


def create(conn, fields: dict, body, actor: str | None = None,
           duplicated_from: int | None = None) -> int:
    hdr = validate_header(fields)
    now = utc_now()
    cur = conn.execute(
        """INSERT INTO opords
             (title, mission_date, muster_time, muster_tz, muster_at_utc, commander,
              area_of_operation, status, body_version, body,
              created_at, created_by, updated_at, updated_by, duplicated_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (hdr["title"], hdr["mission_date"], hdr["muster_time"], hdr["muster_tz"],
         hdr["muster_at_utc"], hdr["commander"], hdr["area_of_operation"],
         hdr["status"], BODY_VERSION, json.dumps(normalize_body(body)),
         now, actor, now, actor, duplicated_from))
    conn.commit()
    return cur.lastrowid


def update(conn, opord_id: int, fields: dict, body, actor: str | None = None) -> bool:
    hdr = validate_header(fields)
    cur = conn.execute(
        """UPDATE opords SET
             title=?, mission_date=?, muster_time=?, muster_tz=?, muster_at_utc=?,
             commander=?, area_of_operation=?, status=?, body_version=?, body=?,
             updated_at=?, updated_by=?
           WHERE id=?""",
        (hdr["title"], hdr["mission_date"], hdr["muster_time"], hdr["muster_tz"],
         hdr["muster_at_utc"], hdr["commander"], hdr["area_of_operation"],
         hdr["status"], BODY_VERSION, json.dumps(normalize_body(body)),
         utc_now(), actor, opord_id))
    conn.commit()
    return cur.rowcount > 0


def get(conn, opord_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM opords WHERE id=?", (opord_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_opords(conn, limit: int = 100) -> list[dict]:
    """Newest mission first, undated last. Bodies omitted — the HQ table only
    needs the header, and 100 full documents would be a lot of JSON."""
    rows = conn.execute(
        """SELECT * FROM opords
           ORDER BY (mission_date IS NULL), mission_date DESC, updated_at DESC
           LIMIT ?""", (limit,)).fetchall()
    return [_row_to_dict(r, with_body=False) for r in rows]


def current_opord(conn) -> dict | None:
    """The one the Mission Board should show, or None for the placeholder."""
    rows = conn.execute(
        "SELECT * FROM opords WHERE status='posted' "
        "ORDER BY muster_at_utc DESC").fetchall()
    for row in rows:
        if is_current(row):
            return _row_to_dict(row)
    return None


def post(conn, opord_id: int, actor: str | None = None) -> bool:
    """Make this the board OpOrd, demoting whatever was posted before.

    One at a time by construction — otherwise the Mission Board would need a
    tie-break rule and officers would have no way to see which one wins.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE opords SET status='archived', updated_at=?, updated_by=? "
            "WHERE status='posted' AND id<>?", (utc_now(), actor, opord_id))
        cur = conn.execute(
            "UPDATE opords SET status='posted', posted_at=?, updated_at=?, "
            "updated_by=? WHERE id=?",
            (utc_now(), utc_now(), actor, opord_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise


def duplicate(conn, opord_id: int, actor: str | None = None,
              new_title: str | None = None) -> int | None:
    """Copy an OpOrd as a fresh draft — the reuse mechanism for a repeat op.

    Date and muster are cleared on purpose: a duplicate is a new run, and
    carrying the old date over is how you end up with two OpOrds claiming the
    same night.
    """
    src = conn.execute("SELECT * FROM opords WHERE id=?", (opord_id,)).fetchone()
    if not src:
        return None
    fields = {
        "title": new_title or f"{src['title']} (copy)",
        "mission_date": None,
        "muster_time": src["muster_time"],
        "muster_tz": src["muster_tz"],
        "commander": src["commander"],
        "area_of_operation": src["area_of_operation"],
        "status": "draft",
    }
    return create(conn, fields, src["body"], actor=actor, duplicated_from=opord_id)


def delete(conn, opord_id: int) -> bool:
    cur = conn.execute("DELETE FROM opords WHERE id=?", (opord_id,))
    conn.commit()
    return cur.rowcount > 0
