# ══════════════════════════════════════════════════════════════════════
# IMPORTS
# ══════════════════════════════════════════════════════════════════════
import os, json, argparse, sqlite3, re, uuid
from pathlib import Path
from flask import Flask, jsonify, request, render_template, session, redirect, send_from_directory
import requests
import time
from datetime import timedelta, datetime, timezone
from functools import wraps
import sys

# Code shared with the portal app lives under shared/. The two are separate
# processes with separate venvs, so each puts the repo root on sys.path rather
# than relying on an installed package. org_status owns the ONE definition of
# where org_status.db lives — if each app resolved that path its own way, an
# env var set on one side would silently point them at different files.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from shared import applications, opord, org_status


# ══════════════════════════════════════════════════════════════════════
# APP SETUP
# ══════════════════════════════════════════════════════════════════════
# Install: pip install firebase-admin --break-system-packages
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth

# Create flask app
app = Flask(__name__, template_folder="templates", static_folder="static")
from officer_db import officer_db
app.register_blueprint(officer_db)

DB_PATH = os.environ.get("DATAFORGE_DB", "../../shared/data/dataforge.db")
app.config['DB_PATH'] = DB_PATH  # consumed by the officer_db blueprint
PATCH = None

# Detect environment. Three cases:
#   - Linux, this app installed under /var/www/sol-provision-tools-dev → 'dev'
#   - Linux otherwise                                                   → 'prod'
#   - Windows (local dev)                                               → 'local' (uses dev Firebase project)
# NOTE: judge this process by ITS OWN install path, not by whether the dev
# directory exists anywhere on the box. Dev and prod are co-located on the
# same VPS, so os.path.exists('/var/www/sol-provision-tools-dev') is True for
# the prod process too — which previously made prod wrongly apply the dev-only
# rank-4+ gate and locked everyone out.
is_local = os.name == 'nt'
is_dev   = (not is_local) and str(Path(__file__).resolve()).startswith('/var/www/sol-provision-tools-dev')
app.config['IS_DEV'] = is_dev  # consumed by the officer_db blueprint's auth gate

# The dev deployment is restricted to ranks 4+; everyone else sees this.
DEV_AREA_DENIED = 'You are not authorized to access the Sol Provision Development area'

# ✅ SESSION CONFIG - GOES HERE (before any routes)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-prod')
# SESSION_COOKIE_SECURE requires HTTPS — locally we serve http://localhost,
# so disable secure-only there or the session cookie is never set and the
# login flow silently fails with a 401 on every authed request.
app.config['SESSION_COOKIE_SECURE'] = not is_local
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)


# Shared brand layer (fonts + design tokens), lives OUTSIDE this app's static
# folder so the portal app can consume the exact same files. Served at /brand/…
# rather than /static/… precisely because it is not owned by this app.
BRAND_DIR = Path(__file__).resolve().parent.parent / 'shared' / 'brand'


@app.route('/brand/<path:filename>')
def brand_static(filename):
    """Serve the shared brand bundle (brand.css + the woff2 faces).

    nginx can shortcut this with its own /brand/ location in production; this
    route is what makes it work locally and keeps the apps self-contained.
    """
    return send_from_directory(BRAND_DIR, filename)


# Sibling portal site, linked from the tools header. Points at the matching
# environment so tools-dev doesn't send officers into the production portal.
PORTAL_URL = os.environ.get(
    'PORTAL_URL',
    'https://portal-dev.solprovision.com' if is_dev
    else 'https://portal.solprovision.com',
).rstrip('/')


@app.context_processor
def inject_portal_url():
    return {"portal_url": PORTAL_URL}


@app.context_processor
def inject_asset_version():
    """Cache-busting stamp for static assets referenced in templates.

    theme.css / common.js are served at a fixed URL, so when we ship a change
    (e.g. the brand refresh) returning browsers keep the stale cached copy and
    render new markup against old tokens — mismatched colors that only clear in
    incognito. Templates append ?v={{ asset_v('css/theme.css') }} so the URL
    changes whenever the file's mtime does, forcing a fresh fetch on the next
    load. mtime is cheap and updates on every deploy that rewrites the file.

    'brand/…' paths resolve against BRAND_DIR instead of the static folder —
    without that, a token change in the shared brand.css would ship with a
    stale ?v= and reintroduce exactly the stale-cache mismatch described above.
    """
    def asset_v(rel_path):
        if rel_path.startswith('brand/'):
            base, rel = BRAND_DIR, rel_path[len('brand/'):]
        else:
            base, rel = app.static_folder, rel_path
        try:
            return int(os.path.getmtime(os.path.join(base, rel)))
        except OSError:
            return ""
    return {"asset_v": asset_v}

# Initialize Firebase Admin SDK (for token verification).
# FIREBASE_SERVICE_ACCOUNT / FIREBASE_DB_URL env vars override the resolved
# defaults so ops can swap credentials without code changes.
if is_local:
    # Windows: service-account JSON sits next to server.py in app/.
    default_cred_path = str(Path(__file__).resolve().parent / 'firebase-service-account.json')
    default_db_url    = 'https://sp-ledger-dev-default-rtdb.firebaseio.com'
elif is_dev:
    default_cred_path = '/var/www/sol-provision-tools-dev/app/firebase-service-account.json'
    default_db_url    = 'https://sp-ledger-dev-default-rtdb.firebaseio.com'
else:
    default_cred_path = '/var/www/sol-provision-tools/app/firebase-service-account.json'
    default_db_url    = 'https://sp-ledger-default-rtdb.firebaseio.com'

cred   = credentials.Certificate(os.environ.get('FIREBASE_SERVICE_ACCOUNT', default_cred_path))
db_url = os.environ.get('FIREBASE_DB_URL', default_db_url)

firebase_admin.initialize_app(cred, {
    'databaseURL': db_url
})

def require_org_member(f):
    """Decorator to verify user is still an org member before accessing endpoint"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for discord_id in session
        discord_id = session.get('discord_id')
        if not discord_id:
            return jsonify({'error': 'Not authenticated'}), 401
        
        # Check if still in org using the most recent snapshot
        user_db = get_user_db()
        cursor = user_db.cursor()
        
        # Query latest snapshot for this user (matches auth verification pattern)
        cursor.execute('''
            SELECT user_id 
            FROM discord_members 
            WHERE user_id = ?
            ORDER BY snapshot_date DESC
            LIMIT 1
        ''', (discord_id,))
        
        member = cursor.fetchone()
        user_db.close()
        
        if not member:
            # No longer in org (or never was) - clear session
            session.clear()
            return jsonify({'error': 'No longer an org member'}), 403
        
        return f(*args, **kwargs)
    return decorated_function
    
def rank_int(rank):
    """Coerce a rank value to int. Rank may be stored as int or string
    depending on the snapshot import; treat missing/unparseable as 0 so we
    never grant access by accident if the column drifts."""
    try:
        return int(rank) if rank is not None else 0
    except (TypeError, ValueError):
        return 0

def require_officer(f):
    """Decorator to gate endpoints behind officer rank (rank >= 5).

    Builds on require_org_member's session check, then enforces the rank
    threshold. The officer dashboard pulls stats across the whole org, so
    we don't want regular members hitting these endpoints.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        discord_id = session.get('discord_id')
        if not discord_id:
            return jsonify({'error': 'Not authenticated'}), 401
        if rank_int(session.get('rank')) < 5:
            return jsonify({'error': 'Officer access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def require_page_login(f):
    """Page-level gate for UI routes. Anonymous visitors are bounced to the
    dashboard, which hosts the 'Login with Discord' overlay — without this,
    every page was reachable by typing its URL directly. On the dev
    deployment, logged-in members below rank 4 are refused outright."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('discord_id'):
            return redirect('/')
        if is_dev and rank_int(session.get('rank')) < 4:
            return DEV_AREA_DENIED, 403
        return f(*args, **kwargs)
    return decorated_function



# ══════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
    
def get_user_db():
    """Connect to Sol Provision user database (Discord members from SPARQy).

    Local Windows dev keeps a copy at the repo root; on Linux deploys (dev/prod)
    the canonical copy lives under /var/www/sparqy/data/. MEE6_DB env var wins
    if set so ops can override without code changes.
    """
    path = os.environ.get('MEE6_DB')
    if not path:
        if os.name == 'nt':
            path = str(Path(__file__).resolve().parent.parent / 'mee6_snapshots.db')
        else:
            path = '/var/www/sparqy/data/mee6_snapshots.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_org_status_db(read_only=False):
    """Connect to the org-status DB (division readiness shown on the portal).

    This app is the only WRITER; the portal opens the same file read-only.
    Mirrors get_ownership_db()'s auto-create-on-first-use so a fresh deploy
    needs no manual sqlite3 setup — but the schema is version-tracked with
    PRAGMA user_version inside shared/org_status.py rather than re-running
    CREATE TABLE IF NOT EXISTS, so an older file can't sit at a stale shape and
    fail at query time with no signal.
    """
    conn = org_status.connect(read_only=read_only)
    if not read_only:
        org_status.migrate(conn)
    return conn


def get_applications_db(read_only=True):
    """Connect to applications.db — the public /join form's landing table.

    READ-ONLY by default and by design: the PORTAL owns writes to this file
    (it is the thing taking submissions), the same way this app owns writes to
    org_status.db and the portal only reads that. Officer review that needs to
    change a row's status would make this a second writer, which is a
    deliberate decision, not something to switch on quietly.
    """
    return applications.connect(read_only=read_only)


def get_opord_db(read_only=False):
    """Connect to opord.db, creating and migrating it on first use.

    This app is the WRITER (the officer-only editor); the portal will read it
    read-only for the Mission Board, same direction as org_status.db.
    """
    conn = opord.connect(read_only=read_only)
    if not read_only:
        opord.migrate(conn)
    return conn


# Mission Commander picker order, set by Dusty: these four by name, then any
# other rank-5, then rank-4 (Wing Commanders) alphabetically. Names not on the
# roster are simply skipped, so this list can outlive a rank change.
COMMANDER_PRIORITY = ["Sulyce", "Marauder", "Jenner Darr", "Entriri"]


def commander_options():
    """Rank 4+ members for the Mission Commander dropdown."""
    conn = get_user_db()
    try:
        rows = conn.execute(
            """SELECT display_name, username, rank FROM discord_members
               WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM discord_members)
                 AND CAST(rank AS INTEGER) >= 4""").fetchall()
    finally:
        conn.close()

    people = []
    seen = set()
    for r in rows:
        name = (r["display_name"] or r["username"] or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        people.append({"name": name, "rank": rank_int(r["rank"])})

    by_name = {p["name"].lower(): p for p in people}
    ordered = []
    for pinned in COMMANDER_PRIORITY:
        p = by_name.pop(pinned.lower(), None)
        if p:
            ordered.append(p)
    # Sort on letters only: a display name like '"Steel Team Six" WickerBeast'
    # would otherwise lead the list on its opening quote rather than its name.
    def sort_key(person):
        stripped = re.sub(r"^[^0-9A-Za-z]+", "", person["name"])
        return (-person["rank"], (stripped or person["name"]).lower())

    rest = sorted(by_name.values(), key=sort_key)
    return ordered + rest


# ── Blueprint ownership DB (separate from dataforge.db) ───────────────────────
# The extractor pipeline replaces dataforge.db wholesale every patch, so the
# ownership rows have to live in their own file. Default location sits next to
# dataforge.db so all app-writable state stays in one directory; OWNERSHIP_DB
# env var overrides for ops flexibility.

OWNERSHIP_SCHEMA = """
    CREATE TABLE IF NOT EXISTS blueprint_ownership (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id TEXT NOT NULL,
        blueprint_uuid TEXT NOT NULL,
        blueprint_name TEXT NOT NULL,
        patch_version TEXT NOT NULL,
        claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        env TEXT NOT NULL CHECK(env IN ('prod', 'dev')),
        notes TEXT,
        UNIQUE(discord_id, blueprint_uuid, patch_version)
    )
"""


def _resolve_ownership_db_path():
    """Resolve the ownership DB path. OWNERSHIP_DB env var wins; otherwise it
    sits next to whatever DB_PATH points at (computed lazily so --db overrides
    in __main__ still work)."""
    explicit = os.environ.get('OWNERSHIP_DB')
    if explicit:
        return explicit
    return str(Path(DB_PATH).resolve().parent / 'blueprint_ownership.db')


def get_ownership_db():
    """Connect to blueprint_ownership DB. Auto-creates the table on first run
    so a fresh deploy doesn't need manual sqlite3-CLI setup."""
    conn = sqlite3.connect(_resolve_ownership_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(OWNERSHIP_SCHEMA)
    conn.commit()
    return conn


def get_db_with_ownership():
    """Open the dataforge connection and ATTACH the ownership DB as `own`.

    Use for endpoints that JOIN crafting_blueprints against blueprint_ownership;
    simple lookups stick to get_ownership_db() alone. The ownership path comes
    from our own env config (not user input) so splicing it into the ATTACH
    statement is safe.
    """
    conn = get_db()
    own_path = _resolve_ownership_db_path().replace("'", "''")
    conn.execute(f"ATTACH DATABASE '{own_path}' AS own")
    # CREATE TABLE on the attached side too, so a fresh file is usable in JOINs
    # immediately. `own.` prefix targets the attached DB.
    conn.execute(OWNERSHIP_SCHEMA.replace(
        "CREATE TABLE IF NOT EXISTS blueprint_ownership",
        "CREATE TABLE IF NOT EXISTS own.blueprint_ownership"
    ))
    conn.commit()
    return conn


# ── Ship ownership DB (mirrors blueprint_ownership) ───────────────────────────
# Same design: standalone file so it survives wholesale dataforge.db swaps,
# env column so dev-tools claims never pollute prod. Ships are identified by
# entity_name (stable across patches, unlike uuid).

SHIP_OWNERSHIP_SCHEMA = """
    CREATE TABLE IF NOT EXISTS ship_ownership (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id TEXT NOT NULL,
        rsi_id INTEGER,          -- catalog identity (the claim key; concept-safe)
        ship_entity TEXT NOT NULL,
        ship_name TEXT NOT NULL,
        patch_version TEXT NOT NULL,
        claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        env TEXT NOT NULL CHECK(env IN ('prod', 'dev')),
        notes TEXT,
        source TEXT,            -- how the owner got it: 'pledge' | 'in-game'
        UNIQUE(discord_id, ship_entity, patch_version)
    )
"""

# Saved ship loadouts live in the same standalone DB. `loadout_key` is a short
# random share token (in the URL); discord_id is the creator (saving is login-
# gated, so always set). loadout_json is a full-state snapshot of the page.
SAVED_LOADOUTS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS saved_loadouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        loadout_key TEXT NOT NULL UNIQUE,
        discord_id TEXT NOT NULL,
        ship_entity TEXT NOT NULL,
        name TEXT NOT NULL,
        loadout_json TEXT NOT NULL,
        patch_version TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        env TEXT NOT NULL CHECK(env IN ('prod', 'dev'))
    )
"""


def _resolve_ship_ownership_db_path():
    """SHIP_OWNERSHIP_DB env var wins; otherwise next to DB_PATH (lazy so --db
    overrides in __main__ still work)."""
    explicit = os.environ.get('SHIP_OWNERSHIP_DB')
    if explicit:
        return explicit
    return str(Path(DB_PATH).resolve().parent / 'ship_ownership.db')


def get_ship_ownership_db():
    """Connect to ship_ownership DB. Auto-creates both tables on first run."""
    conn = sqlite3.connect(_resolve_ship_ownership_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(SHIP_OWNERSHIP_SCHEMA)
    conn.execute(SAVED_LOADOUTS_SCHEMA)
    # Migrations: add columns introduced after the table first shipped.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ship_ownership)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE ship_ownership ADD COLUMN source TEXT")
    if "rsi_id" not in cols:
        conn.execute("ALTER TABLE ship_ownership ADD COLUMN rsi_id INTEGER")
    conn.commit()
    return conn


_OWNERSHIP_MIGRATION_DONE = False

def _migrate_legacy_ownership_rows():
    """One-time copy of blueprint_ownership rows from dataforge.db (where they
    used to live) into the new standalone ownership DB. Idempotent — does
    nothing if the new DB already has rows, so safe to call on every boot.

    Has to run before the extractor replaces dataforge.db; otherwise the source
    rows are gone. Logged so deployments can confirm the count moved over.
    """
    global _OWNERSHIP_MIGRATION_DONE
    if _OWNERSHIP_MIGRATION_DONE:
        return
    _OWNERSHIP_MIGRATION_DONE = True
    try:
        own = get_ownership_db()
        n_existing = own.execute("SELECT COUNT(*) FROM blueprint_ownership").fetchone()[0]
        if n_existing > 0:
            own.close()
            return
        df = sqlite3.connect(DB_PATH)
        df.row_factory = sqlite3.Row
        legacy = df.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='blueprint_ownership'"
        ).fetchone()
        if not legacy:
            df.close()
            own.close()
            return
        rows = df.execute("""
            SELECT discord_id, blueprint_uuid, blueprint_name, patch_version,
                   claimed_at, env, notes
            FROM blueprint_ownership
        """).fetchall()
        df.close()
        copied = 0
        for r in rows:
            try:
                own.execute("""
                    INSERT INTO blueprint_ownership
                        (discord_id, blueprint_uuid, blueprint_name,
                         patch_version, claimed_at, env, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (r['discord_id'], r['blueprint_uuid'], r['blueprint_name'],
                      r['patch_version'], r['claimed_at'], r['env'], r['notes']))
                copied += 1
            except sqlite3.IntegrityError:
                pass
        own.commit()
        own.close()
        if copied:
            print(f"  Migrated {copied} blueprint_ownership rows → {_resolve_ownership_db_path()}")
    except sqlite3.Error as e:
        # Swallow so a half-set-up local dev env doesn't crash the whole app.
        print(f"  Ownership legacy migration skipped: {e}")


_CLAIM_RSI_MIGRATION_DONE = False

def _migrate_claims_rsi_id():
    """Backfill rsi_id on existing ship_ownership rows by mapping their
    ship_entity (dataforge name) to the catalog's rsi_id. Idempotent — only
    touches rows still missing rsi_id. Needs both DBs (catalog in dataforge.db,
    claims in the standalone ownership DB)."""
    global _CLAIM_RSI_MIGRATION_DONE
    if _CLAIM_RSI_MIGRATION_DONE:
        return
    _CLAIM_RSI_MIGRATION_DONE = True
    try:
        own = get_ship_ownership_db()
        pending = own.execute(
            "SELECT id, ship_entity FROM ship_ownership "
            "WHERE rsi_id IS NULL AND ship_entity IS NOT NULL").fetchall()
        if not pending:
            own.close()
            return
        df = sqlite3.connect(DB_PATH); df.row_factory = sqlite3.Row
        if not df.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ship_catalog'").fetchone():
            df.close(); own.close()
            return
        patch = latest_patch(df)
        # A data_name may back several catalog rows (editions aliased to a base
        # ship). Order so the genuine matched base (rsi_id stamped on the ship
        # row) is last and wins the dict overwrite — legacy claims migrate to it.
        name_to_id = {r["data_name"]: r["rsi_id"] for r in df.execute(
            """SELECT sc.data_name, sc.rsi_id
               FROM ship_catalog sc
               LEFT JOIN ships s ON s.entity_name = sc.data_name
                                AND s.patch_version = sc.patch_version
               WHERE sc.patch_version=? AND sc.data_name IS NOT NULL
               ORDER BY (sc.rsi_id = s.rsi_ship_id) ASC""",
            (patch,))}
        df.close()
        fixed = 0
        for row in pending:
            rid = name_to_id.get(row["ship_entity"])
            if rid is not None:
                own.execute("UPDATE ship_ownership SET rsi_id=? WHERE id=?", (rid, row["id"]))
                fixed += 1
        own.commit(); own.close()
        if fixed:
            print(f"  Backfilled rsi_id on {fixed} ship_ownership rows")
    except sqlite3.Error as e:
        print(f"  Claim rsi_id migration skipped: {e}")


# ── Warehouse inventory DB (org ore reserves, fed from a Google Sheet) ────────
# Standalone file, same rationale as the ownership DBs: this is org operational
# state (not catalog data), so it lives outside dataforge.db and survives the
# wholesale patch swaps. A cron script (tools/pull_warehouse_inventory.py)
# mirrors a shared Google Sheet into warehouse_inventory on a schedule; the
# /api/officers/warehouse endpoint only reads it. The pull is the sole writer.

WAREHOUSE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS warehouse_inventory (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        material   TEXT NOT NULL,
        qty        REAL,
        quality    TEXT,            -- refinery quality band, e.g. '750-799'
        location   TEXT,
        row_index  INTEGER,         -- source sheet order, for stable display
        pulled_at  INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS warehouse_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
"""


def _resolve_warehouse_db_path():
    """WAREHOUSE_DB env var wins; otherwise next to DB_PATH (lazy so --db
    overrides in __main__ still resolve correctly)."""
    explicit = os.environ.get('WAREHOUSE_DB')
    if explicit:
        return explicit
    return str(Path(DB_PATH).resolve().parent / 'warehouse_inventory.db')


def get_warehouse_db():
    """Connect to the warehouse inventory DB. Auto-creates the schema so a fresh
    deploy (or a sheet that hasn't been pulled yet) serves an empty table rather
    than 500-ing."""
    conn = sqlite3.connect(_resolve_warehouse_db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(WAREHOUSE_SCHEMA)
    conn.commit()
    return conn


def latest_patch(conn):
    row = conn.execute("SELECT patch_version FROM patch_history ORDER BY imported_at DESC LIMIT 1").fetchone()
    return row["patch_version"] if row else "4.6"


def is_placeholder(name):
    """CIG ships unfinished items with a '<= PLACEHOLDER =>' display name; these
    should never surface in the UI. Matches any name containing 'placeholder'."""
    return bool(name) and "placeholder" in str(name).lower()


# ── Cargo planner user DB (saved plans + activity) ────────────────────────────
_cargo_schema_ready = False

def _resolve_cargo_db_path():
    """cargo_planner.db lives alongside dataforge.db unless overridden."""
    return os.environ.get('CARGO_PLANNER_DB') or \
        str(Path(DB_PATH).resolve().parent / 'cargo_planner.db')

def _ensure_cargo_schema(conn):
    """Apply the canonical schema from tools/init_cargo_planner_db.py so the
    server is self-sufficient (no separate init step needed in deploys).
    ensure_schema() runs tables → column heals → indexes in that order, so a DB
    whose tables pre-date a column (e.g. mission_stacks.discord_id) self-heals."""
    import importlib.util
    init_path = Path(__file__).resolve().parent.parent / "tools" / "init_cargo_planner_db.py"
    spec = importlib.util.spec_from_file_location("init_cargo_planner_db", init_path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.ensure_schema(conn)

def get_cargo_db():
    global _cargo_schema_ready
    conn = sqlite3.connect(_resolve_cargo_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not _cargo_schema_ready:
        _ensure_cargo_schema(conn)
        _cargo_schema_ready = True
    return conn


# ── UEX live commodity-price feed DB (read side) ──────────────────────────────
# Populated by the 15-min cron writer (tools/pull_uex_prices.py). The web app is
# a reader; we self-heal the schema once so a missing table never 500s the page.
_uex_schema_ready = False

def _resolve_uex_db_path():
    """uex_feed.db sits at the repo root (matches tools/init_uex_feed_db.py's
    DEFAULT_DB_PATH) unless overridden by UEX_FEED_DB."""
    return os.environ.get('UEX_FEED_DB') or \
        str(Path(__file__).resolve().parent.parent / 'uex_feed.db')

def get_uex_db():
    global _uex_schema_ready
    conn = sqlite3.connect(_resolve_uex_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")  # ride out the 15-min writer's lock
    if not _uex_schema_ready:
        import importlib.util
        init_path = Path(__file__).resolve().parent.parent / "tools" / "init_uex_feed_db.py"
        spec = importlib.util.spec_from_file_location("init_uex_feed_db", init_path)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        mod.ensure_schema(conn)
        _uex_schema_ready = True
    return conn

def _utc_now():
    return datetime.now(timezone.utc).isoformat()

def _cargo_session_id():
    sid = session.get('_cargo_sid')
    if not sid:
        sid = uuid.uuid4().hex
        session['_cargo_sid'] = sid
    return sid

def _cargo_touch_user(conn, discord_id):
    """Upsert the users row so the activity dashboard has a current name."""
    now = _utc_now()
    conn.execute("""
        INSERT INTO users (discord_id, first_seen_utc, last_seen_utc, username, display_name)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            last_seen_utc = excluded.last_seen_utc,
            username      = COALESCE(excluded.username, users.username),
            display_name  = COALESCE(excluded.display_name, users.display_name)
    """, (discord_id, now, now, session.get('username'), session.get('callsign')))

def _cargo_log(conn, discord_id, event_type, details=None):
    conn.execute("""
        INSERT INTO activity_log (discord_id, event_type, event_details, timestamp_utc, session_id)
        VALUES (?, ?, ?, ?, ?)
    """, (discord_id, event_type,
          json.dumps(details) if details is not None else None,
          _utc_now(), _cargo_session_id()))

MFR_MAP = {
    "aegs":"Aegis","anvl":"Anvil","argo":"Argo","cnou":"C.O.","crus":"Crusader",
    "drak":"Drake","espr":"Esperia","gama":"Gatac","grin":"Greycat","krig":"Kruger",
    "misc":"MISC","mrai":"Mirai","orig":"Origin","rsi":"RSI","tmbl":"Tumbril",
    "behr":"Behring","klwe":"Klaus & Werner","apar":"Apocalypse","acas":"Castra",
    "acom":"Achilles","amrs":"Armistice","basl":"Basilisk","clda":"CLDA","cdas":"CDS",
    "vncl":"Vanduul","xian":"Xi'an","csin":"Preacher","taln":"Talon", "just":"Juno Starwerk"
}
def get_mfr(n): return MFR_MAP.get((n or "").split("_")[0].lower(), (n or "").split("_")[0].upper())
def comp_grade(grade_num):
    """
    Convert numeric grade (1-5) to letter grade (A-F).
    1 = A (Military)
    2 = B (Civilian) 
    3 = C (Industrial)
    4 = D (Competition)
    5 = F (Stealth)
    """
    if grade_num is None:
        return '—'
    
    grade_map = {
        1: 'A',
        2: 'B', 
        3: 'C',
        4: 'D',
        5: 'F'
    }
    
    return grade_map.get(grade_num, '—')

def _prettify_faction(name):
    """Turn faction org names into readable labels for the filter sidebar.
    'CitizensForProsperity' → 'Citizens For Prosperity', 'RedWindLinehaul'
    → 'Red Wind Linehaul'. Names that already contain spaces are left as-is.
    """
    if not name:
        return name
    if " " in name:
        return name
    # Split camelCase / acronym boundaries.
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return s.strip()


def clean_career(s): return (s or "").replace("@vehicle_focus_","").replace("@vehicle_class_","").replace("_"," ").title()

def clean_role(s):   return (s or "").replace("@vehicle_class_","").replace("@vehicle_role_","").replace("_"," ").title()

def best_name(display_name, entity_name):
    """Return the best available display name — loc-resolved first, entity name fallback."""
    if display_name:
        return display_name
    return (entity_name or "").replace("_", " ").title()

EXCLUDE = """entity_name NOT LIKE '%_pu_ai%' AND entity_name NOT LIKE '%_ea_ai%'
  AND entity_name NOT LIKE '%_unmanned%' AND entity_name NOT LIKE '%_ai_template%'
  AND entity_name NOT LIKE '%_showdown' AND entity_name NOT LIKE '%_piano'
  AND entity_name NOT LIKE '%_bombless' AND entity_name NOT LIKE '%_teach'"""

def ensure_columns(db_path):
    """Add display_name/description columns if not present (pre-localization DBs)."""
    conn = sqlite3.connect(db_path)
    migrations = [
        ("ships",    "display_name", "TEXT"),
        ("entities", "display_name", "TEXT"),
        ("entities", "description",  "TEXT"),
    ]
    for table, col, defn in migrations:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            print(f"  Added column: {table}.{col}")
    conn.commit()
    conn.close()


def ensure_indexes(db_path):
    """Create indexes the app's hot queries depend on but the import pipeline
    doesn't build. Idempotent — safe to run on every startup.

    idx_item_components_entity: the /api/crafting/blueprints query joins
    item_components on (entity_name, patch_version). The table's only index is
    the (uuid, patch_version) primary key, so without this the join falls back
    to a full scan per blueprint row — ~10s for the crafting page's initial
    load. With it, that query drops to <100ms.
    """
    conn = sqlite3.connect(db_path)
    indexes = [
        ("idx_item_components_entity",
         "item_components", "(entity_name, patch_version)"),
    ]
    for name, table, cols in indexes:
        existing = {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}
        if name not in existing:
            conn.execute(f"CREATE INDEX {name} ON {table}{cols}")
            print(f"  Created index: {name}")
    conn.commit()
    conn.close()


COMP_PARAMS = {
    "Shield":       "SCItemShieldGeneratorParams",
    "Cooler":       "SCItemCoolerParams",
    "QuantumDrive": "SCItemQuantumDriveParams",
    "PowerPlant":   "SCItemPowerPlantParams",
}

# Maps crafting gameplay-property entity_name → ordered list of (item_table, column)
# candidates. Used by /api/crafting/blueprint to compute the base value that the
# slot quality modifier scales — first table where the output's entity_name exists
# with a non-null value wins.
#
# Tables not yet populated (or props with no clean column counterpart) are
# omitted; the API returns base_value=null for those and the frontend falls
# back to multiplier-only display.
GPP_BASE_RESOLVERS = {
    # Character armor — clothing params
    "gpp_armor_temperaturemin":       [("item_char_armor", "temperature_min")],
    "gpp_armor_temperaturemax":       [("item_char_armor", "temperature_max")],
    "gpp_armor_radiationcapacity":    [("item_char_armor", "radiation_capacity")],
    "gpp_armor_radiationdissipation": [("item_char_armor", "radiation_dissipation")],

    # Generic health — try every item_* table with a health column, first hit wins.
    "gpp_health_maxhealth": [
        ("item_shields",             "health"),
        ("item_coolers",             "health"),
        ("item_powerplants",         "health"),
        ("item_quantum_drives",      "health"),
        ("item_radars",              "health"),
        ("item_fuel_nozzles",        "health"),
        ("item_external_fuel_tanks", "health"),
        ("item_fuel_tanks",          "health"),
        ("item_quantum_fuel_tanks",  "health"),
        ("item_armor",               "health"),
        ("item_scanners",            "health"),
        ("item_weapons",             "health"),
    ],

    # Coolers
    "gpp_itemresource_coolantgeneration": [("item_coolers", "cooling_output")],

    # Quantum drives
    "gpp_quantum_fuelrequirement": [("item_quantum_drives", "quantum_fuel_req")],
    "gpp_quantum_speed":           [("item_quantum_drives", "drive_speed")],

    # Radars
    "gpp_radar_maxaimassistdistance": [("item_radars", "aim_assist_max_m")],
    "gpp_radar_minaimassistdistance": [("item_radars", "aim_assist_min_m")],

    # Shields
    "gpp_shield_maxhealth": [("item_shields", "max_shield_health")],

    # FPS weapons
    "gpp_weapon_firerate": [("item_fps_weapons", "fire_rate")],
    "gpp_weapon_spread":   [("item_fps_weapons", "spread_max")],

    # Salvage scrapers
    "gpp_weapon_hullscraping_speed":      [("item_salvage_modifiers", "salvage_speed_multiplier")],
    "gpp_weapon_hullscraping_radius":     [("item_salvage_modifiers", "radius_multiplier")],
    "gpp_weapon_hullscraping_efficiency": [("item_salvage_modifiers", "extraction_efficiency")],

    # Tractor / towing beams (extracted from SWeaponActionFireTractorBeamParams
    # into item_weapons tractor_* columns).
    "gpp_weapon_tractor_force":            [("item_weapons", "tractor_min_force")],
    "gpp_weapon_tractor_fullstrengthdist": [("item_weapons", "tractor_fullstrength_dist")],
    "gpp_weapon_tractor_maxdist":          [("item_weapons", "tractor_max_distance")],
    "gpp_weapon_tractor_maxvolume":        [("item_weapons", "tractor_max_volume")],
}

# Properties whose modifier curve is shown as a signed percent change rather
# than an absolute value × base. The frontend renders these as "+18%" / "-7%"
# using just (multiplier - 1) × 100; no base lookup needed.
GPP_PERCENT_PROPS = {
    "gpp_weapon_damage",
    "gpp_weapon_damage_override_laser",
    "gpp_weapon_recoil_handling",
    "gpp_weapon_recoil_kick",
    "gpp_weapon_recoil_smoothness",
    "gpp_armor_damagemitigation",
}


def resolve_ingredient_quantization(db, patch, resource_uuid):
    """Look up the quantization curve for an ingredient resource.

    Returns a dict with {uuid, material_name, bands: [{start, end, mapped}]}
    or None if the resource has no linked quantization (most non-mineable
    resources — food, plants — don't have one).
    """
    if not resource_uuid:
        return None
    head = db.execute("""
        SELECT crq.quantization_uuid, qr.material_name
        FROM crafting_resource_quantization crq
        JOIN crafting_quantization_records qr
            ON qr.uuid = crq.quantization_uuid
           AND qr.patch_version = crq.patch_version
        WHERE crq.resource_uuid = ? AND crq.patch_version = ?
        LIMIT 1
    """, (resource_uuid, patch)).fetchone()
    if not head:
        return None
    bands = db.execute("""
        SELECT band_start AS start, band_end AS "end", mapped_value AS mapped
        FROM crafting_quantization_bands
        WHERE quantization_uuid = ? AND patch_version = ?
        ORDER BY band_index ASC
    """, (head["quantization_uuid"], patch)).fetchall()
    return {
        "uuid":          head["quantization_uuid"],
        "material_name": head["material_name"],
        "bands":         [dict(b) for b in bands],
    }


def resolve_gpp_base_value(db, patch, gpp_entity_name, output_entity_name):
    """Look up the output's base value for a given gameplay property.

    Returns (value, table_name) or (None, None) when no candidate hits. The
    table_name is returned so the caller can surface it for debugging or
    cite where the base came from.
    """
    if not gpp_entity_name or not output_entity_name:
        return None, None
    candidates = GPP_BASE_RESOLVERS.get(gpp_entity_name.lower())
    if not candidates:
        return None, None
    for table, column in candidates:
        try:
            row = db.execute(
                f"SELECT {column} AS v FROM {table} "
                f"WHERE entity_name=? AND patch_version=? LIMIT 1",
                (output_entity_name, patch),
            ).fetchone()
        except sqlite3.Error:
            continue
        if row and row["v"] is not None:
            return row["v"], table
    return None, None


# ══════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    # Anonymous visitors are allowed here — this page hosts the Discord
    # login overlay that every other page redirects to. The dev-site rank
    # gate still applies to anyone already logged in.
    if is_dev and session.get('discord_id') and rank_int(session.get('rank')) < 4:
        return DEV_AREA_DENIED, 403
    return render_template("index.html", active_page="/")

@app.route("/ships")
@require_page_login
def ships_page():
    return render_template("ships.html", active_page="/ships")

@app.route("/ships/<entity_name>")
@require_page_login
def ship_detail(entity_name):
    return render_template("ship_detail.html", entity_name=entity_name, active_page="ships")

#@app.route("/components")
#def components_page(): return render_template("components.html", active_page="/components")

#@app.route("/weapons/ship")
#def weapons_ship_page(): return render_template("weapons_ship.html", active_page="/weapons/ship")

#@app.route("/weapons/fps")
#def weapons_fps_page(): return render_template("weapons_fps.html", active_page="/weapons/fps")

#@app.route("/armor")
#def armor_page(): return render_template("armor.html", active_page="/armor")

@app.route("/crafting")
@require_page_login
def crafting():
    return render_template("crafting.html", active_page="/crafting")

@app.route("/officers")
@require_page_login
def officers_page():
    # Page-level gate: non-officers (rank < 5) get bounced to the dashboard
    # instead of seeing an empty shell. The API endpoints behind this page
    # apply the same check via @require_officer.
    if rank_int(session.get('rank')) < 5:
        return redirect('/')
    return render_template("officers.html", active_page="/officers")

@app.route("/mission-rep")
@require_page_login
def mission_rep():
    return render_template("mission_rep.html", active_page="/mission-rep")

@app.route("/mining-signatures")
@require_page_login
def mining_signatures_page():
    return render_template("mining_signatures.html", active_page="/mining-signatures")

@app.route("/prospector")
@require_page_login
def prospector_page():
    return render_template("the_prospector.html", active_page="/prospector")

@app.route("/cargo-planner")
@require_page_login
def cargo_planner_page():
    return render_template("cargo_planner.html", active_page="/cargo-planner")

@app.route("/ledger")
@require_page_login
def ledger(): return render_template("ledger.html", active_page="ledger")

@app.route("/item_collection")
@require_page_login
def item_collection_page(): return render_template("item_collection.html", active_page="/item_collection")

@app.route("/base-builder")
@require_page_login
def base_builder_page(): return render_template("base_builder.html", active_page="/base-builder")

@app.route("/refinery")
@require_page_login
def refinery_page(): return render_template("refinery_session.html", active_page="/refinery")

@app.route("/starmap")
@app.route("/starmap/<system>")
@app.route("/starmap/<system>/<body>")
# /solmap = branded alias for the same page. The starmap JS (util/url.js) parses
# system/body from path segments 1+2 regardless of segment 0, so deep links work;
# it canonicalizes the address bar back to /starmap once loaded.
@app.route("/solmap")
@app.route("/solmap/<system>")
@app.route("/solmap/<system>/<body>")
@require_page_login
def starmap_page(system=None, body=None):
    # JS reads the path off window.location and applies system + body focus.
    return render_template("starmap.html", active_page="/starmap")


@app.route("/trade")
@app.route("/trade/<view>")
@require_page_login
def trade_page(view=None):
    # Live UEX commodity feed. Multiple views are toggled client-side via the
    # left sidebar; the optional <view> segment just deep-links the initial one.
    return render_template("trade.html", active_page="/trade")


@app.route("/api/trade/commodities")
def api_trade_commodities():
    """Commodity board: every commodity in the feed, with its current network
    sell price (avg of the latest non-zero sell across terminals) and a 10-day
    price trend.

    "Current" and "baseline" are both derived from our own snapshot history so
    they stay consistent. The time axis is `pulled_at` (when WE observed a price),
    NOT `date_modified` (UEX's source-edit time, which legitimately spans days
    across terminals): trend means "how the price moved over the 10 days we've
    been watching." Baseline = the earliest observation per terminal inside the
    window. Until the feed has accrued >1 day of our own history the trend is
    reported as 'pending' rather than a misleading flat arrow.
    """
    window_days = 10
    flat_eps = 0.5  # |%| below this reads as flat, not up/down
    cutoff = int(time.time()) - window_days * 86400

    conn = get_uex_db()
    rows = conn.execute("""
        WITH snap AS (
            SELECT id_commodity, id_terminal, price_sell, pulled_at
            FROM price_snapshots
            WHERE price_sell IS NOT NULL AND price_sell > 0
        ),
        latest AS (
            SELECT id_commodity, price_sell,
                   ROW_NUMBER() OVER (PARTITION BY id_commodity, id_terminal
                                      ORDER BY pulled_at DESC) rn
            FROM snap
        ),
        cur AS (
            SELECT id_commodity, AVG(price_sell) AS cur_price, COUNT(*) AS terminals
            FROM latest WHERE rn = 1 GROUP BY id_commodity
        ),
        windowed AS (
            SELECT id_commodity, price_sell,
                   ROW_NUMBER() OVER (PARTITION BY id_commodity, id_terminal
                                      ORDER BY pulled_at ASC) rn
            FROM snap WHERE pulled_at >= ?
        ),
        base AS (
            SELECT id_commodity, AVG(price_sell) AS base_price
            FROM windowed WHERE rn = 1 GROUP BY id_commodity
        ),
        span AS (
            SELECT id_commodity, MIN(pulled_at) AS first_pa,
                                 MAX(pulled_at) AS last_pa
            FROM snap GROUP BY id_commodity
        )
        SELECT cm.id_commodity, cm.code, cm.name,
               cur.cur_price, cur.terminals,
               base.base_price, span.first_pa, span.last_pa
        FROM commodities cm
        JOIN (SELECT DISTINCT id_commodity FROM price_snapshots) feed
             ON feed.id_commodity = cm.id_commodity
        LEFT JOIN cur  ON cur.id_commodity  = cm.id_commodity
        LEFT JOIN base ON base.id_commodity = cm.id_commodity
        LEFT JOIN span ON span.id_commodity = cm.id_commodity
        ORDER BY cm.name COLLATE NOCASE
    """, (cutoff,)).fetchall()
    as_of = conn.execute("SELECT MAX(pulled_at) FROM price_snapshots").fetchone()[0]
    conn.close()

    out = []
    for r in rows:
        cur_price, base_price = r["cur_price"], r["base_price"]
        first_pa, last_pa = r["first_pa"], r["last_pa"]
        # Only call a direction once we've actually watched it for >1 day.
        has_span = first_pa is not None and last_pa is not None and (last_pa - first_pa) >= 86400
        if has_span and cur_price and base_price:
            pct = (cur_price - base_price) / base_price * 100.0
            state = "flat" if abs(pct) < flat_eps else ("up" if pct > 0 else "down")
            trend = {"state": state, "pct": round(pct, 1)}
        else:
            trend = {"state": "pending", "pct": None}
        out.append({
            "id": r["id_commodity"],
            "code": r["code"] or (r["name"] or "")[:4].upper(),
            "name": r["name"],
            "price": round(cur_price, 2) if cur_price is not None else None,
            "terminals": r["terminals"] or 0,
            "trend": trend,
        })

    return jsonify({
        "as_of": as_of,
        "window_days": window_days,
        "count": len(out),
        "commodities": out,
    })


@app.route("/api/trade/commodity/<int:id_commodity>")
def api_trade_commodity_detail(id_commodity):
    """Detail panel for one commodity: the latest observation at every terminal
    that trades it (joined to the terminal's location/faction metadata), plus a
    summary block powering the four header cards.

    Per the live feed, a terminal is either a BUY point (price_buy>0, you source
    it there) or a SELL point (price_sell>0, you offload it there). We take the
    most recent snapshot per terminal so the panel reflects the current market.
    """
    conn = get_uex_db()
    cm = conn.execute(
        "SELECT id_commodity, code, name FROM commodities WHERE id_commodity = ?",
        (id_commodity,),
    ).fetchone()

    rows = conn.execute("""
        WITH latest AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY id_terminal
                                         ORDER BY pulled_at DESC) AS rn
            FROM price_snapshots
            WHERE id_commodity = ?
        )
        SELECT l.id_terminal, l.price_buy, l.price_sell,
               l.scu_buy, l.scu_sell, l.scu_sell_stock,
               l.status_buy, l.status_sell, l.container_sizes,
               l.date_modified, l.pulled_at,
               t.name AS terminal_name, t.code AS terminal_code,
               t.star_system_name, t.planet_name, t.orbit_name, t.moon_name,
               t.space_station_name, t.city_name, t.outpost_name,
               t.faction_name, t.max_container_size
        FROM latest l
        LEFT JOIN terminals t ON t.id_terminal = l.id_terminal
        WHERE l.rn = 1
    """, (id_commodity,)).fetchall()
    as_of = conn.execute("SELECT MAX(pulled_at) FROM price_snapshots").fetchone()[0]
    conn.close()

    def loc(r):
        """Readable 'planetary system' label: planet → moon/orbit, else station/city."""
        planet = r["planet_name"]
        sub = r["moon_name"] or r["orbit_name"]
        if planet and sub and sub != planet:
            return f"{planet} · {sub}"
        return planet or r["space_station_name"] or r["city_name"] or r["orbit_name"] or "—"

    terminals = []
    for r in rows:
        pb, ps = r["price_buy"], r["price_sell"]
        # Classify: a terminal you BUY at (sources stock) vs SELL at (takes goods).
        kind = "buy" if (pb and pb > 0) else ("sell" if (ps and ps > 0) else "none")
        terminals.append({
            "id_terminal":   r["id_terminal"],
            "terminal":      r["terminal_name"] or r["terminal_code"] or f"#{r['id_terminal']}",
            "system":        r["star_system_name"],
            "planet_system": loc(r),
            "faction":       r["faction_name"],
            "kind":          kind,
            "price_buy":     pb if (pb and pb > 0) else None,
            "price_sell":    ps if (ps and ps > 0) else None,
            "scu_buy":       r["scu_buy"],
            "scu_sell":      r["scu_sell"],
            "scu_stock":     r["scu_sell_stock"],
            "containers":    r["container_sizes"],
            "max_container": r["max_container_size"],
            "updated":       r["date_modified"] or r["pulled_at"],
        })

    # ── Summary cards ──────────────────────────────────────────────────────
    buys  = [t for t in terminals if t["price_buy"] is not None]
    sells = [t for t in terminals if t["price_sell"] is not None]

    best_buy = min(buys, key=lambda t: t["price_buy"]) if buys else None
    best_sell = max(sells, key=lambda t: t["price_sell"]) if sells else None

    # Profit/SCU = best (max) sell − best (min) buy, when both sides exist.
    profit = None
    if best_buy and best_sell:
        profit = {
            "value":     round(best_sell["price_sell"] - best_buy["price_buy"], 2),
            "buy_at":    best_buy["terminal"],
            "buy_price": best_buy["price_buy"],
            "sell_at":   best_sell["terminal"],
            "sell_price": best_sell["price_sell"],
        }

    # Supply vs demand: on-hand stock vs sought, summed across sell terminals.
    supply = sum(t["scu_stock"] or 0 for t in terminals)
    demand = sum(t["scu_sell"] or 0 for t in terminals)

    def card_loc(t):
        return None if t is None else {
            "terminal": t["terminal"], "system": t["system"],
            "faction": t["faction"], "price": t["price_sell"] or t["price_buy"],
        }

    return jsonify({
        "as_of": as_of,
        "id_commodity": id_commodity,
        "code": (cm["code"] if cm else None) or "",
        "name": (cm["name"] if cm else None) or "",
        "terminal_count": len(terminals),
        "summary": {
            "profit_per_scu": profit,
            "best_buy":  card_loc(best_buy),
            "best_sell": card_loc(best_sell),
            "supply": round(supply, 1),
            "demand": round(demand, 1),
        },
        "terminals": terminals,
    })


# ── Trade Routes (greedy profit optimizer) ────────────────────────────────────
# The route math lives in helpers/trade_routes.py (pure, unit-tested). These
# endpoints marshal the live feed into a per-location market and, when a quantum
# drive is supplied, enrich each leg with QT time via the existing nav engine.

_TRADE_MARKET_SQL = """
    WITH latest AS (
        SELECT ps.*, ROW_NUMBER() OVER (PARTITION BY ps.id_commodity, ps.id_terminal
                                        ORDER BY ps.pulled_at DESC) AS rn
        FROM price_snapshots ps
        WHERE (ps.price_buy > 0) OR (ps.price_sell > 0)
    )
    SELECT l.id_commodity, l.id_terminal, l.price_buy, l.price_sell,
           l.scu_buy, l.scu_sell,
           c.code AS commodity_code, c.name AS commodity_name,
           t.name AS terminal_name,
           t.id_star_system, t.star_system_name, t.id_planet, t.planet_name,
           t.id_orbit, t.orbit_name, t.id_moon, t.moon_name,
           t.space_station_name, t.city_name, t.outpost_name, t.faction_name
    FROM latest l
    JOIN commodities c ON c.id_commodity = l.id_commodity
    LEFT JOIN terminals t ON t.id_terminal = l.id_terminal
    WHERE l.rn = 1
"""


def _build_trade_market():
    """Build the per-location market from the latest snapshot per (commodity,
    terminal). Returns (market, as_of)."""
    from helpers.trade_routes import build_market
    conn = get_uex_db()
    try:
        rows = conn.execute(_TRADE_MARKET_SQL).fetchall()
        as_of = conn.execute("SELECT MAX(pulled_at) FROM price_snapshots").fetchone()[0]
    finally:
        conn.close()
    return build_market(rows), as_of


def _enrich_legs_with_qt(legs, market, ship_uuid, qd_uuid):
    """Best-effort: stamp each leg with QT time/fuel by matching its from/to
    locations to dataforge nav_points and reusing plan_leg(). Mutates legs in
    place; unmatched or cross-system-unreachable legs get qt={'unknown': True}.
    Returns (total_time_s, total_fuel_scu, unknown_count)."""
    from helpers.quantum_travel import plan_leg
    from helpers.trade_routes import build_navpoint_index, match_location_to_navpoint

    conn = get_db()
    try:
        p = PATCH or latest_patch(conn)
        qd_row = conn.execute(
            "SELECT * FROM item_quantum_drives WHERE patch_version=? AND uuid=?",
            (p, qd_uuid)).fetchone()
        if not qd_row:
            return None, None, len(legs)
        qd = dict(qd_row)
        ship = {}
        if ship_uuid:
            srow = conn.execute("SELECT * FROM ships WHERE patch_version=? AND uuid=?",
                                (p, ship_uuid)).fetchone()
            if srow:
                ship = dict(srow)

        nav = _get_nav_graph(conn, p)
        by_uuid, resolve = _build_navpt_hierarchy(conn, p)
        index = build_navpoint_index(by_uuid, resolve)

        # Resolve each market location to a nav uuid once.
        uuid_of = {}
        for key, loc in market.items():
            uuid_of[key] = match_location_to_navpoint(loc, index)

        total_t, total_f, unknown = 0.0, 0.0, 0
        for leg in legs:
            o, d = uuid_of.get(leg["from_key"]), uuid_of.get(leg["to_key"])
            if not o or not d:
                leg["qt"] = {"unknown": True, "reason": "location not on nav map"}
                unknown += 1
                continue
            try:
                sub = plan_leg(o, d, ship, qd, nav)
                t = sum(s.get("time_s", 0) or 0 for s in sub)
                f = sum(s.get("fuel_scu", 0) or 0 for s in sub)
                dist = sum(s.get("distance_m", 0) or 0 for s in sub)
                jumps = sum(1 for s in sub if s.get("kind") == "jump")
                leg["qt"] = {"unknown": False, "time_s": round(t, 1),
                             "fuel_scu": round(f, 2), "distance_m": dist, "jumps": jumps}
                total_t += t
                total_f += f
            except Exception as e:  # CrossSystemError, missing geometry, etc.
                leg["qt"] = {"unknown": True, "reason": str(e)}
                unknown += 1
        return round(total_t, 1), round(total_f, 2), unknown
    finally:
        conn.close()


@app.route("/api/trade/locations")
def api_trade_locations():
    """Selectable start/end locations for the route planner: every grouped
    location that has at least one buy or sell, ordered by system then label.

    location_meta_available tells the UI whether terminal location metadata has
    synced from UEX yet — when false, locations collapse to one-per-terminal and
    grouped routing isn't meaningful."""
    market, as_of = _build_trade_market()
    locations = []
    meta_available = False
    for loc in market.values():
        if loc["system"]:
            meta_available = True
        locations.append({
            "key": loc["key"],
            "label": loc["label"],
            "system": loc["system"],
            "buy_commodities": len(loc["buys"]),
            "sell_commodities": len(loc["sells"]),
        })
    locations.sort(key=lambda x: (x["system"] or "zzz", x["label"] or ""))
    return jsonify({
        "as_of": as_of,
        "count": len(locations),
        "location_meta_available": meta_available,
        "locations": locations,
    })


@app.route("/api/trade/route", methods=["POST"])
def api_trade_route():
    """Greedy max-profit route. Body: {capital, start_key, stops, end_key?,
    cargo_scu? | ship_uuid?, qd_uuid?}. Reinvests each leg's proceeds into the
    next; only the final leg is forced to end_key. With a qd_uuid, each leg also
    gets a best-effort QT time/fuel estimate."""
    from helpers.trade_routes import plan_trade_route, RouteParams

    body = request.get_json(silent=True) or {}
    try:
        capital = float(body.get("capital"))
        stops = int(body.get("stops"))
    except (TypeError, ValueError):
        return jsonify({"error": "capital and stops are required numbers"}), 400
    start_key = body.get("start_key")
    end_key = body.get("end_key") or None
    if not start_key:
        return jsonify({"error": "start_key is required"}), 400
    if stops < 1 or stops > 12:
        return jsonify({"error": "stops must be between 1 and 12"}), 400
    if capital <= 0:
        return jsonify({"error": "capital must be positive"}), 400

    # Cargo capacity: explicit override, else look up the ship in dataforge.
    cargo_scu = body.get("cargo_scu")
    ship_uuid = body.get("ship_uuid")
    qd_uuid = body.get("qd_uuid") or body.get("quantum_drive_uuid")
    if cargo_scu is None and ship_uuid:
        dconn = get_db()
        try:
            p = PATCH or latest_patch(dconn)
            srow = dconn.execute(
                "SELECT COALESCE(NULLIF(rsi_cargo_scu,0), cargo_scu) AS scu "
                "FROM ships WHERE patch_version=? AND uuid=?", (p, ship_uuid)).fetchone()
            if srow:
                cargo_scu = srow["scu"]
        finally:
            dconn.close()
    try:
        cargo_scu = float(cargo_scu)
    except (TypeError, ValueError):
        return jsonify({"error": "cargo_scu (or a ship with cargo) is required"}), 400
    if cargo_scu <= 0:
        return jsonify({"error": "ship has no cargo capacity"}), 400

    market, as_of = _build_trade_market()
    if start_key not in market:
        return jsonify({"error": "unknown start location"}), 400

    result = plan_trade_route(market, RouteParams(
        cargo_scu=cargo_scu, capital=capital, start_key=start_key,
        stops=stops, end_key=end_key, same_system=bool(body.get("same_system"))))

    qt_time = qt_fuel = qt_unknown = None
    if qd_uuid and result["legs"]:
        qt_time, qt_fuel, qt_unknown = _enrich_legs_with_qt(
            result["legs"], market, ship_uuid, qd_uuid)

    result.update({
        "as_of": as_of,
        "cargo_scu": cargo_scu,
        "qt_total_time_s": qt_time,
        "qt_total_fuel_scu": qt_fuel,
        "qt_unknown_legs": qt_unknown,
    })
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════════
# AUTH API ROUTES
# ══════════════════════════════════════════════════════════════════════

# Mock auth for local development. Sets the same session fields the real
# verify flow does, so the page/API auth gates behave as a logged-in officer.
# Guarded by is_local so a spoofed Host header can't enable it on a deploy.
@app.before_request
def mock_auth():
    if is_local and 'discord_id' not in session and request.host.startswith('localhost'):
        session['discord_id'] = '123456789'
        session['username'] = 'TestUser'
        session['callsign'] = 'Test User'
        session['rank'] = 5
        session['division'] = None
            
@app.route('/api/auth/verify', methods=['POST'])
def verify_auth():
    """
    Verify Firebase ID token and check user database for membership
    """
    try:
        data = request.get_json()
        id_token = data.get('idToken')
        
        if not id_token:
            return jsonify({'error': 'No token provided'}), 401
        
        # Verify the Firebase ID token
        decoded_token = firebase_auth.verify_id_token(id_token)
        
        # Extract Discord ID from provider data (not uid!)
        # Firebase uid is a Firebase-generated ID, not the Discord user ID
        firebase_data = decoded_token.get('firebase', {})
        identities = firebase_data.get('identities', {})
        
        # Discord ID should be in identities['oidc.discord'][0]
        discord_ids = identities.get('oidc.discord', [])
        
        if not discord_ids:
            print(f"No Discord ID found in token. Full token: {decoded_token}")
            return jsonify({
                'error': 'Discord ID not found in token',
                'message': 'Authentication token is missing Discord information.'
            }), 401
        
        discord_id = discord_ids[0]
        
        # Query user database
        user_conn = get_user_db()
        cursor = user_conn.cursor()
        
        cursor.execute('''
            SELECT user_id, username, display_name, rank, division, roles, join_date
            FROM discord_members 
            WHERE user_id = ?
            ORDER BY snapshot_date DESC
            LIMIT 1
        ''', (discord_id,))
        
        user = cursor.fetchone()
        
        if not user:
            user_conn.close()
            print(f"Discord ID {discord_id} not found in database")
            return jsonify({
                'error': 'Not a Sol Provision member',
                'message': 'You must be a member of the Sol Provision Discord server.'
            }), 403

        # The dev deployment is restricted to ranks 4+. Reject the login
        # outright (no session) so the overlay shows the denial message.
        if is_dev and rank_int(user['rank']) < 4:
            user_conn.close()
            return jsonify({
                'error': 'Not authorized',
                'message': DEV_AREA_DENIED
            }), 403

        # Update last login timestamp
        cursor.execute('''
            UPDATE discord_members 
            SET last_login = CURRENT_TIMESTAMP 
            WHERE user_id = ?
        ''', (discord_id,))
        user_conn.commit()
        user_conn.close()
        
        # Use display_name as callsign
        callsign = user['display_name']
        
        # Set session with rich user data
        session['discord_id'] = discord_id
        session['username'] = user['username']
        session['callsign'] = callsign
        session['rank'] = user['rank']
        session['division'] = user['division']
        session.permanent = True
        
        return jsonify({
            'discord_id': discord_id,
            'username': user['username'],
            'callsign': callsign,
            'rank': user['rank'],
            'division': user['division'],
            'join_date': user['join_date'],
            'verified': True
        })
        
    except Exception as e:
        print(f"Auth verification error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 401


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """Logout current user"""
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/me', methods=['GET'])
def get_current_user():
    """Get current logged-in user info"""
    discord_id = session.get('discord_id')
    if not discord_id:
        return jsonify({'error': 'Not authenticated'}), 401
    
    return jsonify({
        'discord_id': discord_id,
        'username': session.get('username'),
        'callsign': session.get('callsign'),
        'rank': session.get('rank'),
        'division': session.get('division')
    })


# ══════════════════════════════════════════════════════════════════════
# DATA API ROUTES
# ══════════════════════════════════════════════════════════════════════
@app.route("/api/meta")
def api_meta():
    conn = get_db(); p = PATCH or latest_patch(conn)
    row = conn.execute("SELECT * FROM patch_history WHERE patch_version=?", (p,)).fetchone()
    conn.close()
    return jsonify({"patch_version":p,
                    "total_ships":    row["total_ships"]    if row else 0,
                    "total_entities": row["total_entities"] if row else 0,
                    "imported_at":    row["imported_at"]    if row else "",
                    "environment":    row["environment"]    if row else "live",
                    "game_version":   row["game_version"]   if row else None})

@app.route("/api/patches")
def api_patches():
    """All imported patches with metadata — powers the patch selector UI."""
    conn = get_db()
    rows = conn.execute(
        "SELECT patch_version, environment, game_version, imported_at, "
        "       total_entities, total_ships, total_cargo_grids "
        "FROM patch_history ORDER BY imported_at DESC"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "patch_version":   r["patch_version"],
            "environment":     r["environment"] or "live",
            "game_version":    r["game_version"],
            "imported_at":     r["imported_at"],
            "total_entities":  r["total_entities"],
            "total_ships":     r["total_ships"],
            "total_cargo_grids": r["total_cargo_grids"],
        })
    return jsonify(result)

@app.route("/api/patchnotes")
def api_patchnotes():
    """Diff records between two patches, grouped and summarized."""
    patch_from = request.args.get("from")
    patch_to   = request.args.get("to")
    category   = request.args.get("category")
    limit      = request.args.get("limit", 2000, type=int)

    if not patch_from or not patch_to:
        # Default: oldest live as from, newest as to
        conn = get_db()
        patches = conn.execute(
            "SELECT patch_version FROM patch_history ORDER BY imported_at ASC"
        ).fetchall()
        conn.close()
        if len(patches) < 2:
            return jsonify({"error": "Need at least 2 imported patches to diff"}), 400
        patch_from = patches[0]["patch_version"]
        patch_to   = patches[-1]["patch_version"]

    conn = get_db()

    # changes = only modified field-level changes, joined with display_name from entities
    sql = """
        SELECT pd.entity_name, pd.category, pd.field_path,
               pd.old_value, pd.new_value, pd.change_type,
               COALESCE(e.display_name, s.display_name) as display_name
        FROM patch_diffs pd
        LEFT JOIN entities e ON e.entity_name=pd.entity_name AND e.patch_version=?
        LEFT JOIN ships    s ON s.entity_name=pd.entity_name AND s.patch_version=?
        WHERE pd.patch_from=? AND pd.patch_to=?
          AND pd.field_path != '_entity'
          AND pd.change_type = 'modified'
    """
    params = [patch_to, patch_to, patch_from, patch_to]
    if category:
        sql += " AND pd.category LIKE ?"
        params.append(f"%{category}%")
    sql += " ORDER BY pd.category, pd.entity_name, pd.field_path LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    # Entity-level adds with display_name
    added = conn.execute(
        """SELECT pd.entity_name, pd.category,
                  COALESCE(e.display_name, s.display_name) as display_name
           FROM patch_diffs pd
           LEFT JOIN entities e ON e.entity_name=pd.entity_name AND e.patch_version=?
           LEFT JOIN ships    s ON s.entity_name=pd.entity_name AND s.patch_version=?
           WHERE pd.patch_from=? AND pd.patch_to=?
             AND pd.change_type='added' AND pd.field_path='_entity'""",
        (patch_to, patch_to, patch_from, patch_to)
    ).fetchall()

    # Entity-level removes — join against patch_from since they don't exist in patch_to
    removed = conn.execute(
        """SELECT pd.entity_name, pd.category,
                  COALESCE(e.display_name, s.display_name) as display_name
           FROM patch_diffs pd
           LEFT JOIN entities e ON e.entity_name=pd.entity_name AND e.patch_version=?
           LEFT JOIN ships    s ON s.entity_name=pd.entity_name AND s.patch_version=?
           WHERE pd.patch_from=? AND pd.patch_to=?
             AND pd.change_type='removed' AND pd.field_path='_entity'""",
        (patch_from, patch_from, patch_from, patch_to)
    ).fetchall()

    # Count of distinct entities with modifications
    modified_count = conn.execute(
        "SELECT COUNT(DISTINCT entity_name) as n FROM patch_diffs "
        "WHERE patch_from=? AND patch_to=? AND change_type='modified' AND field_path != '_entity'",
        (patch_from, patch_to)
    ).fetchone()["n"]

    conn.close()
    return jsonify({
        "patch_from":     patch_from,
        "patch_to":       patch_to,
        "changes":        [dict(r) for r in rows],
        "added":          [dict(r) for r in added],
        "removed":        [dict(r) for r in removed],
        "modified_count": modified_count,
        "total_changes":  len(rows),
    })

@app.route("/api/counts")
def api_counts():
    conn = get_db()
    p = PATCH or latest_patch(conn)
    def cnt(tbl, where="", params=()):
        sql = f"SELECT COUNT(*) as n FROM {tbl} WHERE patch_version=?"
        if where: sql += f" AND {where}"
        return conn.execute(sql, (p,)+params).fetchone()["n"]
    ships = conn.execute(
        "SELECT COUNT(*) as n FROM ships s "
        "JOIN ships_index si ON si.entity_name = s.entity_name "
        "WHERE s.patch_version = ?", (p,)
    ).fetchone()["n"]
    result = {
        "ships":        ships,
        "components":   cnt("entities", "item_type IN ('Shield','Cooler','QuantumDrive','PowerPlant')"),
        "ship_weapons": cnt("entities", "item_type IN ('WeaponGun','WeaponMissile','WeaponDefensive')"),
        "fps_weapons":  cnt("entities", "item_type='WeaponPersonal' AND category LIKE '%fps_weapons%'"),
        "armor":        cnt("entities", "item_type LIKE 'Char_Armor%' AND category LIKE '%pu_armor%'"),
        "hardpoints":   cnt("ship_hardpoints"),
        "cargo_grids":  cnt("cargo_grids"),
        "entities":     cnt("entities"),
        "blueprints":   conn.execute("SELECT COUNT(*) FROM crafting_blueprints WHERE patch_version = ?", (p,)).fetchone()[0],
    }
    conn.close(); return jsonify(result)


# ── Mining signatures ────────────────────────────────────────────────────────
@app.route("/api/mining-signatures")
def api_mining_signatures():
    """Mineable-rock radar signatures for the latest patch.

    Returns one row per (resource, source_type) — collapses asteroid+surface
    duplicates from the underlying entity table since they share a signature.
    """
    conn = get_db()
    p = PATCH or latest_patch(conn)
    rows = conn.execute("""
        SELECT
            resource_key,
            MIN(resource_name) AS resource_name,
            source_type,
            MIN(rarity)        AS rarity,
            MIN(rock_type)     AS rock_type,
            MIN(signature)     AS signature,
            COUNT(*)           AS variant_count
        FROM mining_signatures
        WHERE patch_version = ?
        GROUP BY resource_key, source_type, rarity, rock_type
        ORDER BY signature ASC, resource_name ASC
    """, (p,)).fetchall()
    conn.close()
    return jsonify({
        "patch_version": p,
        "signatures":    [dict(r) for r in rows],
    })


# ── Mission reputation ───────────────────────────────────────────────────────
#
# Replaces the legacy static /static/data/mission_rep.json that
# make_mission_rep_json.py used to produce. Same response shape, but rolled up
# server-side from the dataforge_missions.py tables:
#
#   missions
#   mission_reputation_rewards     -> joined to mission_reputation_scopes
#                                                + mission_faction_reputations
#   mission_reputation_requirements -> joined to mission_reputation_scopes
#                                                 + mission_reputation_standings
#
# A mission's rep block lists every (faction, scope, outcome) contribution. We
# pick a "primary" career scope via MISSION_CAREER_MAP (named scopes the player
# actually progresses on) and roll the rest into `extras`. This mirrors the
# rollup that lived in the old make_mission_rep_json.py CAREER_MAP.

# Scope name -> friendly career bucket shown in the UI / filters. Anything not
# in here is treated as a secondary "extra" rep impact (Affinity, Faction_Band,
# etc. fire on almost every mission and would dominate the table).
MISSION_CAREER_MAP = {
    "Hauling":                          "Hauling",
    "Courier":                          "Hauling",
    "Courier_TransportGuild":           "Hauling",
    "Delivery_CitizensForPyro":         "Hauling",
    "Delivery_RoughAndReady":           "Hauling",
    "BountyHunter":                     "Bounty Hunting",
    "BountyHunter_BountyHuntersGuild":  "Bounty Hunting",
    "HiredMuscle":                      "Mercenary",
    "Assassination":                    "Mercenary",
    "Security":                         "Security",
    "Security_MercenaryGuild":          "Security",
    "Emergency":                        "Emergency Services",
    "FPS_Combat":                       "PvE Combat",
    "FPS_Combat_XenoThreat":            "PvE Combat",
    "FPS_Combat_CitizensForPyro":       "PvE Combat",
    "FPS_Combat_HeadHunter":            "PvE Combat",
    "FPS_Combat_RoughAndReady":         "PvE Combat",
    "ShipCombat":                       "Ship Combat",
    "ShipCombat_XenoThreat":            "Ship Combat",
    "ShipCombat_HeadHunters":           "Ship Combat",
    "ShipCombat_RoughAndReady":         "Ship Combat",
    "Salvaging":                        "Salvage",
    "Technician":                       "Repair",
    "Maintenance":                      "Repair",
    "HandyMan":                         "Handyman",
    "HandyMan_CitizensForPyro":         "Handyman",
    "RacingShip":                       "Racing",
    "Racing_HeadHunter":                "Racing",
    "HoverTimeTrial":                   "Racing",
    "WheeledTimeTrial":                 "Racing",
    "Smuggling":                        "Smuggling",
    "Theft":                            "Theft",
    "Wikelo":                           "Wikelo",
    "Worker":                           "Worker",
    "Worker_RoughAndReady":             "Worker",
}


@app.route("/api/mission-rep")
def api_mission_rep():
    """PU mission reputation/UEC data for the latest patch.

    One record per mission with the primary-career rep rolled to top-level
    (rep_s/rep_f) and other contributions in `extras`.
    """
    conn = get_db()
    p = PATCH or latest_patch(conn)

    missions = conn.execute("""
        SELECT entry_uuid, file_path, title_resolved, desc_resolved,
               mission_giver_resolved, lawful, uec_reward, currency_type,
               difficulty_tier
        FROM missions
        WHERE patch_version = ?
    """, (p,)).fetchall()

    rewards = conn.execute("""
        SELECT mrr.entry_uuid, mrr.outcome, mrr.outcome_index,
               s.scope_name, f.display_resolved AS faction_name,
               mrr.rep_change
        FROM mission_reputation_rewards mrr
        LEFT JOIN mission_reputation_scopes s
               ON s.scope_uuid = mrr.scope_uuid
              AND s.patch_version = mrr.patch_version
        LEFT JOIN mission_faction_reputations f
               ON f.faction_uuid = mrr.faction_uuid
              AND f.patch_version = mrr.patch_version
        WHERE mrr.patch_version = ?
    """, (p,)).fetchall()

    requirements = conn.execute("""
        SELECT mreq.entry_uuid, s.scope_name,
               f.display_resolved AS faction_name,
               mreq.comparison,
               st.display_resolved AS standing_name,
               st.min_reputation
        FROM mission_reputation_requirements mreq
        LEFT JOIN mission_reputation_scopes s
               ON s.scope_uuid = mreq.scope_uuid
              AND s.patch_version = mreq.patch_version
        LEFT JOIN mission_faction_reputations f
               ON f.faction_uuid = mreq.faction_uuid
              AND f.patch_version = mreq.patch_version
        LEFT JOIN mission_reputation_standings st
               ON st.standing_uuid = mreq.standing_uuid
              AND st.patch_version = mreq.patch_version
        WHERE mreq.patch_version = ?
    """, (p,)).fetchall()
    conn.close()

    # ── Group rewards per mission ────────────────────────────────────────
    # rewards_by_mission[entry_uuid][scope_name] = {
    #     "fac": faction_name,
    #     "rep_s": int|None,         # rep on Success outcome
    #     "rep_f": int|None,         # rep on Failure outcome
    # }
    rewards_by_mission: dict[str, dict[str, dict]] = {}
    for r in rewards:
        scope = r["scope_name"]
        if not scope:
            continue
        m = rewards_by_mission.setdefault(r["entry_uuid"], {})
        entry = m.setdefault(scope, {"fac": r["faction_name"],
                                     "rep_s": None, "rep_f": None})
        rep = r["rep_change"]
        if r["outcome"] == "Success" and entry["rep_s"] is None:
            entry["rep_s"] = rep
        elif r["outcome"] == "Failure" and entry["rep_f"] is None:
            entry["rep_f"] = rep
        # Outcome3+ rows are kept on the raw table but not surfaced here.

    # ── Group requirements per mission ───────────────────────────────────
    reqs_by_mission: dict[str, list[dict]] = {}
    for r in requirements:
        reqs_by_mission.setdefault(r["entry_uuid"], []).append({
            "scope":      r["scope_name"],
            "fac":        r["faction_name"],
            "comparison": r["comparison"],
            "standing":   r["standing_name"],
            "min_rep":    r["min_reputation"],
        })

    # ── Build output records ─────────────────────────────────────────────
    out = []
    skipped_no_career = 0
    for m in missions:
        scope_rows = rewards_by_mission.get(m["entry_uuid"], {})

        primary_scope = None
        for scope in scope_rows:
            if scope in MISSION_CAREER_MAP:
                primary_scope = scope
                break

        if primary_scope is None:
            # Affinity-only / no-career mission — same skip behavior as the
            # legacy JSON. We could surface them under "Other" if useful.
            skipped_no_career += 1
            continue

        primary = scope_rows[primary_scope]
        extras = []
        for scope, data in scope_rows.items():
            if scope == primary_scope:
                continue
            extras.append({
                "scope": scope,
                "fac":   data["fac"],
                "rep_s": data["rep_s"],
                "rep_f": data["rep_f"],
            })

        # Pick a requirement that matches the primary scope if possible.
        reqs = reqs_by_mission.get(m["entry_uuid"], [])
        primary_req = next((r for r in reqs if r["scope"] == primary_scope), None)
        if primary_req is None:
            primary_req = next((r for r in reqs if r["scope"] != "Affinity"), None)
        if primary_req is None and reqs:
            primary_req = reqs[0]

        out.append({
            "f":        m["file_path"],
            "t":        m["title_resolved"],
            "desc":     m["desc_resolved"],
            "g":        m["mission_giver_resolved"],
            "law":      1 if m["lawful"] == 1 else 0,
            "uec":      m["uec_reward"] or 0,
            "currency": m["currency_type"],
            "career":   MISSION_CAREER_MAP[primary_scope],
            "scope":    primary_scope,
            "fac":      primary["fac"],
            "rep_s":    primary["rep_s"],
            "rep_f":    primary["rep_f"],
            "tier":     m["difficulty_tier"],
            "min_rank": primary_req["standing"] if primary_req else None,
            "min_rep":  primary_req["min_rep"] if primary_req else None,
            "extras":   extras,
        })

    out.sort(key=lambda x: (x["career"], -(x["rep_s"] or 0)))

    return jsonify({
        "patch":              p,
        "missions":           out,
        "skipped_no_career":  skipped_no_career,
    })


# ── Ships ─────────────────────────────────────────────────────────────────────
@app.route("/api/careers")
def api_careers():
    conn = get_db(); p = PATCH or latest_patch(conn)
    rows = conn.execute(
        "SELECT DISTINCT career FROM ships WHERE patch_version=? AND career IS NOT NULL ORDER BY career", (p,)
    ).fetchall()
    conn.close()
    return jsonify([clean_career(r["career"]) for r in rows if r["career"]])

@app.route("/api/ships")
def api_ships():
    conn = get_db(); p = PATCH or latest_patch(conn)
    search   = request.args.get("search","").lower()
    career   = request.args.get("career","")
    min_scu  = request.args.get("min_scu",  type=float)
    max_crew = request.args.get("max_crew", type=int)
    sort_by  = request.args.get("sort","entity_name")
    limit    = request.args.get("limit", 500, type=int)
    if sort_by not in {"entity_name","cargo_scu","crew_size","length_m","length_rsi_m","career","display_name"}:
        sort_by = "entity_name"
    sort_by = f"s.{sort_by}"

    # ── Master catalog path (RSI matrix) — all ships incl. concepts. ──────────
    # Falls back to the legacy ships_index path below if the catalog isn't in
    # this DB yet (so the app can deploy before the catalog-bearing DB lands).
    have_catalog = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ship_catalog'").fetchone() \
        and conn.execute(
            "SELECT 1 FROM ship_catalog WHERE patch_version=? LIMIT 1", (p,)).fetchone()
    if have_catalog:
        crows = conn.execute("""
            SELECT sc.rsi_id, sc.rsi_name, sc.manufacturer, sc.focus, sc.type,
                   sc.production_status, sc.flyable, sc.data_name,
                   sc.size AS rsi_size, sc.cargo_scu AS rsi_cargo,
                   sc.length_m AS rsi_length, sc.max_crew,
                   s.display_name AS game_name, s.career AS game_career,
                   s.crew_size AS game_crew, s.cargo_scu AS game_cargo,
                   s.length_m AS game_length, s.size_class,
                   COALESCE(vr.display_name, s.role) AS game_role
            FROM ship_catalog sc
            LEFT JOIN ships s ON s.entity_name = sc.data_name AND s.patch_version = ?
            LEFT JOIN vehicle_roles vr ON vr.role_key = s.role
            WHERE sc.patch_version = ?
            ORDER BY sc.manufacturer, sc.rsi_name
        """, (p, p)).fetchall()
        conn.close()
        out = []
        for r in crows:
            disp = r["rsi_name"] or r["game_name"] or ""
            if search and search not in disp.lower() \
               and search not in (r["manufacturer"] or "").lower():
                continue
            flyable = bool(r["flyable"])
            out.append({
                "rsi_id":            r["rsi_id"],
                "entity_name":       r["data_name"],          # null for concepts
                "display_name":      disp,
                "manufacturer":      r["manufacturer"],
                "flyable":           flyable,
                "production_status": r["production_status"],
                # RSI classification (present for every ship, incl. concepts)
                "type":              r["type"],
                "focus":             r["focus"],
                "size":              r["rsi_size"],
                # game-data fields when flyable (else RSI/None)
                "career":            r["game_career"],
                "role":              r["game_role"],
                "size_class":        r["size_class"],
                "crew_size":         r["game_crew"] if flyable else r["max_crew"],
                "cargo_scu":         r["rsi_cargo"] if r["rsi_cargo"] is not None else r["game_cargo"],
                "length_m":          r["game_length"] if flyable else r["rsi_length"],
            })
        return jsonify(out[:limit])

    sql = f"""SELECT s.uuid, s.entity_name, s.display_name, s.vehicle_name,
                 s.career, COALESCE(vr.display_name, s.role) AS role, s.crew_size, s.cargo_scu,
                 s.rsi_cargo_scu,
                 s.size_class,
                 s.length_m, s.beam_m, s.height_m,
                 s.length_rsi_m, s.beam_rsi_m, s.height_rsi_m,
                 s.rsi_name, s.rsi_url
          FROM ships s
          JOIN ships_index si ON si.entity_name = s.entity_name
          LEFT JOIN vehicle_roles vr ON vr.role_key = s.role
          WHERE s.patch_version = ?"""
    params = [p]
    if career:   sql += " AND s.career LIKE ?";   params.append(f"%{career}%")
    if min_scu:  sql += " AND s.cargo_scu >= ?";  params.append(min_scu)
    if max_crew: sql += " AND s.crew_size <= ?";  params.append(max_crew)
    sql += f" ORDER BY {sort_by} NULLS LAST LIMIT ?"; params.append(limit)

    rows = conn.execute(sql, params).fetchall(); conn.close()
    result = []
    for r in rows:
        name = r["entity_name"]
        disp = best_name(r["display_name"], name)
        # Search across both entity name and display name
        if search and search not in name.lower() and search not in disp.lower():
            continue
        result.append({
            "uuid":         r["uuid"],
            "entity_name":  name,
            "display_name": disp,
            "manufacturer": get_mfr(name),
            "career":       clean_career(r["career"] or ""),
            "role":         r["role"],
            "size_class":   r["size_class"],
            "crew_size":    r["crew_size"],
            # RSI published spec is authoritative — including a published 0
            # (combat/mining/special hulls RSI rates at 0 cargo). Only fall
            # back to the in-game value when RSI has no row at all (NULL).
            "cargo_scu":    (r["rsi_cargo_scu"] if r["rsi_cargo_scu"] is not None else r["cargo_scu"]),
            "cargo_game_scu": r["cargo_scu"],
            "cargo_rsi_scu":  r["rsi_cargo_scu"],
            "length_m":     r["length_m"],
            "beam_m":       r["beam_m"],
            "height_m":     r["height_m"],
            "length_rsi_m": r["length_rsi_m"],
            "beam_rsi_m":   r["beam_rsi_m"],
            "height_rsi_m": r["height_rsi_m"],
            "rsi_name":     r["rsi_name"],
            "rsi_url":      r["rsi_url"],
        })
    return jsonify(result)

def get_ship_components(conn, ship_entity, patch):
    """
    Pull all installed component stats for a ship.
    Join: ship_hardpoints.installed_name → item_components.entity_name → item_* tables.
    Returns a dict keyed by component category.
    """
    def q(sql):
        rows = []
        for r in conn.execute(sql, {"ship": ship_entity, "patch": patch}).fetchall():
            d = dict(r)
            if is_placeholder(d.get("display_name")):
                continue
            # Bespoke ship items (e.g. RADR_RSI_S04_Polaris) carry a loc_name_key
            # CIG never shipped a string for, so display_name comes back blank.
            # Fall back to a formatted entity name — same pattern as ship names.
            if not (d.get("display_name") or "").strip():
                d["display_name"] = best_name(d.get("display_name"), d.get("entity_name"))
            rows.append(d)
        return rows

    # Reusable join fragment — all component queries share this structure
    def join(tbl):
        return f"""
        FROM ship_hardpoints sh
        JOIN item_components ic
          ON LOWER(ic.entity_name) = LOWER(sh.installed_name)
         AND ic.patch_version = :patch
        JOIN {tbl} t
          ON t.uuid = ic.uuid
         AND t.patch_version = :patch
        WHERE sh.ship_entity_name = :ship
          AND sh.patch_version    = :patch"""

    armor = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.signal_cross_section, t.signal_electromagnetic, t.signal_infrared,
               t.dmg_physical, t.dmg_energy, t.dmg_distortion,
               t.dmg_thermal, t.dmg_biochemical, t.dmg_stun,
               t.deflect_physical, t.deflect_energy, t.deflect_distortion,
               t.deflect_thermal, t.deflect_biochemical, t.deflect_stun,
               t.res_physical, t.res_energy, t.res_distortion,
               t.res_thermal, t.res_biochemical, t.res_stun,
               t.health
        {join("item_armor")}""")

    shields = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.max_shield_health, t.max_shield_regen,
               t.damaged_regen_delay, t.downed_regen_delay,
               t.decay_ratio, t.reserve_drain_ratio,
               t.absorb_physical_min, t.absorb_physical_max,
               t.absorb_energy_min,   t.absorb_energy_max,
               t.resist_physical_min, t.resist_physical_max,
               t.resist_energy_min,   t.resist_energy_max,
               t.resist_distort_min,  t.resist_distort_max,
               t.absorb_distort_min,  t.absorb_distort_max,
               t.resist_physical_min, t.resist_physical_max,
               t.resist_energy_min,   t.resist_energy_max,
               t.resist_distort_min,  t.resist_distort_max,
               t.power_draw, t.em_signature, t.health,
               t.power_low, t.power_medium, t.power_high
        {join("item_shields")}""")

    coolers = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.power_draw, t.cooling_output,
               t.em_signature, t.ir_signature, t.health,
               t.power_low, t.power_medium, t.power_high
        {join("item_coolers")}""")

    powerplants = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.power_output, t.em_signature, t.health,
               t.power_low, t.power_medium, t.power_high
        {join("item_powerplants")}""")

    quantum_drives = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.drive_speed / 1000 as drive_speed, t.stage_one_accel_mps2 as accel1,
               t.stage_two_accel_mps2 as accel2,t.spool_up_time, t.cooldown_time,
               t.calibration_rate, t.calibration_delay,
               t.fuel_per_gm_mscu, t.power_draw,
               t.em_signature, t.health,
               t.power_low, t.power_medium, t.power_high
        {join("item_quantum_drives")}""")

    fuel_tanks = q(f"""
        SELECT ic.entity_name, ic.display_name, t.capacity_scu
        {join("item_fuel_tanks")}""")

    quantum_fuel_tanks = q(f"""
        SELECT ic.entity_name, ic.display_name, t.capacity_scu
        {join("item_quantum_fuel_tanks")}""")

    # Flight controllers: exclude SPD/HND blade variants (alternate tuning, not separate installs)
    flight_controllers = q(f"""
        SELECT ic.entity_name, ic.display_name,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.scm_speed, t.max_speed,
               t.boost_speed_forward, t.boost_speed_backward,
               t.max_pitch_speed, t.max_roll_speed, t.max_yaw_speed,
               t.afterburner_ramp_up, t.afterburner_ramp_down,
               t.ab_ang_mult_pitch, t.ab_ang_mult_roll, t.ab_ang_mult_yaw,
               t.ab_accel_mult_fwd, t.spool_up_time, t.power_draw,
               t.power_low, t.power_medium, t.power_high
        {join("item_flight_controllers")}
        AND ic.entity_name NOT LIKE '%_blade_spd'
        AND ic.entity_name NOT LIKE '%_blade_hnd'""")

    # Thrusters: group by type for cleaner frontend display
    thrusters_raw = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size,
               t.thruster_type, t.thrust_capacity, t.thrust_capacity_new,
               t.fuel_burn_rate_per_10k_n, t.only_active_in_vtol
        {join("item_thrusters")}
        ORDER BY t.thruster_type, t.thrust_capacity DESC""")

    thrusters = {}
    for t in thrusters_raw:
        ttype = t.get("thruster_type") or "Unknown"
        thrusters.setdefault(ttype, []).append(t)

    # Weapons: join fire modes as nested list, plus the linked AmmoParams
    # damage block so the frontend can compute DPS/alpha.
    # Note: join() includes the WHERE clause so we can't add a LEFT JOIN
    # after it; instead we fetch ammo separately by UUID per weapon.
    weapons_base = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.item_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.heat_rate_online, t.power_active_cooldown,
               t.overheat_temperature, t.cooling_per_second,
               t.time_till_cooling_starts, t.overheat_fix_time,
               t.max_ammo_load, t.max_regen_per_sec,
               t.regen_cooldown, t.regen_cost_per_bullet,
               t.power_draw, t.power_low, t.power_medium, t.power_high,
               t.ammo_uuid
        {join("item_weapons")}
        ORDER BY ic.size DESC, ic.entity_name""")

    def _enrich_weapon(weapon):
        """Attach fire modes + the linked AmmoParams damage block to a weapon dict
        so the client can compute DPS = total damage × pellet_count × fire_rate."""
        weapon["fire_modes"] = [dict(r) for r in conn.execute("""
            SELECT fire_mode_type, fire_rate, heat_per_shot, heat_per_second,
                   ammo_type, pellet_count, ammo_cost,
                   spread_min, spread_max, spread_attack, spread_decay,
                   full_damage_range, zero_damage_range,
                   charge_time, cooldown_time
            FROM item_weapon_fire_modes
            WHERE uuid = (
                SELECT uuid FROM item_components
                WHERE entity_name = ? AND patch_version = ? LIMIT 1
            )
            ORDER BY fire_mode_type
        """, (weapon["entity_name"], patch)).fetchall()]
        ammo = None
        if weapon.get("ammo_uuid"):
            ar = conn.execute("""
                SELECT entity_name, projectile_type, speed, lifetime,
                       dmg_physical, dmg_energy, dmg_distortion,
                       dmg_thermal, dmg_biochemical, dmg_stun
                FROM item_ammo
                WHERE uuid = ? AND patch_version = ?
            """, (weapon["ammo_uuid"], patch)).fetchone()
            if ar:
                ammo = dict(ar)
        weapon["ammo"] = ammo
        return weapon

    weapons = [_enrich_weapon(dict(w)) for w in weapons_base]

    # ── Hierarchical weapon groups: hardpoint → mount → weapon ────────────────
    # The flat `weapons` list above loses the hardpoint/mount structure. Walk
    # ship_hardpoints (parent_port chains) so the UI can render each weapon
    # hardpoint, its installed mount (fixed/gimbal, from item_weapon_mounts), and
    # the nested weapon(s). Roots that contain no weapon/mount are dropped
    # (e.g. pure missile racks — handled by their own column).
    hp_rows = [dict(r) for r in conn.execute("""
        SELECT port_name, parent_port, min_size, max_size, flags, installed_name,
               port_type, accepted_types
        FROM ship_hardpoints
        WHERE ship_entity_name = :ship AND patch_version = :patch
    """, {"ship": ship_entity, "patch": patch}).fetchall()]

    children_map = {}
    for r in hp_rows:
        children_map.setdefault((r["parent_port"] or "").lower(), []).append(r)

    mount_map = {dict(r)["entity_name"].lower(): dict(r) for r in conn.execute(
        "SELECT * FROM item_weapon_mounts WHERE patch_version = ?", (patch,)).fetchall()}
    weapon_map = {w["entity_name"].lower(): w for w in weapons}
    comp_map = {r["entity_name"].lower(): dict(r) for r in conn.execute("""
        SELECT DISTINCT ic.entity_name, ic.display_name, ic.size,
               ic.item_type, ic.item_sub_type
        FROM ship_hardpoints sh
        JOIN item_components ic
          ON LOWER(ic.entity_name) = LOWER(sh.installed_name)
         AND ic.patch_version = :patch
        WHERE sh.ship_entity_name = :ship AND sh.patch_version = :patch
    """, {"ship": ship_entity, "patch": patch}).fetchall()}

    def _has_weapon(node):
        return (node["kind"] in ("weapon", "mount")
                or any(_has_weapon(ch) for ch in node["children"]))

    def _is_salvage_item(item):
        if not item:
            return False
        it = (item.get("item_type") or "").lower()
        if "salvage" in it and "controller" not in it:
            return True
        # Salvage arms are extracted as weapon mounts tagged salvageMount.
        return "salvagemount" in (item.get("tags") or "").lower()

    def _has_salvage(node):
        return (_is_salvage_item(node.get("item"))
                or any(_has_salvage(ch) for ch in node["children"]))

    def _is_mining_item(item):
        if not item:
            return False
        it = (item.get("item_type") or "").lower()
        if it in ("weaponmining", "miningmodifier"):
            return True
        # Mining arms are extracted as weapon mounts tagged miningMount.
        return "miningmount" in (item.get("tags") or "").lower()

    def _has_mining(node):
        return (_is_mining_item(node.get("item"))
                or any(_has_mining(ch) for ch in node["children"]))

    def _build_node(hp, ancestors=()):
        name  = hp["installed_name"]
        lname = (name or "").lower()
        kind, item = "empty", None
        if lname in mount_map:
            kind, item = "mount", mount_map[lname]
        elif lname in weapon_map:
            kind, item = "weapon", weapon_map[lname]
        elif lname in comp_map:
            item = comp_map[lname]
            it = (item.get("item_type") or "").lower()
            kind = ("turret"  if "turret"  in it else
                    "missile" if "missile" in it else "other")
        elif name:
            kind, item = "unknown", {"entity_name": name, "display_name": name}
        cur_port = (hp["port_name"] or "").lower()
        # ship_hardpoints links children to parents by port NAME, which is not
        # unique across the tree: a ship's matching top/bottom turrets both use
        # e.g. hardpoint_weapon_left → hardpoint_class_2, so a namesake parent
        # would otherwise pull in every twin's guns. Dedup children by their own
        # port_name (each physical slot appears once per parent).
        # (Proper fix = unique parent paths in the extractor; tracked as follow-up.)
        # Cycle guard by ROW IDENTITY: the data nests a port inside a same-named
        # parent — mining ships put the mining laser row under the same-named
        # mount port, so the laser legitimately shares its parent's port_name.
        # Keying the guard on port name dropped the laser from the tree; keying
        # on the actual row object only stops a row that is literally its own
        # ancestor (the self-referential hardpoint_mining_laser → itself edge that
        # otherwise recursed forever → 500).
        path = ancestors + (id(hp),)
        seen_ports, child_rows = set(), []
        for ch in children_map.get(cur_port, []):
            if id(ch) in path:
                continue
            key = (ch["port_name"] or "").lower()
            if key in seen_ports:
                continue
            seen_ports.add(key)
            child_rows.append(ch)
        children = [_build_node(ch, path) for ch in child_rows]
        # Drop non-weapon/non-salvage/non-mining child subtrees (MFDs, seats, …).
        children = [c for c in children if _has_weapon(c) or _has_salvage(c) or _has_mining(c)]
        return {
            "port_name":      hp["port_name"],
            "port_type":      hp["port_type"],
            "accepted_types": hp["accepted_types"],
            "min_size":       hp["min_size"],
            "max_size":       hp["max_size"],
            "flags":          hp["flags"],
            "editable":       "uneditable" not in (hp["flags"] or "").lower(),
            "installed_name": name,
            "kind":           kind,
            "item":           item,
            "children":       children,
        }

    def _flatten(nodes):
        # Promote weapon subtrees out of empty container ports (rooms/seats) so
        # each group root is a real turret / mount / weapon, not an empty wrapper.
        out = []
        for n in nodes:
            if n["kind"] == "empty":
                out.extend(_flatten(n["children"]))
            else:
                out.append(n)
        return out

    tops = [_build_node(r) for r in hp_rows if not (r["parent_port"] or "")]

    # Pilot weapon vs crewed/remote/PDC turret — decided by the port's accepted
    # <Types> (extracted into ship_hardpoints), the exact in-game signal:
    #   TurretBase:*                          → crewed turret
    #   Turret:* / UtilityTurret:* (no WeaponGun) → remote/PDC turret
    #   anything accepting WeaponGun          → pilot-fireable weapon
    # (Pilot gimbal mounts also use port_type 'turret' but their accepted_types
    # include WeaponGun, so they correctly stay pilot.)
    def _is_turret_port(n):
        pt  = (n.get("port_type") or "").lower()
        acc = n.get("accepted_types") or ""
        # A crewed/remote turret sits on a TurretBase (needs a seat/base). Plain
        # 'Turret:' mounts (nose/canard/ball/gun turret mounts) are pilot-fired
        # even when their accepted_types omit WeaponGun, so they must NOT be
        # treated as crew turrets — that buried e.g. the Starfarer's side cannons
        # and the Hornet's nose/ball guns in Crew DPS.
        if pt == "turretbase" or "TurretBase:" in acc:
            return True
        return False

    def _turret_class(n):
        # Manned vs remote vs PDC — name-keyword heuristic, default manned.
        nm = ((n["port_name"] or "") + " " + (n["installed_name"] or "")).lower()
        if "pdc" in nm or "point_defense" in nm or "pointdefense" in nm:
            return "pdc"
        if "remote" in nm or "unmanned" in nm:
            return "remote"
        return "manned"

    # A crewed turret can sit one level down inside a room/seat wrapper (kind
    # 'empty') — e.g. the Star Runner's turret seats live under a 'room' port,
    # so classifying only the top node buries the turret's guns in Pilot. Promote
    # through empty non-turret wrappers so the turretbase surfaces for
    # classification, but STOP at turret ports so an empty turret base (whose
    # default guns are foundry-backfilled) still stays its own group root.
    def _expand_wrappers(nodes):
        out = []
        for n in nodes:
            if n["kind"] == "empty" and not _is_turret_port(n):
                out.extend(_expand_wrappers(n["children"]))
            else:
                out.append(n)
        return out
    tops = _expand_wrappers(tops)

    # Turret tops keep their node as the group root (even an empty turret base
    # whose default guns are foundry-backfilled) so all-turret ships like the
    # Hammerhead don't leak into Pilot. Pilot tops are flattened to promote guns
    # out of empty room/seat wrappers.
    pilot_tops, turret_tops, utility_tops, salvage_tops, mining_tops = [], [], [], [], []
    for n in tops:
        # Salvage gear first: salvage arms (toolarm + salvageMount mount) and
        # salvage turrets (subtree holds SalvageHead/Modifier items) belong to
        # the Salvage card, not Weapons/Turrets — mirrors SPViewer's grouping.
        if _has_salvage(n):
            it = (n.get("item") or {}).get("item_type") or ""
            # Filler stations / bare buff ports are placeholder items — the buff
            # surfaces via salvage_buff below; stations have nothing to show.
            if it != "SalvageFillerStation" and "buff" not in (n["port_name"] or "").lower():
                salvage_tops.append(n)
            continue
        # Mining gear: MOLE cabs are utilityturret ports holding mining lasers;
        # Prospector arms are toolarm mounts. Route before the utility branch.
        if _has_mining(n):
            mining_tops.append(n)
            continue
        if not _has_weapon(n):
            continue
        if (n.get("port_type") or "").lower() == "utilityturret":
            utility_tops.append(n)
        elif _is_turret_port(n):
            turret_tops.append(n)
        else:
            pilot_tops.append(n)

    weapon_groups = _flatten(pilot_tops)
    turret_groups = {"manned": [], "remote": [], "pdc": []}
    for n in turret_tops:
        turret_groups[_turret_class(n)].append(n)
    utility_turrets = list(utility_tops)
    salvage_groups  = list(salvage_tops)
    mining_groups   = list(mining_tops)

    _wsort = lambda n: (-(n["max_size"] or 0), n["port_name"] or "")
    weapon_groups.sort(key=_wsort)
    utility_turrets.sort(key=_wsort)
    salvage_groups.sort(key=_wsort)
    mining_groups.sort(key=_wsort)
    for _b in turret_groups.values():
        _b.sort(key=_wsort)

    # Ship-wide salvage buff (e.g. Reclaimer's 10× speed / 2.18× radius /
    # 0.55× efficiency) — installed on an attachable_buff port; values live in
    # item_salvage_modifiers.
    salvage_buff = None
    for r in hp_rows:
        inst = (r["installed_name"] or "").lower()
        if "salvage_buff" in inst or "buff_modifier" in inst:
            row = conn.execute(
                "SELECT entity_name, salvage_speed_multiplier, radius_multiplier, "
                "       extraction_efficiency "
                "FROM item_salvage_modifiers "
                "WHERE LOWER(entity_name)=LOWER(?) AND patch_version=?",
                (r["installed_name"], patch)).fetchone()
            if row:
                salvage_buff = dict(row)
                break

    # ── Aggregate DPS + alpha: pilot weapons vs turret/PDC guns (excl. missiles) ──
    def _weapon_dps_alpha(item):
        ammo = (item or {}).get("ammo") or {}
        shot = sum((ammo.get(k) or 0) for k in
                   ("dmg_physical", "dmg_energy", "dmg_distortion",
                    "dmg_thermal", "dmg_biochemical", "dmg_stun"))
        if not shot:
            return 0.0, 0.0
        fms = (item or {}).get("fire_modes") or []
        # Charge weapons (Banu Singe/Tachyon cannons) fire one shot per charge
        # cycle; their primary mode is 'Charged' with a charge_time and no
        # fire_rate. Falling through to the fire_rate scan below picks the
        # alternate rapid mode and massively overstates DPS (Singe read 1519 vs
        # spviewer ~319), so handle the charged primary mode explicitly.
        primary = fms[0] if fms else None
        if primary and (primary.get("fire_mode_type") or "").lower() == "charged" \
                and (primary.get("charge_time") or 0) > 0:
            alpha = shot * (primary.get("pellet_count") or 1)
            cycle = primary["charge_time"] + (primary.get("cooldown_time") or 0)
            return (alpha / cycle if cycle else 0.0), alpha
        fm = next((f for f in fms if (f.get("fire_rate") or 0) > 0), None)
        if not fm:
            return 0.0, 0.0
        alpha = shot * (fm.get("pellet_count") or 1)
        return alpha * (fm["fire_rate"] / 60.0), alpha

    # Utility tools (tractor/tow beams, tool arms, mining/salvage heads) are not
    # weapons. Some carry a phantom default gun in a nested class slot (a foundry
    # backfill artifact), so their subtree must be skipped entirely or those guns
    # inflate weapon DPS — e.g. the Hull C's tractor-beam turrets each nest a
    # spurious Panther. spviewer excludes them.
    _UTILITY_ITEM_TYPES = {"tractorbeam", "towbeam", "toolarm",
                           "mininglaser", "salvagehead", "salvagemodifier"}

    def _sum_dps_alpha(roots):
        dps, alpha, stack = 0.0, 0.0, list(roots)
        while stack:
            n = stack.pop()
            it = n.get("item") or {}
            if (it.get("item_type") or "").lower() in _UTILITY_ITEM_TYPES:
                continue  # utility tool — skip its whole subtree (phantom guns)
            kids = n.get("children") or []
            # Only count LEAF guns. Some hardpoints resolve the same weapon on both
            # the port and a nested class-slot child (e.g. Sabre Firebird's wing
            # Mantis appears as parent AND child) — counting the parent too would
            # double the DPS. A weapon node with a weapon child is acting as a
            # mount, so skip it and count the child.
            if n.get("kind") == "weapon" and not any(c.get("kind") == "weapon" for c in kids):
                d, a = _weapon_dps_alpha(it)
                dps += d; alpha += a
            stack.extend(kids)
        return dps, alpha

    _turret_roots = (turret_groups["manned"] + turret_groups["remote"]
                     + turret_groups["pdc"])
    _pd, _pa = _sum_dps_alpha(weapon_groups)
    _td, _ta = _sum_dps_alpha(_turret_roots)
    pilot_dps,  pilot_alpha  = round(_pd), round(_pa)
    turret_dps, turret_alpha = round(_td), round(_ta)

    # Missile racks with their missile type looked up from item_missiles
    missile_racks = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.launch_delay, t.detach_velocity_forward,
               t.detach_velocity_right, t.detach_velocity_up,
               t.rack_tag
        {join("item_missile_racks")}""")

    # Missiles installed directly (GMISL_ entities on hardpoints)
    missiles = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.arm_time, t.max_lifetime,
               t.dmg_physical, t.dmg_energy, t.dmg_distortion,
               t.dmg_thermal, t.dmg_biochemical, t.dmg_stun,
               t.linear_speed, t.fuel_tank_size,
               t.lock_range_max, t.lock_range_min,
               t.lock_time, t.locking_angle, t.tracking_signal_type,
               t.ordnance_type, t.is_dumbfire,
               t.blast_radius_min, t.blast_radius_max, t.phys_radius_max
        {join("item_missiles")}""")

    # Missiles nested under their parent rack. Each missilelauncher port installs
    # a rack; the rack's child *_attach ports each hold one missile, so identical
    # missiles under a rack collapse into one row with a count (×2, ×8, …).
    _rack_by_name = {r["entity_name"].lower(): r for r in missile_racks}
    _missile_by_name = {m["entity_name"].lower(): m for m in missiles}
    missile_groups = []
    for r in hp_rows:
        if (r["port_type"] or "").lower() not in ("missilelauncher", "bomblauncher"):
            continue
        counts, order = {}, []
        for ch in children_map.get((r["port_name"] or "").lower(), []):
            mi = _missile_by_name.get((ch["installed_name"] or "").lower())
            if not mi:
                continue
            key = mi["entity_name"].lower()
            if key not in counts:
                counts[key] = {"missile": mi, "count": 0}
                order.append(key)
            counts[key]["count"] += 1
        if not order:
            continue
        rack_item = _rack_by_name.get((r["installed_name"] or "").lower())
        missile_groups.append({
            "port_name":      r["port_name"],
            "size":           r["max_size"],
            "installed_name": r["installed_name"],
            "rack":           rack_item,
            "missiles":       [counts[k] for k in order],
        })
    missile_groups.sort(key=lambda g: (-(g["size"] or 0), g["port_name"] or ""))

    radars = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.power_draw, t.em_signature, t.health, t.aim_assist_min_m,
               t.aim_assist_max_m, t.shutdown_dmg, t.decay_delay_sec, t.decay_rate,
               t.shutdown_time_sec, t.ir_sensitivity, t.em_sensitivity, t.cs_sensitivity, t.db_sensitivity, t.rs_sensitivity,
               t.power_low, t.power_medium, t.power_high
        {join("item_radars")}""")
        
    external_fuel_tanks = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description,
               t.capacity_scu, t.hydrogen_flow_mult, t.quantum_flow_mult,
               t.health
        {join("item_external_fuel_tanks")}""")

    # Fuel nozzles (refueling nozzles, including dockingport variants)
    fuel_nozzles = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description,
               t.hydrogen_flow_rate, t.quantum_flow_rate,
               t.health
        {join("item_fuel_nozzles")}""")

    # Life support (declared on the ship's foundry record, backfilled into
    # ship_hardpoints by dataforge_foundry_loadouts.py).
    lifesupport = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.power_draw, t.lifesupport_output,
               t.em_signature, t.ir_signature, t.health,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_lifesupport")}""")

    # Salvage components: heads, scraper/buff modifiers, and per-ship filler
    # stations. All three live in item_salvage with a salvage_type column.
    # Also catches weapon-mount tractor/towing beams (wep_tractorbeam_*)
    # which are typed SalvageHead but live under ships/weapons/.
    salvage = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.salvage_type, t.power_draw,
               t.salvage_speed_multiplier, t.radius_multiplier, t.extraction_efficiency,
               t.em_signature, t.ir_signature, t.health,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_salvage")}""")

    # EMP devices (Mantis, Hawk, Vanguard Sentinel, Scorpius variant).
    emp = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.charge_time, t.unleash_time, t.cooldown_time,
               t.distortion_damage,
               t.emp_radius, t.min_emp_radius,
               t.phys_radius, t.min_phys_radius, t.pressure,
               t.em_signature, t.ir_signature, t.health,
               t.power_draw,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_emp")}""")

    # QED — Quantum Enforcement Devices (interdictors).
    qed = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.base_power_draw_fraction, t.pulse_power_fraction, t.jammer_power_fraction,
               t.charge_time_secs, t.discharge_time_secs, t.cooldown_time_secs,
               t.radius_meters, t.max_power_draw,
               t.active_power_draw_fraction, t.tethering_power_draw_fraction,
               t.green_zone_check_range,
               t.em_signature, t.ir_signature, t.health,
               t.power_draw,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_qed")}""")

    # Tool arm mounts (tractor + mining arms). Structural — the actual
    # power-bearing tool installs into the arm's hardpoint. tool_kind
    # discriminates 'tractor' vs 'mining'.
    tool_arms = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.tool_kind, t.ignore_warmup_cooldown,
               t.em_signature, t.ir_signature, t.health,
               t.power_draw,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_tool_arms")}""")

    # Mining lasers / heads (Prospector, MOLE, Golem, handheld). Only ic.*
    # columns are selected so this is robust to the item_mining_lasers schema.
    mining_lasers = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.max_ammo_load, t.overheat_temperature,
               t.mining_dps, t.module_slots, t.module_slot_size,
               t.power_draw, t.em_signature, t.ir_signature
        {join("item_mining_lasers")}""")

    # Ground-vehicle wheels controllers (analog to flight_controllers for ships).
    wheels_controllers = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               ic.heat_baseline, ic.coolant_consumption,
               ic.heat_gen_rate, ic.overheat_temperature, ic.overheat_warning_temp,
               ic.overheat_recovery_temp, ic.min_cooling_temperature,
               ic.cooling_equalization_rate, ic.cooling_equalization_tdiff,
               ic.powered_ambient_cool_mult, ic.overheat_enabled,
               t.minimum_power_amount,
               t.em_signature, t.ir_signature, t.health,
               t.power_draw,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_wheels_controllers")}""")

    # Mining/salvage hardpoint trees carry display-only items (from the generic
    # component map), but their stats (power_draw, em/ir_signature, mining_dps,
    # multipliers) live in the flat mining_lasers / salvage lists. Merge those
    # into the tree node items so the sidebar can derive power/cooling/signature
    # straight from the trees — which is what swaps mutate.
    def _merge_tree_stats(groups, by_name):
        for root in groups:
            stack = [root]
            while stack:
                n = stack.pop()
                it = n.get("item")
                ent = (it or {}).get("entity_name")
                if it and ent:
                    full = by_name.get(ent.lower())
                    if full:
                        for k, v in full.items():
                            it.setdefault(k, v)   # add stats, keep display fields
                stack.extend(n.get("children") or [])

    _merge_tree_stats(mining_groups,
                      {m["entity_name"].lower(): m for m in mining_lasers if m.get("entity_name")})
    _merge_tree_stats(salvage_groups,
                      {s["entity_name"].lower(): s for s in salvage if s.get("entity_name")})

    return {
        "armor":              armor,
        "shields":            shields,
        "coolers":            coolers,
        "powerplants":        powerplants,
        "quantum_drives":     quantum_drives,
        "fuel_tanks":         fuel_tanks,
        "quantum_fuel_tanks": quantum_fuel_tanks,
        "external_fuel_tanks": external_fuel_tanks,
        "fuel_nozzles":        fuel_nozzles,
        "flight_controllers": flight_controllers,
        "thrusters":          thrusters,
        "weapons":            weapons,
        "weapon_groups":      weapon_groups,
        "turret_groups":      turret_groups,
        "utility_turrets":    utility_turrets,
        "pilot_dps":          pilot_dps,
        "pilot_alpha":        pilot_alpha,
        "turret_dps":         turret_dps,
        "turret_alpha":       turret_alpha,
        "missile_racks":      missile_racks,
        "missiles":           missiles,
        "missile_groups":     missile_groups,
        "radars":             radars,
        "lifesupport":        lifesupport,
        "salvage":            salvage,
        "salvage_groups":     salvage_groups,
        "salvage_buff":       salvage_buff,
        "mining_groups":      mining_groups,
        "emp":                emp,
        "qed":                qed,
        "mining_lasers":      mining_lasers,
        "tool_arms":          tool_arms,
        "wheels_controllers": wheels_controllers,
    }


@app.route("/api/ship/<entity_name>")
def api_ship_detail(entity_name):
    conn = get_db(); p = PATCH or latest_patch(conn)
    ship = conn.execute("""
        SELECT s.*, si.lineart_file,
               COALESCE(vr.display_name, s.role) AS role_display
        FROM ships s
        LEFT JOIN ships_index si ON si.entity_name = s.entity_name
        LEFT JOIN vehicle_roles vr ON vr.role_key = s.role
        WHERE s.entity_name = ? AND s.patch_version = ?
    """, (entity_name, p)).fetchone()
    if not ship: conn.close(); return jsonify({"error":"Not found"}), 404
    
    ship_damage_parts = conn.execute(
        """SELECT sdp.part_name, sdp.damage_max
           FROM ship_damage_parts sdp
           WHERE sdp.ship_entity_name=? AND sdp.patch_version=?
        """,(entity_name, p)
    ).fetchall()
    
    ship_parts = {}
    for sp in ship_damage_parts: ship_parts.setdefault(sp["part_name"], []).append(dict(sp))
    hull_hp = sum(row["damage_max"] or 0 for row in ship_damage_parts)

    grids = conn.execute(
        """SELECT cg.entity_name, cg.scu, cg.dim_x, cg.dim_y, cg.dim_z,
                  cg.is_external, cg.is_personal, ic.entity_name as container_name
           FROM cargo_grids cg
           LEFT JOIN inventory_containers ic ON ic.uuid=cg.container_uuid AND ic.patch_version = cg.patch_version
           WHERE cg.ship_entity_name=? AND cg.patch_version=? AND cg.is_template=0
           ORDER BY cg.scu DESC NULLS LAST""",
        (entity_name, p)
    ).fetchall()

    hps = conn.execute(
        """SELECT port_name, port_type, min_size, max_size, flags,
                  is_editable, parent_port, installed_name, installed_uuid
           FROM ship_hardpoints
           WHERE ship_entity_name=? AND patch_version=?
           ORDER BY port_type, max_size DESC NULLS LAST, port_name""",
        (entity_name, p)
    ).fetchall()

    hardpoints = {}
    # Foundry-backfilled rows can have NULL port_type (no per-port type info
    # in the foundry XML). Bucket those under "misc" so the dict keys stay
    # str-only — otherwise Flask's sort_keys=True JSON encoder blows up on
    # str vs None comparison.
    for hp in hps:
        key = hp["port_type"] or "misc"
        hardpoints.setdefault(key, []).append(dict(hp))
    cargo_total = sum(g["scu"] or 0 for g in grids if not g["is_personal"])
    personal    = sum(g["scu"] or 0 for g in grids if g["is_personal"])

    components = get_ship_components(conn, entity_name, p)
    # Catalog rsi_id (the claim key) for this hull, if the catalog is present.
    rsi_id = None
    try:
        # A data_name may back several catalog rows (editions/bundles aliased to
        # a base ship). Prefer the genuine matched base — the rsi_id the matcher
        # stamped onto the ship row — over any alias sharing the same data_name.
        crow = conn.execute(
            """SELECT sc.rsi_id FROM ship_catalog sc
               WHERE sc.data_name = ? AND sc.patch_version = ?
               ORDER BY (sc.rsi_id = (SELECT s.rsi_ship_id FROM ships s
                                      WHERE s.entity_name = ? AND s.patch_version = ?)) DESC,
                        sc.rsi_id ASC
               LIMIT 1""",
            (entity_name, p, entity_name, p)).fetchone()
        if crow:
            rsi_id = crow["rsi_id"]
    except sqlite3.Error:
        pass
    conn.close()

    return jsonify({
        "uuid":                   ship["uuid"],
        "entity_name":            entity_name,
        "rsi_id":                 rsi_id,
        "display_name":           best_name(ship["display_name"], entity_name),
        "manufacturer":           get_mfr(entity_name),
        "career":                 clean_career(ship["career"] or ""),
        "role": ship["role_display"] or clean_role(ship["role"] or ""),
        "crew_size":              ship["crew_size"],
        # Display cargo = RSI published capacity when RSI lists the hull
        # (authoritative, including a published 0 for combat/mining/special
        # ships). Fall back to the in-game value only when RSI has no row
        # (NULL). Both raw values exposed for transparency.
        "cargo_scu":              (ship["rsi_cargo_scu"] if ship["rsi_cargo_scu"] is not None else ship["cargo_scu"]),
        "cargo_game_scu":         ship["cargo_scu"],
        "cargo_rsi_scu":          ship["rsi_cargo_scu"],
        "cargo_total_calculated": round(cargo_total, 1),
        "personal_scu":           round(personal, 1),
        "length_m":               ship["length_m"],
        "beam_m":                 ship["beam_m"],
        "height_m":               ship["height_m"],
        "length_rsi_m":           ship["length_rsi_m"] if "length_rsi_m" in ship.keys() else None,
        "beam_rsi_m":             ship["beam_rsi_m"]   if "beam_rsi_m"   in ship.keys() else None,
        "height_rsi_m":           ship["height_rsi_m"] if "height_rsi_m" in ship.keys() else None,
        "rsi_name":               ship["rsi_name"]     if "rsi_name"     in ship.keys() else None,
        "rsi_url":                ship["rsi_url"]      if "rsi_url"      in ship.keys() else None,
        "mass_kg":                ship["mass_kg"] if "mass_kg" in ship.keys() else None,
        "size":                   ship["size_class"] if "size_class" in ship.keys() else None,
        "cargo_grids":            [dict(g) for g in grids],
        "hull_hp":                hull_hp,
        "ship_parts":             ship_parts,
        "hardpoints":             hardpoints,
        "components":             components,
        "lineart_file":           ship["lineart_file"] or None,
        "insurance": {
            "claim_time_mins":    ship["insurance_claim_time_mins"],
            "expedite_time_mins": ship["insurance_expedite_time_mins"],
            "expedite_fee":       ship["insurance_expedite_fee"],
        },
    })

@app.route("/api/compare")
def api_compare():
    names = [n.strip() for n in request.args.get("ships","").split(",") if n.strip()]
    results = []
    for name in names[:4]:
        with app.test_client() as c:
            r = c.get(f"/api/ship/{name}")
            if r.status_code == 200: results.append(r.get_json())
    return jsonify(results)

# ── Components ────────────────────────────────────────────────────────────────
@app.route("/api/components")
def api_components():
    conn = get_db(); p = PATCH or latest_patch(conn)
    item_type = request.args.get("type", "Shield")
    sort_by   = request.args.get("sort", "size")
    limit     = request.args.get("limit", 500, type=int)
    if sort_by not in {"entity_name","size","grade","display_name"}: sort_by = "size"

    rows = conn.execute(
        f"""SELECT uuid, entity_name, display_name, description,
                   item_type, item_subtype, grade, size, data
            FROM entities
            WHERE patch_version=? AND item_type=?
              AND entity_name NOT LIKE '%template%'
              AND entity_name NOT LIKE '%test%'
            ORDER BY {sort_by} NULLS LAST LIMIT ?""",
        (p, item_type, limit)
    ).fetchall()
    conn.close()

    param_key = COMP_PARAMS.get(item_type, "")
    result = []
    for r in rows:
        d = json.loads(r["data"])
        stats = d.get(param_key, {}) or {}
        power = d.get("power", {}) or {}
        result.append({
            "entity_name":  r["entity_name"],
            "display_name": best_name(r["display_name"], r["entity_name"]),
            "description":  r["description"],
            "item_type":    r["item_type"],
            "item_subtype": r["item_subtype"],
            "grade":        r["grade"],
            "size":         r["size"],
            "stats":        {k:v for k,v in stats.items() if not k.startswith("__")},
            "power":        power,
        })
    return jsonify(result)


@app.route('/api/components/compatible')
def get_compatible_components():
    """
    Get all components compatible with a specific hardpoint.
    Query params:
      - type: component type (PowerPlant, Shield, Cooler, QuantumDrive, Radar)
      - size: hardpoint size (1-7)
      - patch: optional patch version (defaults to latest)
    """
    comp_type = request.args.get('type', 'PowerPlant')
    size = request.args.get('size', type=int)
    patch = request.args.get('patch')
    
    if not size:
        return jsonify({"error": "size parameter required"}), 400
    
    # Get latest patch if not specified
    if not patch:
        conn = get_db()
        patch = conn.execute(
            "SELECT patch_version FROM patch_history ORDER BY imported_at DESC LIMIT 1"
        ).fetchone()['patch_version']
    
    conn = get_db()
    
    # Map component type to dedicated table
    table_map = {
        'PowerPlant': 'item_powerplants',
        'Shield': 'item_shields',
        'Cooler': 'item_coolers',
        'QuantumDrive': 'item_quantum_drives',
        'Radar': 'item_radars'
    }
    
    table_name = table_map.get(comp_type)
    if not table_name:
        return jsonify({"error": f"Unknown component type: {comp_type}"}), 400
    
    # Query the dedicated component table joined with entities for size, plus
    # item_components for the shared-only fields (heat_baseline, coolant) the
    # sidebar's cooling model needs but the typed tables don't carry.
    query = f"""
        SELECT
            c.*,
            e.size,
            e.grade,
            ic.heat_baseline,
            ic.coolant_consumption,
            ic.heat_gen_rate,
            ic.overheat_temperature,
            ic.overheat_warning_temp,
            ic.overheat_recovery_temp,
            ic.min_cooling_temperature,
            ic.cooling_equalization_rate,
            ic.cooling_equalization_tdiff,
            ic.powered_ambient_cool_mult,
            ic.overheat_enabled
        FROM {table_name} c
        JOIN entities e ON c.entity_name = e.entity_name AND c.patch_version = e.patch_version
        LEFT JOIN item_components ic ON ic.uuid = c.uuid AND ic.patch_version = c.patch_version
        WHERE c.patch_version = ?
          AND e.size = ?
        ORDER BY c.entity_name
    """
    
    rows = conn.execute(query, (patch, size)).fetchall()
    
    components = []
    for row in rows:
        # Convert row to dict
        comp = dict(row)

        # Unit-normalize to match get_ship_components: it serves QD speed as
        # drive_speed/1000 (km/s). The raw table value is ×1000, so a swapped-in
        # drive would otherwise read 1000× too fast in the sidebar/card.
        if comp_type == 'QuantumDrive' and comp.get('drive_speed') is not None:
            comp['drive_speed'] = comp['drive_speed'] / 1000

        # Lookup display_name from localization table
        # Entity format: powr_acom_s02_solarflare_scitem (lowercase, with _scitem)
        # Key formats in DB:
        #   - item_NamePOWR_ACOM_S02_...     (no underscore after Name)
        #   - item_Name_POWR_ACOM_S02_...    (underscore after Name)
        #   - May or may not have _SCItem suffix
        # Keys preserve original casing (SolarFlare, not SOLARFLARE)
        
        entity_name = comp['entity_name']
        # Strip _scitem suffix if present
        entity_base = entity_name.lower().replace('_scitem', '')
        entity_upper = entity_base.upper()
        
        # Try all four combinations with case-insensitive matching
        # Plus a 5th attempt with common typo patterns (e.g., Idris -> Idirs)
        entity_typo = entity_upper.replace('IDRIS', 'IDIRS')  # Handle known CIG typo
        
        loc_result = conn.execute("""
            SELECT value FROM localization 
            WHERE key LIKE ? COLLATE NOCASE 
               OR key LIKE ? COLLATE NOCASE
               OR key LIKE ? COLLATE NOCASE
               OR key LIKE ? COLLATE NOCASE
               OR key LIKE ? COLLATE NOCASE
               OR key LIKE ? COLLATE NOCASE
               OR key LIKE ? COLLATE NOCASE
               OR key LIKE ? COLLATE NOCASE
            LIMIT 1
        """, (
            f"item_Name{entity_upper}",           # item_NameCOOL_AEGS_S04_IDRIS
            f"item_Name{entity_upper}_SCItem",    # item_NameCOOL_AEGS_S04_IDRIS_SCItem
            f"item_Name_{entity_upper}",          # item_Name_COOL_AEGS_S04_IDRIS
            f"item_Name_{entity_upper}_SCItem",   # item_Name_COOL_AEGS_S04_IDRIS_SCItem
            f"item_Name{entity_typo}",            # item_NameCOOL_AEGS_S04_IDIRS (typo)
            f"item_Name{entity_typo}_SCItem",     # item_NameCOOL_AEGS_S04_IDIRS_SCItem
            f"item_Name_{entity_typo}",           # item_Name_COOL_AEGS_S04_IDIRS
            f"item_Name_{entity_typo}_SCItem"     # item_Name_COOL_AEGS_S04_IDIRS_SCItem
        )).fetchone()
        
        display_name = (loc_result['value'] if loc_result else comp.get('display_name')) \
                       or comp.get('entity_name')
        
        # Add grade letter
        comp['grade_letter'] = comp_grade(comp.get('grade'))
        
        # Extract manufacturer from entity_name
        entity_name_without_type = '_'.join(comp['entity_name'].split('_')[1:])
        comp['manufacturer'] = get_mfr(entity_name_without_type)
        
        # Organize stats based on component type
        stats = {}
        
        if comp_type == 'PowerPlant':
            stats['power_output'] = comp.get('power_output', 0)
            stats['em_signature'] = comp.get('em_signature', 0)
            
        elif comp_type == 'Shield':
            stats['max_shield_health'] = comp.get('max_shield_health', 0)
            stats['max_shield_regen'] = comp.get('max_shield_regen', 0)
            stats['downed_regen_delay'] = comp.get('downed_regen_delay', 0)
            stats['damaged_regen_delay'] = comp.get('damaged_regen_delay', 0)
            stats['em_signature'] = comp.get('em_signature', 0)
            stats['power_draw'] = comp.get('power_draw', 0)
            
        elif comp_type == 'Cooler':
            stats['cooling_output'] = comp.get('cooling_output', 0)
            stats['em_signature'] = comp.get('em_signature', 0)
            stats['ir_signature'] = comp.get('ir_signature', 0)
            stats['power_draw'] = comp.get('power_draw', 0)
            
        elif comp_type == 'QuantumDrive':
            # Use the real column names. drive_speed is already km/s-normalized
            # above; speed_mps here is the km/s value the card/modal display.
            stats['quantum_fuel_requirement'] = comp.get('fuel_per_gm_mscu', 0)
            stats['speed_mps'] = comp.get('drive_speed', 0)
            stats['cooldown_time'] = comp.get('cooldown_time', 0)
            stats['spool_up_time'] = comp.get('spool_up_time', 0)
            stats['em_signature'] = comp.get('em_signature', 0)
            stats['power_draw'] = comp.get('power_draw', 0)
            
        elif comp_type == 'Radar':
            stats['detection_range'] = comp.get('detection_lifetime_max', 0)
            stats['em_signature'] = comp.get('em_signature', 0)
            stats['power_draw'] = comp.get('power_draw', 0)
        
        # Skip unfinished placeholder items
        if is_placeholder(display_name):
            continue

        # Return the full flat row (every typed-table + shared field) so a
        # swapped-in component is shape-compatible with get_ship_components and
        # the sidebar's power/cooling/signature math reads it directly. `stats`
        # is kept for the swap modal's curated per-type display.
        component = dict(comp)
        component.update({
            'display_name': display_name,   # localized
            'item_type':    comp_type,
            'stats':        stats,
        })
        
        components.append(component)
    
    return jsonify({
        'type': comp_type,
        'size': size,
        'patch_version': patch,
        'count': len(components),
        'components': components
    })


@app.route("/api/ship/<entity_name>/port_options")
def api_port_options(entity_name):
    """Items installable on a weapon/mount slot, by the SC port-tag rule:
       size ∈ [min,max]  AND  (item_type, item_sub_type) ∈ port accepted_types
       AND  item tags satisfy the port's required_tags.

    Modes (one of):
      ?mount=<entity>        → weapons that fit inside that mount (its child slot)
      ?missile_size=<n>      → missiles/bombs that fit a rack slot of size n
      ?port=<port_name>      → mounts/weapons/racks that fit a top hardpoint
      ?types=A,B&min_size=&max_size= → items of the given item_type(s) in a size
                               range (mining lasers/modules, salvage heads/modules,
                               EMP, QED, fuel nozzles, tractor arms, …)
    """
    conn = get_db()
    p = PATCH or latest_patch(conn)
    port         = request.args.get("port")
    mount        = request.args.get("mount")
    missile_size = request.args.get("missile_size", type=int)
    types_param  = request.args.get("types")

    def _tag_ok(item_tags, required_tag):
        # Port requires a tag (e.g. "$ANVL_Hornet_Base"); item must carry it.
        if not required_tag:
            return True
        need = required_tag.lstrip("$").lower()
        toks = {t.lstrip("$").lower() for t in (item_tags or "").split()}
        return need in toks

    def _item_required_ok(item_tags, port_tags):
        # Reverse direction: a "$"-prefixed tag on the item is *required* — the
        # port must provide it via port_tags. Bespoke mounts (e.g. $MISC_Starfarer_Base)
        # only fit ports that advertise that family tag.
        need = {t.lstrip("$").lower() for t in (item_tags or "").split() if t.startswith("$")}
        if not need:
            return True
        have = {t.lstrip("$").lower() for t in (port_tags or "").split()}
        return need <= have

    def weapons_by(item_type, smin, smax, subtypes=None, mount_usable=True):
        rows = [dict(r) for r in conn.execute(
            "SELECT uuid, entity_name, display_name, size, item_type, item_sub_type, "
            "       grade_letter, class, tags "
            "FROM item_components WHERE patch_version=? AND item_type=? "
            "  AND size BETWEEN ? AND ?", (p, item_type, smin or 0, smax or 99)).fetchall()]
        if mount_usable and item_type == "WeaponGun":
            rows = [r for r in rows if "weaponmountusable" in (r.get("tags") or "").lower()]
        if subtypes:
            subs = {s.lower() for s in subtypes if s}
            if subs:
                rows = [r for r in rows if (r.get("item_sub_type") or "").lower() in subs]
        kind = "mining" if item_type == "WeaponMining" else "weapon"
        for r in rows:
            r["kind"] = kind
            r["display_name"] = r.get("display_name") or r["entity_name"]
            _enrich_weapon_stats(r)
        return rows

    def _enrich_weapon_stats(r):
        # Precompute dps / max_dmg / power_draw so a swapped weapon card renders
        # correct numbers without the full ammo/fire-mode structures client-side.
        w = conn.execute(
            "SELECT power_draw, max_ammo_load, ammo_uuid FROM item_weapons "
            "WHERE uuid=? AND patch_version=?", (r["uuid"], p)).fetchone()
        if not w:
            return
        r["power_draw"] = w["power_draw"]
        r["max_ammo_load"] = w["max_ammo_load"]
        a = conn.execute(
            "SELECT dmg_physical,dmg_energy,dmg_distortion,dmg_thermal,dmg_biochemical,dmg_stun "
            "FROM item_ammo WHERE uuid=? AND patch_version=?", (w["ammo_uuid"], p)).fetchone() if w["ammo_uuid"] else None
        shot = sum((a[k] or 0) for k in a.keys()) if a else 0
        fm = conn.execute(
            "SELECT fire_rate, pellet_count FROM item_weapon_fire_modes "
            "WHERE uuid=? AND patch_version=? AND fire_rate IS NOT NULL AND fire_rate>0 "
            "ORDER BY fire_rate DESC LIMIT 1", (r["uuid"], p)).fetchone()
        if shot and fm and fm["fire_rate"]:
            pellet = fm["pellet_count"] or 1
            r["dps"] = round(shot * pellet * (fm["fire_rate"] / 60.0))
            if w["max_ammo_load"]:
                r["max_dmg"] = round(shot * pellet * w["max_ammo_load"])

    def mounts_by(smin, smax, subtypes=None, required_tag=None):
        rows = [dict(r) for r in conn.execute(
            "SELECT entity_name, display_name, size, mount_type, sub_type, "
            "       weapon_port_count, weapon_min_size, weapon_max_size, "
            "       primary_port_type, tags "
            "FROM item_weapon_mounts WHERE patch_version=? AND size BETWEEN ? AND ?",
            (p, smin or 0, smax or 99)).fetchall()]
        if subtypes:
            subs = {s.lower() for s in subtypes if s}
            if subs:
                rows = [r for r in rows if (r.get("sub_type") or "").lower() in subs]
        rows = [r for r in rows if _tag_ok(r.get("tags"), required_tag)]
        for r in rows:
            r["kind"] = "mount"
            r["display_name"] = r.get("display_name") or r["entity_name"]
        return rows

    def missiles_by(size):
        rows = [dict(r) for r in conn.execute(
            "SELECT ic.entity_name, ic.display_name, ic.size, ic.item_sub_type, "
            "       m.ordnance_type, m.is_dumbfire, m.tracking_signal_type, "
            "       m.dmg_physical, m.dmg_energy, m.dmg_distortion, m.dmg_thermal, "
            "       m.dmg_biochemical, m.dmg_stun, m.blast_radius_min, m.blast_radius_max, "
            "       m.lock_range_min, m.lock_range_max, m.linear_speed "
            "FROM item_missiles m JOIN item_components ic "
            "  ON ic.uuid=m.uuid AND ic.patch_version=m.patch_version "
            "WHERE m.patch_version=? AND ic.size=?", (p, size)).fetchall()]
        for r in rows:
            r["kind"] = "bomb" if (r.get("ordnance_type") or "").lower() == "bomb" else "missile"
            r["display_name"] = r.get("display_name") or r["entity_name"]
        return rows

    options, ctx = [], {}

    if mount:
        m = conn.execute(
            "SELECT primary_port_type, weapon_min_size, weapon_max_size "
            "FROM item_weapon_mounts WHERE LOWER(entity_name)=LOWER(?) AND patch_version=?",
            (mount, p)).fetchone()
        if m:
            ppt = (m["primary_port_type"] or "WeaponGun")
            ctx = {"mode": "weapon_in_mount", "primary_port_type": ppt,
                   "size_range": [m["weapon_min_size"], m["weapon_max_size"]]}
            options = weapons_by(ppt if ppt in ("WeaponGun", "WeaponMining") else "WeaponGun",
                                 m["weapon_min_size"], m["weapon_max_size"])
    elif missile_size is not None:
        ctx = {"mode": "missile_in_rack", "size": missile_size}
        options = missiles_by(missile_size)
    elif types_param:
        tlist = [t.strip() for t in types_param.split(",") if t.strip()]
        smin = request.args.get("min_size", type=int) or 0
        smax = request.args.get("max_size", type=int) or 99
        ctx = {"mode": "types", "types": tlist, "size_range": [smin, smax]}
        qmarks = ",".join("?" * len(tlist))
        options = [dict(r) for r in conn.execute(
            f"SELECT uuid, entity_name, display_name, size, item_type, "
            f"       item_sub_type, grade_letter, class, tags "
            f"FROM item_components WHERE patch_version=? "
            f"  AND item_type IN ({qmarks}) AND size BETWEEN ? AND ?",
            (p, *tlist, smin, smax)).fetchall()]
        # Per-type stat enrichment so a swapped-in item is field-complete: the
        # cards render real numbers AND the sidebar (power/cooling/signatures +
        # per-type damage) recomputes correctly. Every typed table here carries
        # power_draw + em/ir_signature; item_components adds the cooling fields.
        TYPE_ENRICH = {
            "EMP": ("item_emp", [
                "power_draw", "em_signature", "ir_signature",
                "distortion_damage", "emp_radius", "charge_time"]),
            "QuantumInterdictionGenerator": ("item_qed", [
                "power_draw", "em_signature", "ir_signature",
                "radius_meters", "charge_time_secs", "cooldown_time_secs"]),
            "WeaponMining": ("item_mining_lasers", [
                "power_draw", "em_signature", "ir_signature",
                "mining_dps", "module_slots", "module_slot_size", "overheat_temperature"]),
            "SalvageHead": ("item_salvage", [
                "power_draw", "em_signature", "ir_signature",
                "salvage_speed_multiplier", "radius_multiplier", "extraction_efficiency"]),
            "SalvageModifier": ("item_salvage", [
                "power_draw", "em_signature", "ir_signature",
                "salvage_speed_multiplier", "radius_multiplier", "extraction_efficiency"]),
            "ToolArm": ("item_tool_arms", [
                "power_draw", "em_signature", "ir_signature"]),
            # Ore pods (Container/Cargo): capacity is the swap-relevant stat.
            "Container": ("item_mining_pods", ["capacity_scu"]),
        }
        for o in options:
            o["kind"] = "item"
            o["display_name"] = o.get("display_name") or o["entity_name"]
            # Shared cooling fields (all categories).
            ic = conn.execute(
                "SELECT heat_baseline, coolant_consumption FROM item_components "
                "WHERE uuid=? AND patch_version=?", (o["uuid"], p)).fetchone()
            if ic:
                o.update(dict(ic))
            enrich = TYPE_ENRICH.get(o["item_type"])
            if enrich:
                tbl, fields = enrich
                row = conn.execute(
                    f"SELECT {', '.join(fields)} FROM {tbl} "
                    f"WHERE uuid=? AND patch_version=?", (o["uuid"], p)).fetchone()
                if row:
                    o.update(dict(row))
        # Container/Cargo pods (ore pods): expose capacity as `scu` (matching
        # cargo_grids) and restrict to the pods that actually fit THIS ship's pod
        # ports. Type alone (Container:Cargo, size 1) is too coarse — a Prospector
        # would otherwise be offered the Drake Golem's bespoke pod and the Greycat
        # ROC's ground-vehicle pods. Two real constraints separate them:
        #   • category — ship pods (cargo_shipmining_*) vs ground-vehicle pods
        #     (cargo_groundvehiclemining_*); a ship port only takes its own kind.
        #   • bespoke lock — a $-prefixed item tag ($DRAK_Golem) means the pod only
        #     fits ports that advertise that tag, so it's locked to its own ship.
        # Collapsed/template variants have no item_mining_pods row (capacity_scu
        # is None) and drop out here too.
        if "Container" in tlist:
            inst = conn.execute(
                "SELECT installed_name FROM ship_hardpoints "
                "WHERE ship_entity_name=? AND patch_version=? AND installed_name IS NOT NULL "
                "  AND LOWER(accepted_types) LIKE 'container:cargo%' LIMIT 1",
                (entity_name, p)).fetchone()
            inst_name = (inst["installed_name"] if inst else "").lower()
            inst_ground = "groundvehicle" in inst_name
            kept = []
            for o in options:
                if o["item_type"] != "Container":
                    kept.append(o); continue
                if o.get("capacity_scu") is None:
                    continue                                     # collapsed/template
                en = o["entity_name"].lower()
                if ("groundvehicle" in en) != inst_ground:
                    continue                                     # ship vs ground-vehicle
                bespoke = any(t.startswith("$") for t in (o.get("tags") or "").split())
                if bespoke and en != inst_name:
                    continue                                     # locked to another ship
                o["scu"] = o["capacity_scu"]
                kept.append(o)
            options = kept
    elif port:
        hp = conn.execute(
            "SELECT accepted_types, required_tags, port_tags, min_size, max_size "
            "FROM ship_hardpoints WHERE ship_entity_name=? AND port_name=? AND patch_version=?",
            (entity_name, port, p)).fetchone()
        if hp:
            smin, smax = hp["min_size"], hp["max_size"]
            req = hp["required_tags"]
            ctx = {"mode": "hardpoint", "accepted_types": hp["accepted_types"],
                   "required_tags": req, "size_range": [smin, smax]}
            # accepted_types = 'Turret:Gun,GunTurret|WeaponGun:Gun'
            for chunk in (hp["accepted_types"] or "").split("|"):
                if not chunk:
                    continue
                typ, _, subs_raw = chunk.partition(":")
                subs = [s for s in subs_raw.split(",") if s]
                typ_l = typ.lower()
                if typ_l in ("turret", "turretbase", "utilityturret"):
                    options += mounts_by(smin, smax, subs, req)
                elif typ_l == "weapongun":
                    options += weapons_by("WeaponGun", smin, smax, subs)
                elif typ_l in ("missilelauncher", "bomblauncher"):
                    options += [dict(r, kind="rack") for r in conn.execute(
                        "SELECT ic.entity_name, ic.display_name, ic.size, ic.item_sub_type, ic.tags "
                        "FROM item_missile_racks mr JOIN item_components ic "
                        "  ON ic.uuid=mr.uuid AND ic.patch_version=mr.patch_version "
                        "WHERE mr.patch_version=? AND ic.size BETWEEN ? AND ?",
                        (p, smin or 0, smax or 99)).fetchall()]
                # Module / FlightController / etc. — not weapon-relevant, skipped

            # Reverse tag gate: drop bespoke items the port can't satisfy.
            options = [o for o in options if _item_required_ok(o.get("tags"), hp["port_tags"])]

    # Clean the option list (applies to every mode):
    #  1. Drop non-selectable engine variants — LOD render meshes (*_lowpoly, which
    #     are never installed) and placeholders (*_dummy, plus CIG's
    #     "<= PLACEHOLDER =>" names). They carry a real item's display name and
    #     just pad the picker.
    #  2. Collapse entities that share a (display name, size) to one canonical
    #     option. CIG ships turret/collector/bespoke variants — and even distinct
    #     guns — under one name: the S7 "M9A Cannon" alone spans 8 entities, and
    #     they're indistinguishable in a name-based picker. Prefer the base entity
    #     (fewest name segments, then shortest) so the plain weapon wins over its
    #     _turret / _idris_m / _collector sibling. Keyed on size too, so genuinely
    #     different sizes of the same-named weapon are never merged.
    best = {}
    for o in options:
        ent = (o.get("entity_name") or "").lower()
        if not ent or ent.endswith(("_lowpoly", "_dummy")) \
                or is_placeholder(o.get("display_name")):
            continue
        key = ((o.get("display_name") or ent).lower(), o.get("size"))
        rank = (ent.count("_"), len(ent))
        cur = best.get(key)
        if cur is None or rank < cur[0]:
            best[key] = (rank, o)
    uniq = [v[1] for v in best.values()]
    uniq.sort(key=lambda o: (o.get("size") or 0, (o.get("display_name") or "")))
    return jsonify({"entity_name": entity_name, "patch_version": p,
                    "context": ctx, "count": len(uniq), "options": uniq})


@app.route("/api/compare/components")
def api_compare_components():
    names = [n.strip() for n in request.args.get("items","").split(",") if n.strip()]
    results = []
    for name in names[:4]:
        with app.test_client() as c:
            r = c.get(f"/api/component/{name}")
            if r.status_code == 200: results.append(r.get_json())
    return jsonify(results)

# ── Weapons & Armor ───────────────────────────────────────────────────────────
@app.route("/api/weapons/ship")
def api_weapons_ship():
    conn = get_db(); p = PATCH or latest_patch(conn)
    rows = conn.execute(
        """SELECT entity_name, display_name, item_type, item_subtype, grade, size, data
           FROM entities WHERE patch_version=?
           AND item_type IN ('WeaponGun','WeaponMissile','WeaponDefensive')
           AND entity_name NOT LIKE '%template%' AND entity_name NOT LIKE '%test%'
           ORDER BY size NULLS LAST LIMIT 500""", (p,)
    ).fetchall()
    conn.close()
    return jsonify([{
        "entity_name":  r["entity_name"],
        "display_name": best_name(r["display_name"], r["entity_name"]),
        "item_type":    r["item_type"],
        "grade":        r["grade"],
        "size":         r["size"],
        "power":        (json.loads(r["data"]).get("power") or {}),
    } for r in rows])

@app.route("/api/weapons/fps")
def api_weapons_fps():
    conn = get_db(); p = PATCH or latest_patch(conn)
    rows = conn.execute(
        """SELECT entity_name, display_name, item_type, item_subtype, grade, size, data
           FROM entities WHERE patch_version=?
           AND item_type='WeaponPersonal' AND category LIKE '%fps_weapons%'
           AND entity_name NOT LIKE '%template%'
           ORDER BY entity_name LIMIT 500""", (p,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = json.loads(r["data"]); fire = {}
        for tag in ["SWeaponActionFireRapidParams","SWeaponActionFireSingleParams"]:
            v = d.get(tag)
            if v and isinstance(v, dict):
                fire = {"mode": v.get("name",""), "fire_rate": v.get("fireRate")}
                break
        result.append({
            "entity_name":  r["entity_name"],
            "display_name": best_name(r["display_name"], r["entity_name"]),
            "item_type":    r["item_type"],
            "item_subtype": r["item_subtype"],
            "grade":        r["grade"],
            "size":         r["size"],
            "fire":         fire,
        })
    return jsonify(result)

@app.route("/api/armor")
def api_armor():
    conn = get_db(); p = PATCH or latest_patch(conn)
    rows = conn.execute(
        """SELECT entity_name, display_name, description, item_type, item_subtype, grade, size, data
           FROM entities WHERE patch_version=?
           AND item_type LIKE 'Char_Armor%' AND category LIKE '%pu_armor%'
           AND entity_name NOT LIKE '%template%'
           ORDER BY item_type, entity_name LIMIT 500""", (p,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = json.loads(r["data"])
        cl = d.get("SCItemClothingParams", {}) or {}
        tr = cl.get("TemperatureResistance", {}) if isinstance(cl.get("TemperatureResistance"), dict) else {}
        rr = cl.get("RadiationResistance",   {}) if isinstance(cl.get("RadiationResistance"),   dict) else {}
        result.append({
            "entity_name":  r["entity_name"],
            "display_name": best_name(r["display_name"], r["entity_name"]),
            "description":  r["description"],
            "item_type":    r["item_type"],
            "item_subtype": r["item_subtype"],
            "grade":        r["grade"],
            "size":         r["size"],
            "temp_max":     tr.get("MaxResistance"),
            "temp_min":     tr.get("MinResistance"),
            "rad_capacity": rr.get("MaximumRadiationCapacity"),
        })
    return jsonify(result)
    
# ─────────────────────────────────────────────────────────────────────────────
# CRAFTING — Add these routes to server.py
# ─────────────────────────────────────────────────────────────────────────────

# ── API: Top-level categories ─────────────────────────────────────────────────
# GET /api/crafting/top_levels
# Returns the three top-level groupings (FPS Gear, Vehicle Gear, Mission Items)
# with blueprint counts. Always uses latest patch.

# Top-level crafting categories to hide from the UI. CIG ships some categories
# (e.g. mission items) whose blueprints aren't real crafting recipes from a
# player's perspective. Add to this set to hide additional top levels.
HIDDEN_CRAFTING_TOP_LEVELS = {"missionitems"}


@app.route("/api/crafting/top_levels")
def api_crafting_top_levels():
    db = get_db()
    patch = latest_patch(db)
    if not patch:
        return jsonify([])

    placeholders = ",".join("?" for _ in HIDDEN_CRAFTING_TOP_LEVELS) or "''"
    rows = db.execute(f"""
        SELECT
            c.top_level,
            c.top_display,
            COUNT(b.uuid) AS blueprint_count
        FROM crafting_categories c
        LEFT JOIN crafting_blueprints b
            ON b.category_id = c.id
            AND b.patch_version = c.patch_version
            -- Exclude *_template recipes so counts match the filtered list view
            AND b.entity_name NOT LIKE '%\\_template' ESCAPE '\\'
        WHERE c.patch_version = ?
          AND c.top_level NOT IN ({placeholders})
        GROUP BY c.top_level, c.top_display
        HAVING blueprint_count > 0
        ORDER BY c.top_display
    """, (patch, *HIDDEN_CRAFTING_TOP_LEVELS)).fetchall()

    return jsonify([dict(r) for r in rows])


# ── API: Mid-level categories within a top level ─────────────────────────────
# GET /api/crafting/categories?top_level=fpsgear
# Returns mid-level categories under that top group.

@app.route("/api/crafting/categories")
def api_crafting_categories():
    db = get_db()
    patch = latest_patch(db)
    top_level = request.args.get("top_level", "").strip()

    if not patch or not top_level:
        return jsonify([])

    rows = db.execute("""
        SELECT
            c.mid_level,
            c.mid_display,
            COUNT(b.uuid) AS blueprint_count
        FROM crafting_categories c
        LEFT JOIN crafting_blueprints b
            ON b.category_id = c.id
            AND b.patch_version = c.patch_version
            -- Exclude *_template recipes so counts match the filtered list view
            AND b.entity_name NOT LIKE '%\\_template' ESCAPE '\\'
        WHERE c.patch_version = ?
          AND c.top_level = ?
          AND c.mid_level IS NOT NULL
        GROUP BY c.mid_level, c.mid_display
        HAVING blueprint_count > 0
        ORDER BY c.mid_display
    """, (patch, top_level)).fetchall()

    # Also count blueprints directly under the top level (no mid_level)
    direct = db.execute("""
        SELECT COUNT(b.uuid) AS cnt
        FROM crafting_categories c
        LEFT JOIN crafting_blueprints b
            ON b.category_id = c.id
            AND b.patch_version = c.patch_version
            -- Exclude *_template recipes so counts match the filtered list view
            AND b.entity_name NOT LIKE '%\\_template' ESCAPE '\\'
        WHERE c.patch_version = ?
          AND c.top_level = ?
          AND c.mid_level IS NULL
    """, (patch, top_level)).fetchone()

    result = [dict(r) for r in rows]

    # If there are blueprints directly under the top level, add an "All" pseudo-card
    if direct and direct["cnt"] > 0:
        result.insert(0, {
            "mid_level":       None,
            "mid_display":     "All Items",
            "blueprint_count": direct["cnt"],
        })

    return jsonify(result)


# ── API: Sub-level categories (for filter chips) ──────────────────────────────
# GET /api/crafting/sublevels?top_level=fpsgear&mid_level=armour
# Returns distinct sub_level values found under (top, mid).

@app.route("/api/crafting/sublevels")
def api_crafting_sublevels():
    db = get_db()
    patch = latest_patch(db)
    top_level = request.args.get("top_level", "").strip()
    mid_level = request.args.get("mid_level", "").strip()

    if not patch or not top_level:
        return jsonify([])

    rows = db.execute("""
        SELECT DISTINCT
            c.sub_level,
            c.sub_display
        FROM crafting_categories c
        WHERE c.patch_version = ?
          AND c.top_level = ?
          AND c.mid_level = ?
          AND c.sub_level IS NOT NULL
        ORDER BY c.sub_display
    """, (patch, top_level, mid_level)).fetchall()

    return jsonify([dict(r) for r in rows])


# ── API: Blueprint list ───────────────────────────────────────────────────────
# GET /api/crafting/blueprints?top_level=&mid_level=&sub_level=
# Returns blueprints matching the category filter.

@app.route("/api/crafting/blueprints")
def api_crafting_blueprints():
    db = get_db()
    patch = latest_patch(db)
    top_level = request.args.get("top_level", "").strip()
    mid_level = request.args.get("mid_level", "").strip()
    sub_level = request.args.get("sub_level", "").strip()
    if not patch:
        return jsonify([])
    # Aggregated mission counts + lawful breakdown per blueprint.
    # mission_count uses DISTINCT (title, tier) to match how the missions popup
    # groups identical missions across regions; orphan pools (no contract link)
    # count toward mission_count but contribute to lawful counts as 'unknown'.
    query = """
        WITH mission_stats AS (
            SELECT
                cmp.blueprint_uuid,
                cmp.patch_version,
                COUNT(DISTINCT COALESCE(c.title_resolved, cmp.mission_name)) AS mission_count,
                SUM(CASE WHEN c.lawful = 1 THEN 1 ELSE 0 END) AS lawful_count,
                SUM(CASE WHEN c.lawful = 0 THEN 1 ELSE 0 END) AS unlawful_count
            FROM crafting_mission_pools cmp
            LEFT JOIN contract_blueprint_rewards r
                ON r.pool_uuid = cmp.pool_uuid
               AND r.patch_version = cmp.patch_version
            LEFT JOIN contracts c
                ON c.contract_uuid = r.contract_uuid
               AND c.patch_version = r.patch_version
            GROUP BY cmp.blueprint_uuid, cmp.patch_version
        )
        SELECT
            b.uuid,
            b.patch_version,
            b.entity_name,
            b.output_uuid,
            b.output_name,
            -- Filter empty strings and CIG's "<= PLACEHOLDER =>" sentinel so a real name
-- further down the chain (or our ship-name override on b.output_display) can win.
COALESCE(
    NULLIF(NULLIF(NULLIF(NULLIF(ic.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
    -- ic2: fallback for blueprints with no output_name (e.g. fuel nozzles),
    -- keyed on the item entity derived from the bp's own entity_name.
    NULLIF(NULLIF(NULLIF(NULLIF(ic2.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
    NULLIF(e.display_name, ''),
    NULLIF(b.output_display, ''),
    b.output_name,
    b.entity_name
) AS output_display,
            b.craft_time_sec,
            b.slots_required,
            c.top_level,
            c.mid_level,
            c.sub_level,
            c.sub_sub_level,
            c.display_path,
            c.sub_display,
            c.sub_sub_display,
            -- Component size + type, surfaced on the card for ship components/weapons.
            -- item_components is the canonical source (same table ship_detail reads).
            COALESCE(ic.size, ic2.size) AS item_size,
            COALESCE(ic.item_type, ic2.item_type) AS item_type,
            -- High-level item bucket for the new sidebar filter.
            CASE
                WHEN c.top_level = 'fpsgear'     AND c.mid_level = 'armour'  THEN 'armor'
                WHEN c.top_level = 'fpsgear'     AND c.mid_level = 'ammo'     THEN 'ammo'
                WHEN c.top_level = 'fpsgear'     AND c.mid_level = 'weapons'  THEN 'fps_weapons'
                WHEN c.top_level = 'vehiclegear' AND c.mid_level = 'weapons'  THEN 'ship_weapons'
                WHEN c.top_level = 'vehiclegear'                             THEN 'ship_components'
                ELSE NULL
            END AS item_category,
            COALESCE(ms.mission_count, 0)  AS mission_count,
            COALESCE(ms.lawful_count, 0)   AS lawful_count,
            COALESCE(ms.unlawful_count, 0) AS unlawful_count
        FROM crafting_blueprints b
        LEFT JOIN crafting_categories c
            ON c.id = b.category_id
        LEFT JOIN entities e
            ON e.uuid = b.output_uuid
            AND e.patch_version = b.patch_version
        -- item_components carries the canonical display_name for ship/FPS
        -- components (resolved from the entity's own loc_name_key) — covers
        -- categories whose loc keys don't fit the item_Name* patterns the
        -- entity-level localizer knows about (e.g. fuel nozzles).
        LEFT JOIN item_components ic
            ON ic.entity_name = b.output_name
           AND ic.patch_version = b.patch_version
        -- Fallback: some blueprints (fuel nozzles) carry no output_name and a
        -- dangling output_uuid, so neither ic nor entities resolve a name. The
        -- item entity is recoverable from the bp's entity_name by stripping the
        -- 'bp_craft_' prefix (e.g. bp_craft_nozzle_fuelgiver_grin_nozzlefast →
        -- nozzle_fuelgiver_grin_nozzlefast → "Norfield").
        LEFT JOIN item_components ic2
            ON ic2.entity_name = REPLACE(b.entity_name, 'bp_craft_', '')
           AND ic2.patch_version = b.patch_version
        LEFT JOIN mission_stats ms
            ON ms.blueprint_uuid = b.uuid
           AND ms.patch_version = b.patch_version
        WHERE b.patch_version = ?
          -- *_template blueprints are CIG's internal recipe templates that all
          -- point at the same default output (e.g. all 7 bp_craftnozzle_*_template
          -- → RN-7s) rather than craftable per-item recipes. Hide them.
          AND b.entity_name NOT LIKE '%\\_template' ESCAPE '\\'
          -- Hide top levels we don't surface (mission items).
          AND (c.top_level IS NULL OR c.top_level NOT IN ({hidden_top_levels}))
    """.format(hidden_top_levels=",".join("?" for _ in HIDDEN_CRAFTING_TOP_LEVELS) or "''")
    params = [patch, *HIDDEN_CRAFTING_TOP_LEVELS]
    if top_level:
        query += " AND c.top_level = ?"
        params.append(top_level)
    if mid_level:
        query += " AND c.mid_level = ?"
        params.append(mid_level)
    if sub_level:
        query += " AND c.sub_level = ?"
        params.append(sub_level)
    query += " ORDER BY COALESCE(ic.display_name, ic2.display_name, e.display_name, b.output_display, b.output_name, b.entity_name) ASC"
    rows = db.execute(query, params).fetchall()
    
    # Apply localization lookup for blueprints without proper display names
    results = []
    for row in rows:
        bp = dict(row)
        
        # If output_display is still the output_name (entity name), try localization
        if bp['output_display'] == bp['output_name'] and bp['output_name']:
            entity_name = bp['output_name']
            # Strip _scitem suffix
            entity_base = entity_name.lower().replace('_scitem', '')
            entity_upper = entity_base.upper()
            
            # Handle known typos (Idris -> Idirs)
            entity_typo = entity_upper.replace('IDRIS', 'IDIRS')
            
            # Try all localization key variations
            loc_result = db.execute("""
                SELECT value FROM localization 
                WHERE key LIKE ? COLLATE NOCASE 
                   OR key LIKE ? COLLATE NOCASE
                   OR key LIKE ? COLLATE NOCASE
                   OR key LIKE ? COLLATE NOCASE
                   OR key LIKE ? COLLATE NOCASE
                   OR key LIKE ? COLLATE NOCASE
                   OR key LIKE ? COLLATE NOCASE
                   OR key LIKE ? COLLATE NOCASE
                LIMIT 1
            """, (
                f"item_Name{entity_upper}",
                f"item_Name{entity_upper}_SCItem",
                f"item_Name_{entity_upper}",
                f"item_Name_{entity_upper}_SCItem",
                f"item_Name{entity_typo}",
                f"item_Name{entity_typo}_SCItem",
                f"item_Name_{entity_typo}",
                f"item_Name_{entity_typo}_SCItem"
            )).fetchone()
            
            if loc_result:
                bp['output_display'] = loc_result['value']

        results.append(bp)

    # ── Attach mission facets (legality / mission type / faction / difficulty) ──
    # One aggregation query for all blueprints, folded into per-blueprint sets.
    facet_rows = db.execute("""
        SELECT
            cmp.blueprint_uuid AS bp,
            c.mission_type_display AS mission_type,
            c.tier   AS tier,
            c.lawful AS lawful,
            COALESCE(o.display_name, o.name) AS faction
        FROM crafting_mission_pools cmp
        JOIN contract_blueprint_rewards r
            ON r.pool_uuid = cmp.pool_uuid AND r.patch_version = cmp.patch_version
        JOIN contracts c
            ON c.contract_uuid = r.contract_uuid AND c.patch_version = r.patch_version
        LEFT JOIN mission_organizations o
            ON o.org_uuid = c.contractor_org_uuid AND o.patch_version = c.patch_version
        WHERE cmp.patch_version = ?
    """, (patch,)).fetchall()

    facets = {}
    for fr in facet_rows:
        f = facets.setdefault(fr["bp"], {"mission_types": set(), "factions": set(),
                                         "tiers": set(), "legality": set()})
        if fr["mission_type"]:
            f["mission_types"].add(fr["mission_type"])
        if fr["faction"]:
            f["factions"].add(_prettify_faction(fr["faction"]))
        if fr["tier"]:
            f["tiers"].add(fr["tier"])
        f["legality"].add("Lawful" if fr["lawful"] == 1
                          else "Unlawful" if fr["lawful"] == 0 else "Unknown")

    # ── Attach ingredient inputs (powers the "Ingredients" sidebar filter) ──
    # Distinct ingredient display names per blueprint, resolved the same way the
    # detail endpoint does (entity name → stored display → resource name) so the
    # filter labels match the chips shown on each card.
    input_rows = db.execute("""
        SELECT
            ci.blueprint_uuid AS bp,
            COALESCE(e.display_name, ci.display_name, ci.resource_name) AS name
        FROM crafting_ingredients ci
        LEFT JOIN entities e
            ON e.uuid = ci.resource_uuid AND e.patch_version = ci.patch_version
        WHERE ci.patch_version = ?
    """, (patch,)).fetchall()

    inputs_by_bp = {}
    for ir in input_rows:
        if ir["name"]:
            inputs_by_bp.setdefault(ir["bp"], set()).add(ir["name"])

    for bp in results:
        f = facets.get(bp["uuid"])
        bp["mission_types"] = sorted(f["mission_types"]) if f else []
        bp["factions"]      = sorted(f["factions"])      if f else []
        bp["tiers"]         = sorted(f["tiers"])         if f else []
        bp["legality"]      = sorted(f["legality"])      if f else []
        bp["inputs"]        = sorted(inputs_by_bp.get(bp["uuid"], ()))

    return jsonify(results)
    
    
# ── API: Blueprint detail (with slots, ingredients, modifiers) ────────────────
# GET /api/crafting/blueprint/<uuid>?patch=
# Now also includes piecewise modifier ranges.

@app.route("/api/crafting/blueprint/<uuid>")
def api_crafting_blueprint_detail(uuid):
    db = get_db()
    patch = request.args.get("patch") or latest_patch(db)
    if not patch:
        return jsonify({"error": "No patch data available"}), 404

    bp = db.execute("""
        SELECT
            b.uuid,
            b.patch_version,
            b.entity_name,
            b.output_uuid,
            b.output_name,
            -- Filter empty strings and CIG's "<= PLACEHOLDER =>" sentinel so a real name
-- further down the chain (or our ship-name override on b.output_display) can win.
COALESCE(
    NULLIF(NULLIF(NULLIF(NULLIF(ic.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
    -- ic2: fallback for blueprints with no output_name (e.g. fuel nozzles),
    -- keyed on the item entity derived from the bp's own entity_name.
    NULLIF(NULLIF(NULLIF(NULLIF(ic2.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
    NULLIF(e.display_name, ''),
    NULLIF(b.output_display, ''),
    b.output_name,
    b.entity_name
) AS output_display,
            b.craft_time_sec,
            b.slots_required,
            b.has_optional,
            c.display_path AS category_path,
            c.top_display,
            c.mid_display,
            c.sub_display,
            c.sub_sub_display
        FROM crafting_blueprints b
        LEFT JOIN crafting_categories c ON c.id = b.category_id
        LEFT JOIN entities e
            ON e.uuid = b.output_uuid
            AND e.patch_version = b.patch_version
        -- See note in /api/crafting/blueprints — item_components covers cases
        -- where the entity-level localizer didn't find a matching pattern.
        LEFT JOIN item_components ic
            ON ic.entity_name = b.output_name
           AND ic.patch_version = b.patch_version
        -- Fallback for blueprints with no output_name (fuel nozzles); see the
        -- matching note in /api/crafting/blueprints.
        LEFT JOIN item_components ic2
            ON ic2.entity_name = REPLACE(b.entity_name, 'bp_craft_', '')
           AND ic2.patch_version = b.patch_version
        WHERE b.uuid = ? AND b.patch_version = ?
    """, (uuid, patch)).fetchone()

    if not bp:
        return jsonify({"error": "Blueprint not found"}), 404

    result = dict(bp)

    slots = db.execute("""
        SELECT id, slot_index, slot_debug_name, slot_display
        FROM crafting_slots
        WHERE blueprint_uuid = ? AND patch_version = ?
        ORDER BY slot_index ASC
    """, (uuid, patch)).fetchall()

    result["slots"] = []
    for slot in slots:
        ingredients_raw = db.execute("""
            SELECT
                ci.cost_type,
                ci.resource_uuid,
                ci.resource_name,
                COALESCE(e.display_name, ci.display_name, ci.resource_name) AS display_name,
                ci.quantity,
                ci.min_quality
            FROM crafting_ingredients ci
            LEFT JOIN entities e
                ON e.uuid = ci.resource_uuid
                AND e.patch_version = ci.patch_version
            WHERE ci.slot_id = ?
            ORDER BY ci.id ASC
        """, (slot["id"],)).fetchall()
        
        # Apply localization lookup for ingredients without display_name
        ingredients = []
        for ing_row in ingredients_raw:
            ing = dict(ing_row)
            
        #    print(f"[DEBUG] Ingredient: display_name={ing.get('display_name')}, resource_name={ing.get('resource_name')}")
            
            # If display_name is still the resource_name, try localization
            if ing['display_name'] == ing['resource_name'] and ing['resource_name']:
                entity_name = ing['resource_name']
                
                # DEBUG - print what we're looking up
                print(f"[DEBUG] Looking up localization for: {entity_name}")
                
                # Strip _scitem suffix
                entity_base = entity_name.lower().replace('_scitem', '')
                entity_upper = entity_base.upper()
                
                print(f"[DEBUG] Entity upper: {entity_upper}")
                
                # Handle known typos (Idris -> Idirs)
                entity_typo = entity_upper.replace('IDRIS', 'IDIRS')
                
                print(f"[DEBUG] Entity typo: {entity_typo}")
                # Strip _scitem suffix
                entity_base = entity_name.lower().replace('_scitem', '')
                entity_upper = entity_base.upper()
                
                # Handle known typos (Idris -> Idirs)
                entity_typo = entity_upper.replace('IDRIS', 'IDIRS')
                
                # Try all localization key variations
                loc_result = db.execute("""
                    SELECT value FROM localization 
                    WHERE key LIKE ? COLLATE NOCASE 
                       OR key LIKE ? COLLATE NOCASE
                       OR key LIKE ? COLLATE NOCASE
                       OR key LIKE ? COLLATE NOCASE
                       OR key LIKE ? COLLATE NOCASE
                       OR key LIKE ? COLLATE NOCASE
                       OR key LIKE ? COLLATE NOCASE
                       OR key LIKE ? COLLATE NOCASE
                    LIMIT 1
                """, (
                    f"item_Name{entity_upper}",
                    f"item_Name{entity_upper}_SCItem",
                    f"item_Name_{entity_upper}",
                    f"item_Name_{entity_upper}_SCItem",
                    f"item_Name{entity_typo}",
                    f"item_Name{entity_typo}_SCItem",
                    f"item_Name_{entity_typo}",
                    f"item_Name_{entity_typo}_SCItem"
                )).fetchone()
                
                if loc_result:
                    ing['display_name'] = loc_result['value']

            # Attach the per-material quantization curve so the frontend can
            # apply the same raw-quality → mapped-quality step the game does
            # before evaluating the slot's modifier curve.
            if ing.get("cost_type") == "resource":
                ing["quantization"] = resolve_ingredient_quantization(
                    db, patch, ing.get("resource_uuid")
                )

            ingredients.append(ing)

        # Modifiers — collect all ranges and group by gameplay_prop_uuid
        # so the UI can render piecewise curves correctly. The crafting_gameplay_properties
        # join provides canonical localized labels ("Integrity", "Fire Rate") and
        # unit formats ("%.2f RPM") from the gpp_*.xml records; we fall back to the
        # extractor's heuristic prop_display_name when loc resolution misses.
        mod_rows = db.execute("""
            SELECT
                m.gameplay_prop_uuid,
                m.gameplay_prop_name,
                m.prop_display_name,
                COALESCE(loc_label.value, m.prop_display_name) AS display_label,
                loc_unit.value AS unit_format,
                m.range_index,
                m.modifier_at_start,
                m.modifier_at_end,
                m.start_quality,
                m.end_quality
            FROM crafting_slot_modifiers m
            LEFT JOIN crafting_gameplay_properties p
                ON p.uuid = m.gameplay_prop_uuid
               AND p.patch_version = m.patch_version
            LEFT JOIN localization loc_label
                ON loc_label.key = p.property_loc_key
            LEFT JOIN localization loc_unit
                ON loc_unit.key = p.unit_loc_key
            WHERE m.slot_id = ?
            ORDER BY m.gameplay_prop_uuid, m.range_index
        """, (slot["id"],)).fetchall()

        # Group rows by gameplay_prop_uuid → list of ranges. We also resolve
        # base_value once per group (not per range) by looking up the output
        # entity's value for this property — the frontend uses it to compute
        # final_stat = base_value × Π(quality_multipliers).
        modifiers = []
        current = None
        for r in mod_rows:
            if current is None or current["gameplay_prop_uuid"] != r["gameplay_prop_uuid"]:
                gpp = (r["gameplay_prop_name"] or "").lower()
                is_percent = gpp in GPP_PERCENT_PROPS
                if is_percent:
                    # Percent-style: no base lookup needed — frontend uses
                    # (multiplier - 1) × 100% directly.
                    base_value, base_source = None, None
                else:
                    base_value, base_source = resolve_gpp_base_value(
                        db, patch, r["gameplay_prop_name"], result.get("output_name")
                    )
                current = {
                    "gameplay_prop_uuid": r["gameplay_prop_uuid"],
                    "gameplay_prop_name": r["gameplay_prop_name"],
                    "prop_display_name":  r["prop_display_name"],
                    "display_label":      r["display_label"],
                    "unit_format":        r["unit_format"],
                    "is_percent":         is_percent,
                    "base_value":         base_value,
                    "base_source":        base_source,
                    "ranges":             [],
                }
                modifiers.append(current)
            current["ranges"].append({
                "range_index":       r["range_index"],
                "modifier_at_start": r["modifier_at_start"],
                "modifier_at_end":   r["modifier_at_end"],
                "start_quality":     r["start_quality"],
                "end_quality":       r["end_quality"],
            })

        result["slots"].append({
            "slot_index":      slot["slot_index"],
            "slot_debug_name": slot["slot_debug_name"],
            "slot_display":    slot["slot_display"],
            "ingredients":     [dict(i) for i in ingredients],
            "modifiers":       modifiers,
        })

    return jsonify(result)


# ── API: Mission lookup for a blueprint ───────────────────────────────────────
#
# Resolves each blueprint to the real, human-readable missions that drop it.
# Returns a grouped structure:
#
#   { "total": <int>,
#     "factions": [
#        { "name": "Bit Zeros",
#          "missions": [
#             { "title": "Data Transfer", "tier": "Intro",
#               "regions": ["Stanton", "Nyx"],
#               "pool_uuid": "...", "mission_name": "BitZeros_BlackBoxRecovery" },
#             ...
#          ]
#        }, ...
#     ]
#   }
#
# Pools without a contract link (XenoThreat / collector / event rewards) appear
# under an "Other" faction with prettified mission_name as the title.

import re as _re

_TIER_ORDER = {"Intro": 0, "VE": 1, "E": 2, "M": 3, "H": 4, "VH": 5, "S": 6}
_REGION_RX = _re.compile(r"(?:^|[_ ])(Stanton|Nyx|Pyro)(?:[_ ]|$)", _re.I)

def _extract_region(*candidates):
    for c in candidates:
        if not c:
            continue
        m = _REGION_RX.search(c)
        if m:
            return m.group(1).title()
    return None

def _prettify_mission_name(name):
    """Turn 'XenoThreat2_15_01' / 'BitZeros_BlackBoxRecovery' into readable text."""
    if not name:
        return ""
    s = name.replace("_", " ")
    # Insert spaces between camel-case transitions.
    s = _re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = _re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return _re.sub(r"\s+", " ", s).strip()


@app.route("/api/crafting/blueprint/<uuid>/missions")
def api_crafting_blueprint_missions(uuid):
    db    = get_db()
    patch = request.args.get("patch") or latest_patch(db)
    if not patch:
        return jsonify({"factions": [], "total": 0})

    # 1) Every pool this blueprint can drop from
    pools = db.execute("""
        SELECT DISTINCT pool_uuid, mission_name, faction
        FROM crafting_mission_pools
        WHERE blueprint_uuid = ? AND patch_version = ?
    """, (uuid, patch)).fetchall()
    if not pools:
        return jsonify({"factions": [], "total": 0})

    pool_uuids = [p["pool_uuid"] for p in pools]
    placeholders = ",".join("?" * len(pool_uuids))

    # 2) Every contract that drops from any of those pools
    rows = db.execute(f"""
        SELECT
            r.pool_uuid,
            c.title_resolved,
            c.tier,
            c.career_debug_name,
            c.debug_name,
            o.display_name AS org_display,
            o.name         AS org_name,
            cmp.mission_name,
            cmp.faction    AS pool_faction
        FROM contract_blueprint_rewards r
        JOIN contracts c
          ON c.contract_uuid = r.contract_uuid
         AND c.patch_version = r.patch_version
        LEFT JOIN mission_organizations o
          ON o.org_uuid = c.contractor_org_uuid
         AND o.patch_version = c.patch_version
        JOIN crafting_mission_pools cmp
          ON cmp.pool_uuid = r.pool_uuid
         AND cmp.patch_version = r.patch_version
         AND cmp.blueprint_uuid = ?
        WHERE r.pool_uuid IN ({placeholders})
          AND r.patch_version = ?
    """, (uuid, *pool_uuids, patch)).fetchall()

    # 3) Aggregate: faction → { (title, tier) → {regions, pool_uuid, mission_name} }
    grouped = {}
    pools_with_contracts = set()
    for r in rows:
        faction = r["org_display"] or r["org_name"] or "Unknown"
        title   = r["title_resolved"] or _prettify_mission_name(r["mission_name"])
        tier    = r["tier"]
        region  = _extract_region(r["debug_name"], r["career_debug_name"])
        key     = (title, tier)
        slot    = grouped.setdefault(faction, {}).setdefault(key, {
            "regions": set(),
            "pool_uuid": r["pool_uuid"],
            "mission_name": r["mission_name"],
        })
        if region:
            slot["regions"].add(region)
        pools_with_contracts.add(r["pool_uuid"])

    factions = []
    for faction_name in sorted(grouped, key=str.lower):
        missions = []
        for (title, tier), info in grouped[faction_name].items():
            missions.append({
                "title":        title,
                "tier":         tier,
                "regions":      sorted(info["regions"]),
                "pool_uuid":    info["pool_uuid"],
                "mission_name": info["mission_name"],
            })
        missions.sort(key=lambda m: (
            _TIER_ORDER.get(m["tier"], 99),
            m["title"].lower(),
        ))
        factions.append({"name": faction_name, "missions": missions})

    # 4) Orphan pools (no contract linkage — XenoThreat, collectors, events)
    orphans = [p for p in pools if p["pool_uuid"] not in pools_with_contracts]
    if orphans:
        by_faction = {}
        for p in orphans:
            faction = _prettify_mission_name(p["faction"]) if p["faction"] else "Other"
            by_faction.setdefault(faction or "Other", []).append({
                "title":        _prettify_mission_name(p["mission_name"]),
                "tier":         None,
                "regions":      [],
                "pool_uuid":    p["pool_uuid"],
                "mission_name": p["mission_name"],
            })
        for faction_name in sorted(by_faction, key=str.lower):
            missions = by_faction[faction_name]
            missions.sort(key=lambda m: m["title"].lower())
            # Merge into an existing faction bucket if same name; otherwise append
            existing = next((f for f in factions if f["name"].lower() == faction_name.lower()), None)
            if existing:
                existing["missions"].extend(missions)
            else:
                factions.append({"name": faction_name, "missions": missions})

    total = sum(len(f["missions"]) for f in factions)
    return jsonify({"factions": factions, "total": total})


# ── API: Mission detail (unchanged from 4.7) ─────────────────────────────────

@app.route("/api/crafting/mission/<mission_name>")
def api_crafting_mission_detail(mission_name):
    db    = get_db()
    patch = request.args.get("patch") or latest_patch(db)
    if not patch:
        return jsonify({})
    pool = db.execute("""
        SELECT pool_uuid, mission_name, faction
        FROM crafting_mission_pools
        WHERE mission_name = ? AND patch_version = ?
        LIMIT 1
    """, (mission_name, patch)).fetchone()
    if not pool:
        return jsonify({"error": "Mission not found"}), 404

    blueprints = db.execute("""
        SELECT
            cmp.blueprint_uuid,
            cb.entity_name,
            COALESCE(
                NULLIF(NULLIF(NULLIF(NULLIF(ic.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
                NULLIF(e.display_name, ''),
                NULLIF(cb.output_display, ''),
                cb.output_name
            ) AS output_display,
            cb.output_name,
            cb.craft_time_sec,
            cb.slots_required,
            c.display_path AS category_path
        FROM crafting_mission_pools cmp
        LEFT JOIN crafting_blueprints cb
            ON cb.uuid = cmp.blueprint_uuid
            AND cb.patch_version = cmp.patch_version
        LEFT JOIN crafting_categories c
            ON c.id = cb.category_id
        LEFT JOIN entities e
            ON e.uuid = cb.output_uuid
            AND e.patch_version = cb.patch_version
        LEFT JOIN item_components ic
            ON ic.entity_name = cb.output_name
           AND ic.patch_version = cb.patch_version
        WHERE cmp.mission_name = ? AND cmp.patch_version = ?
        ORDER BY COALESCE(
            NULLIF(NULLIF(NULLIF(NULLIF(ic.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
            NULLIF(e.display_name, ''),
            NULLIF(cb.output_display, ''),
            cb.output_name
        ) ASC
    """, (mission_name, patch)).fetchall()

    return jsonify({
        "pool_uuid":    pool["pool_uuid"],
        "mission_name": pool["mission_name"],
        "faction":      pool["faction"],
        "blueprints":   [dict(b) for b in blueprints],
    })

# ─────────────────────────────────────────────────────────────────────────────
# BLUEPRINT OWNERSHIP
# ─────────────────────────────────────────────────────────────────────────────
# Schema (dataforge.db):
#   blueprint_ownership(id, discord_id, blueprint_uuid, blueprint_name,
#                       patch_version, claimed_at, env, notes)
#   UNIQUE(discord_id, blueprint_uuid, patch_version)
# Multi-owner: many users can each claim the same blueprint. The unique
# constraint only blocks the same user re-claiming the same patch.
#
# Display names: we resolve them at read time against mee6_snapshots so they
# stay in sync with the Discord-side display_name (which changes), instead of
# freezing whatever name the user had at claim time.

def _current_env():
    """Map the request host to a 'prod' / 'dev' label stored on each claim."""
    return 'prod' if 'tools.solprovision.com' in request.host else 'dev'


def _resolve_discord_display_names(discord_ids):
    """Return {user_id: latest display_name} for a list of Discord IDs.

    Uses the most-recent snapshot row per user (mee6_snapshots stores one row
    per snapshot_date). Falls back to {} if the mee6 DB is unreachable so the
    ownership endpoints stay usable even when the user DB is missing locally.
    """
    if not discord_ids:
        return {}
    try:
        udb = get_user_db()
    except sqlite3.Error:
        return {}
    placeholders = ','.join('?' * len(discord_ids))
    rows = udb.execute(f'''
        SELECT user_id, display_name
        FROM discord_members
        WHERE user_id IN ({placeholders})
          AND snapshot_date = (
              SELECT MAX(snapshot_date) FROM discord_members dm2
              WHERE dm2.user_id = discord_members.user_id
          )
    ''', discord_ids).fetchall()
    udb.close()
    return {r['user_id']: r['display_name'] for r in rows}


# ── API: Claim a blueprint ───────────────────────────────────────────────────
# POST /api/crafting/blueprint/<uuid>/claim
# Adds the current user as an owner of this blueprint in the current env.

@app.route('/api/crafting/blueprint/<uuid>/claim', methods=['POST'])
@require_org_member
def api_crafting_blueprint_claim(uuid):
    discord_id = session.get('discord_id')
    # No need for auth check - decorator handles it
    
    env = _current_env()
    # The catalog lookup runs on dataforge.db, but the claim is written to the
    # standalone ownership DB (attached as `own`) so it survives the patch
    # extractions that replace dataforge.db — this is what lets claims persist
    # across patches the way they do in-game.
    conn = get_db_with_ownership()
    # Pull the blueprint's name + patch from the catalog so the ownership row
    # carries enough context to be queryable without re-joining (and so the
    # NOT NULL columns are satisfied). We always claim against the latest
    # patch — claims are per-recipe, not per-patch from the user's POV.
    patch = latest_patch(conn)
    bp = conn.execute('''
        SELECT entity_name, output_display, output_name
        FROM crafting_blueprints
        WHERE uuid = ? AND patch_version = ?
    ''', (uuid, patch)).fetchone()
    if not bp:
        conn.close()
        return jsonify({'error': 'Blueprint not found'}), 404
    blueprint_name = bp['output_display'] or bp['output_name'] or bp['entity_name']
    try:
        conn.execute('''
            INSERT INTO own.blueprint_ownership
                (discord_id, blueprint_uuid, blueprint_name, patch_version, env)
            VALUES (?, ?, ?, ?, ?)
        ''', (discord_id, uuid, blueprint_name, patch, env))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Already claimed by you'}), 409
    finally:
        # `conn` may already be closed in the IntegrityError branch; that's fine.
        try: conn.close()
        except Exception: pass
    return jsonify({
        'success': True,
        'blueprint_uuid': uuid,
        'env': env,
        'discord_id': discord_id,
        'display_name': session.get('callsign'),
    })


# ── API: Unclaim a blueprint ─────────────────────────────────────────────────
# DELETE /api/crafting/blueprint/<uuid>/claim
# Removes only the current user's claim. Other owners are untouched.

@app.route('/api/crafting/blueprint/<uuid>/claim', methods=['DELETE'])
@require_org_member
def api_crafting_blueprint_unclaim(uuid):
    discord_id = session.get('discord_id')
    # No need for auth check - decorator handles it
    
    env = _current_env()
    # Persistent ownership DB — see api_crafting_blueprints_ownership.
    conn = get_ownership_db()
    cur = conn.execute('''
        DELETE FROM blueprint_ownership
        WHERE blueprint_uuid = ? AND discord_id = ? AND env = ?
    ''', (uuid, discord_id, env))
    conn.commit()
    removed = cur.rowcount
    conn.close()
    if removed == 0:
        return jsonify({'error': 'Not claimed by you'}), 404
    return jsonify({'success': True, 'blueprint_uuid': uuid, 'env': env})


# ── API: Ownership summary for many blueprints ───────────────────────────────
# GET /api/crafting/blueprints/ownership?uuids=uuid1,uuid2,...
# Returns one entry per claim (multi-owner), so a UUID can appear multiple
# times. The frontend buckets these by blueprint_uuid to render dots/links.
# Also includes the current user's claim flag for button state.

@app.route('/api/crafting/blueprints/ownership')
def api_crafting_blueprints_ownership():
    uuids_param = request.args.get('uuids', '').strip()
    if not uuids_param:
        return jsonify([])
    uuids = [u.strip() for u in uuids_param.split(',') if u.strip()]
    if not uuids:
        return jsonify([])

    env = _current_env()
    # Ownership lives in the standalone blueprint_ownership.db, which survives
    # patch extractions (dataforge.db is replaced wholesale on each import).
    # Reading it here is what lets claimed blueprints persist across patches.
    conn = get_ownership_db()
    placeholders = ','.join('?' * len(uuids))
    rows = conn.execute(f'''
        SELECT blueprint_uuid, discord_id, claimed_at
        FROM blueprint_ownership
        WHERE blueprint_uuid IN ({placeholders}) AND env = ?
    ''', (*uuids, env)).fetchall()
    conn.close()

    return jsonify([{
        'blueprint_uuid': r['blueprint_uuid'],
        'discord_id':     r['discord_id'],
        'claimed_at':     r['claimed_at'],
    } for r in rows])


# ── API: Owner list for a single blueprint (with Discord names) ──────────────
# GET /api/crafting/blueprint/<uuid>/owners
# Powers the "Current Owners" popup — Discord display names + claim times.

@app.route('/api/crafting/blueprint/<uuid>/owners')
def api_crafting_blueprint_owners(uuid):
    env = _current_env()
    # Persistent ownership DB — see api_crafting_blueprints_ownership.
    conn = get_ownership_db()
    rows = conn.execute('''
        SELECT discord_id, claimed_at
        FROM blueprint_ownership
        WHERE blueprint_uuid = ? AND env = ?
        ORDER BY claimed_at ASC
    ''', (uuid, env)).fetchall()
    conn.close()

    discord_ids = [r['discord_id'] for r in rows]
    name_map = _resolve_discord_display_names(discord_ids)
    return jsonify({
        'blueprint_uuid': uuid,
        'env':            env,
        'owners': [{
            'discord_id':   r['discord_id'],
            'display_name': name_map.get(r['discord_id']) or r['discord_id'],
            'claimed_at':   r['claimed_at'],
        } for r in rows],
    })


# ── API: Claim a ship ─────────────────────────────────────────────────────────
# POST /api/ship/<entity_name>/claim — current user claims this ship hull.
# Mirrors blueprint claiming: standalone ship_ownership.db, env-scoped rows.

# Claims key on the catalog rsi_id (stable, per-variant) so BOTH flyable and
# concept ships are claimable. ship_entity is kept for display/back-compat
# (dataforge name when flyable, else the rsi slug).

@app.route('/api/ship/<int:rsi_id>/claim', methods=['POST'])
@require_org_member
def api_ship_claim(rsi_id):
    _migrate_claims_rsi_id()
    discord_id = session.get('discord_id')
    env = _current_env()
    # 'pledge' (RSI pledge store) or 'in-game' (aUEC); invalid → NULL.
    body = request.get_json(silent=True) or {}
    source = body.get('source')
    if source not in ('pledge', 'in-game'):
        source = None
    conn = get_db()
    patch = latest_patch(conn)
    cat = conn.execute('''
        SELECT rsi_name, rsi_slug, data_name
        FROM ship_catalog WHERE rsi_id = ? AND patch_version = ?
    ''', (rsi_id, patch)).fetchone()
    conn.close()
    if not cat:
        return jsonify({'error': 'Ship not found'}), 404
    ship_name = cat['rsi_name'] or cat['rsi_slug'] or str(rsi_id)
    ship_entity = cat['data_name'] or cat['rsi_slug'] or f'rsi-{rsi_id}'
    own = get_ship_ownership_db()
    # Dedup on the claim key (rsi_id), env-scoped — app-level so concept claims
    # (NULL ship_entity historically) can't slip past the legacy UNIQUE.
    if own.execute('SELECT 1 FROM ship_ownership WHERE discord_id=? AND rsi_id=? AND env=?',
                   (discord_id, rsi_id, env)).fetchone():
        own.close()
        return jsonify({'error': 'Already claimed by you'}), 409
    own.execute('''
        INSERT INTO ship_ownership
            (discord_id, rsi_id, ship_entity, ship_name, patch_version, env, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (discord_id, rsi_id, ship_entity, ship_name, patch, env, source))
    own.commit()
    own.close()
    return jsonify({
        'success': True,
        'rsi_id': rsi_id,
        'env': env,
        'source': source,
        'discord_id': discord_id,
        'display_name': session.get('callsign'),
    })


# ── API: Unclaim a ship ───────────────────────────────────────────────────────
# DELETE /api/ship/<rsi_id>/claim — removes only the current user's claim.

@app.route('/api/ship/<int:rsi_id>/claim', methods=['DELETE'])
@require_org_member
def api_ship_unclaim(rsi_id):
    discord_id = session.get('discord_id')
    env = _current_env()
    own = get_ship_ownership_db()
    cur = own.execute(
        'DELETE FROM ship_ownership WHERE rsi_id = ? AND discord_id = ? AND env = ?',
        (rsi_id, discord_id, env))
    own.commit()
    removed = cur.rowcount
    own.close()
    if removed == 0:
        return jsonify({'error': 'Not claimed by you'}), 404
    return jsonify({'success': True, 'rsi_id': rsi_id, 'env': env})


# ── API: Ownership summary for many ships ─────────────────────────────────────
# GET /api/ships/ownership?ids=1,2,3 — one row per claim (multi-owner);
# the frontend buckets by rsi_id for button state + owner counts.

@app.route('/api/ships/ownership')
def api_ships_ownership():
    _migrate_claims_rsi_id()
    ids_param = request.args.get('ids', '').strip()
    if not ids_param:
        return jsonify([])
    try:
        ids = [int(x) for x in ids_param.split(',') if x.strip()]
    except ValueError:
        return jsonify([])
    if not ids:
        return jsonify([])
    env = _current_env()
    own = get_ship_ownership_db()
    placeholders = ','.join('?' * len(ids))
    rows = own.execute(f'''
        SELECT rsi_id, discord_id, claimed_at
        FROM ship_ownership
        WHERE rsi_id IN ({placeholders}) AND env = ?
    ''', (*ids, env)).fetchall()
    own.close()
    return jsonify([{
        'rsi_id':     r['rsi_id'],
        'discord_id': r['discord_id'],
        'claimed_at': r['claimed_at'],
    } for r in rows])


# ── API: Owner list for a single ship (with Discord names) ───────────────────
# GET /api/ship/<rsi_id>/owners

@app.route('/api/ship/<int:rsi_id>/owners')
def api_ship_owners(rsi_id):
    env = _current_env()
    own = get_ship_ownership_db()
    rows = own.execute('''
        SELECT discord_id, claimed_at, source
        FROM ship_ownership
        WHERE rsi_id = ? AND env = ?
        ORDER BY claimed_at ASC
    ''', (rsi_id, env)).fetchall()
    own.close()
    discord_ids = [r['discord_id'] for r in rows]
    name_map = _resolve_discord_display_names(discord_ids)
    return jsonify({
        'rsi_id': rsi_id,
        'env':    env,
        'owners': [{
            'discord_id':   r['discord_id'],
            'display_name': name_map.get(r['discord_id']) or r['discord_id'],
            'claimed_at':   r['claimed_at'],
            'source':       r['source'],
        } for r in rows],
    })


# ── API: Saved ship loadouts ─────────────────────────────────────────────────
# A loadout is a full-state snapshot of the page (components + armament + power
# grid + master mode). Saving is login-gated and tied to the creator; loading by
# key is open so a ?loadout=<key> URL works for anyone.

import secrets as _secrets

def _gen_loadout_key(conn):
    """Short URL-safe share token, unique within the table."""
    for _ in range(8):
        key = _secrets.token_urlsafe(6)  # ~8 chars
        hit = conn.execute(
            "SELECT 1 FROM saved_loadouts WHERE loadout_key = ?", (key,)).fetchone()
        if not hit:
            return key
    raise RuntimeError("could not generate a unique loadout key")


@app.route('/api/ship/<entity_name>/loadout', methods=['POST'])
@require_org_member
def api_ship_loadout_save(entity_name):
    discord_id = session.get('discord_id')
    env = _current_env()
    body = request.get_json(silent=True) or {}
    loadout = body.get('loadout')
    if not isinstance(loadout, dict):
        return jsonify({'error': 'Missing loadout state'}), 400
    name = (body.get('name') or '').strip() or 'Unnamed loadout'
    name = name[:80]
    # Validate the ship exists (and grab the patch for provenance).
    conn = get_db()
    patch = latest_patch(conn)
    ship = conn.execute(
        "SELECT 1 FROM ships WHERE entity_name = ? AND patch_version = ?",
        (entity_name, patch)).fetchone()
    conn.close()
    if not ship:
        return jsonify({'error': 'Ship not found'}), 404
    payload = json.dumps(loadout, separators=(',', ':'))
    # Guard against runaway payloads (full-state snapshots are tens of KB).
    if len(payload) > 1_000_000:
        return jsonify({'error': 'Loadout too large'}), 413
    own = get_ship_ownership_db()
    key = _gen_loadout_key(own)
    own.execute('''
        INSERT INTO saved_loadouts
            (loadout_key, discord_id, ship_entity, name, loadout_json, patch_version, env)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (key, discord_id, entity_name, name, payload, patch, env))
    own.commit()
    own.close()
    return jsonify({
        'success': True,
        'key': key,
        'name': name,
        'ship_entity': entity_name,
        'url': f"/ships/{entity_name}?loadout={key}",
    })


@app.route('/api/loadout/<key>', methods=['GET'])
def api_loadout_get(key):
    """Open (no auth) — powers ?loadout=<key> share links."""
    env = _current_env()
    own = get_ship_ownership_db()
    row = own.execute('''
        SELECT loadout_key, discord_id, ship_entity, name, loadout_json, created_at
        FROM saved_loadouts WHERE loadout_key = ? AND env = ?
    ''', (key, env)).fetchone()
    own.close()
    if not row:
        return jsonify({'error': 'Loadout not found'}), 404
    try:
        loadout = json.loads(row['loadout_json'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Corrupt loadout'}), 500
    return jsonify({
        'key':         row['loadout_key'],
        'ship_entity': row['ship_entity'],
        'name':        row['name'],
        'created_at':  row['created_at'],
        'loadout':     loadout,
    })


@app.route('/api/ship/<entity_name>/loadouts', methods=['GET'])
@require_org_member
def api_ship_loadouts_mine(entity_name):
    """The current user's saved loadouts for this ship (for the Load popup)."""
    discord_id = session.get('discord_id')
    env = _current_env()
    own = get_ship_ownership_db()
    rows = own.execute('''
        SELECT loadout_key, name, created_at
        FROM saved_loadouts
        WHERE ship_entity = ? AND discord_id = ? AND env = ?
        ORDER BY created_at DESC
    ''', (entity_name, discord_id, env)).fetchall()
    own.close()
    return jsonify({
        'ship_entity': entity_name,
        'loadouts': [{
            'key':        r['loadout_key'],
            'name':       r['name'],
            'created_at': r['created_at'],
        } for r in rows],
    })


@app.route('/api/loadout/<key>', methods=['DELETE'])
@require_org_member
def api_loadout_delete(key):
    """Delete only the current user's own saved loadout."""
    discord_id = session.get('discord_id')
    env = _current_env()
    own = get_ship_ownership_db()
    cur = own.execute(
        "DELETE FROM saved_loadouts WHERE loadout_key = ? AND discord_id = ? AND env = ?",
        (key, discord_id, env))
    own.commit()
    removed = cur.rowcount
    own.close()
    if removed == 0:
        return jsonify({'error': 'Not found or not yours'}), 404
    return jsonify({'success': True, 'key': key})


# ── API: All claims by the current user ──────────────────────────────────────
# GET /api/crafting/blueprints/my-claims
# Reserved for a future "My Claims" page — not used by the card UI today,
# but kept since it's cheap and the auth bug needed fixing anyway.

@app.route('/api/crafting/blueprints/my-claims')
@require_org_member
def api_crafting_blueprints_my_claims():
    discord_id = session.get('discord_id')
    # No need for auth check - decorator handles it
    
    env = _current_env()
    # Persistent ownership DB (attached as `own`) so claims survive patch
    # extractions. Display details are resolved against the current catalog
    # patch rather than the patch the claim was filed under — a claim made in
    # an older patch must still render with the latest name/category.
    conn = get_db_with_ownership()
    patch = latest_patch(conn)
    rows = conn.execute('''
        SELECT
            bo.blueprint_uuid,
            bo.blueprint_name,
            bo.claimed_at,
            cb.output_name,
            COALESCE(
                NULLIF(NULLIF(NULLIF(NULLIF(ic.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
                NULLIF(e.display_name, ''),
                NULLIF(cb.output_display, ''),
                cb.output_name,
                bo.blueprint_name
            ) AS output_display,
            cc.display_path
        FROM own.blueprint_ownership bo
        LEFT JOIN crafting_blueprints cb
            ON cb.uuid = bo.blueprint_uuid
            AND cb.patch_version = ?
        LEFT JOIN crafting_categories cc
            ON cc.id = cb.category_id
        LEFT JOIN entities e
            ON e.uuid = cb.output_uuid
            AND e.patch_version = cb.patch_version
        LEFT JOIN item_components ic
            ON ic.entity_name = cb.output_name
            AND ic.patch_version = cb.patch_version
        WHERE bo.discord_id = ? AND bo.env = ?
        ORDER BY bo.claimed_at DESC
    ''', (patch, discord_id, env)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])
    
    
# ═════════════════════════════════════════════════════════════════════════════
# OFFICER DASHBOARD
# ═════════════════════════════════════════════════════════════════════════════
# Org-wide ownership intelligence. All endpoints are gated by @require_officer
# (rank >= 5) and pin env='prod' regardless of where the request is served from
# — officers want a single org-wide picture, not separate dev/prod stats.

OFFICER_OWNERSHIP_ENV = 'prod'

# Mirrors the item_category CASE in /api/crafting/blueprints. Kept as a SQL
# fragment so the officer queries can reuse the same bucketing without drift.
ITEM_CATEGORY_CASE = """
    CASE
        WHEN c.top_level = 'fpsgear'     AND c.mid_level = 'armour'  THEN 'armor'
        WHEN c.top_level = 'fpsgear'     AND c.mid_level = 'ammo'    THEN 'ammo'
        WHEN c.top_level = 'fpsgear'     AND c.mid_level = 'weapons' THEN 'fps_weapons'
        WHEN c.top_level = 'vehiclegear' AND c.mid_level = 'weapons' THEN 'ship_weapons'
        WHEN c.top_level = 'vehiclegear'                             THEN 'ship_components'
        ELSE NULL
    END
"""


def _officer_blueprints(conn, patch):
    """Pull the same blueprint set the catalog page shows, tagged with category.

    Mirrors the filters in /api/crafting/blueprints (template exclusions, hidden
    top levels) so the coverage tile's totals match what users actually see.
    """
    placeholders = ",".join("?" for _ in HIDDEN_CRAFTING_TOP_LEVELS) or "''"
    rows = conn.execute(f"""
        SELECT
            b.uuid,
            COALESCE(
                NULLIF(NULLIF(NULLIF(NULLIF(ic.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
                NULLIF(e.display_name, ''),
                NULLIF(b.output_display, ''),
                b.output_name,
                b.entity_name
            ) AS name,
            c.sub_level,
            c.sub_display,
            c.sub_sub_level,
            c.sub_sub_display,
            {ITEM_CATEGORY_CASE} AS item_category
        FROM crafting_blueprints b
        LEFT JOIN crafting_categories c ON c.id = b.category_id
        LEFT JOIN entities e
            ON e.uuid = b.output_uuid
           AND e.patch_version = b.patch_version
        LEFT JOIN item_components ic
            ON ic.entity_name = b.output_name
           AND ic.patch_version = b.patch_version
        WHERE b.patch_version = ?
          AND b.entity_name NOT LIKE '%\\_template' ESCAPE '\\'
          AND (c.top_level IS NULL OR c.top_level NOT IN ({placeholders}))
    """, (patch, *HIDDEN_CRAFTING_TOP_LEVELS)).fetchall()
    return rows


# ── API: Coverage heatmap ─────────────────────────────────────────────────────
# GET /api/officers/coverage
# Returns nested ownership coverage: category → sub_level → sub_sub_level →
# blueprints, with totals + owned counts at every level. Powers the drilldown.

@app.route('/api/officers/coverage')
@require_officer
def api_officers_coverage():
    conn = get_db()
    patch = latest_patch(conn)
    bps = _officer_blueprints(conn, patch)
    conn.close()

    # All claims in prod env, keyed by blueprint_uuid → list of discord_ids.
    own = get_ownership_db()
    own_rows = own.execute('''
        SELECT blueprint_uuid, discord_id
        FROM blueprint_ownership
        WHERE env = ?
    ''', (OFFICER_OWNERSHIP_ENV,)).fetchall()
    own.close()

    owners_by_bp = {}
    for r in own_rows:
        owners_by_bp.setdefault(r['blueprint_uuid'], []).append(r['discord_id'])
    name_map = _resolve_discord_display_names(list({d for ds in owners_by_bp.values() for d in ds}))

    # Build the nested tree. Buckets without a category map are dropped (a
    # blueprint whose top_level isn't one of our 5 buckets — rare but possible).
    categories = {}
    for bp in bps:
        cat = bp['item_category']
        if not cat:
            continue
        cat_node = categories.setdefault(cat, {"totals": {"total": 0, "owned": 0}, "sub_levels": {}})
        cat_node["totals"]["total"] += 1
        is_owned = bool(owners_by_bp.get(bp['uuid']))
        if is_owned:
            cat_node["totals"]["owned"] += 1

        sub = bp['sub_level'] or '_none'
        sub_disp = bp['sub_display'] or sub
        sub_node = cat_node["sub_levels"].setdefault(sub, {
            "display": sub_disp, "totals": {"total": 0, "owned": 0},
            "sub_sub_levels": {}, "blueprints": [],
        })
        sub_node["totals"]["total"] += 1
        if is_owned:
            sub_node["totals"]["owned"] += 1

        owners_list = [
            {"discord_id": d, "display_name": name_map.get(d) or d}
            for d in owners_by_bp.get(bp['uuid'], [])
        ]
        leaf_bp = {"uuid": bp['uuid'], "name": bp['name'] or bp['uuid'], "owners": owners_list}

        if bp['sub_sub_level']:
            ss = bp['sub_sub_level']
            ss_disp = bp['sub_sub_display'] or ss
            ss_node = sub_node["sub_sub_levels"].setdefault(ss, {
                "display": ss_disp, "totals": {"total": 0, "owned": 0}, "blueprints": [],
            })
            ss_node["totals"]["total"] += 1
            if is_owned:
                ss_node["totals"]["owned"] += 1
            ss_node["blueprints"].append(leaf_bp)
        else:
            # Flat taxonomy (e.g. FPS weapons under a single sub_level) —
            # blueprints attach directly to the sub_level node.
            sub_node["blueprints"].append(leaf_bp)

    # Compute coverage_pct on every bucket. Done after assembly so we don't
    # divide by zero on partial groups during the loop.
    def add_pct(totals):
        t, o = totals["total"], totals["owned"]
        totals["coverage_pct"] = (o * 100.0 / t) if t else 0.0

    for cat_node in categories.values():
        add_pct(cat_node["totals"])
        for sub_node in cat_node["sub_levels"].values():
            add_pct(sub_node["totals"])
            for ss_node in sub_node["sub_sub_levels"].values():
                add_pct(ss_node["totals"])

    return jsonify({"env": OFFICER_OWNERSHIP_ENV, "patch": patch, "categories": categories})


# ── API: Member leaderboard ───────────────────────────────────────────────────
# GET /api/officers/members
# Per-member totals + per-category counts, with Discord display names.

# ══════════════════════════════════════════════════════════════════════
# PORTAL ADMINISTRATION — Division Readiness
# ══════════════════════════════════════════════════════════════════════
# Officers set division posture here; the portal renders it read-only. This app
# is the only writer. Any officer may edit any division on purpose: Science has
# no rank-5 member, so division-scoped editing would leave it permanently
# unmanageable. Accountability is by attribution (updated_by_name), not by lock.

@app.route('/api/officers/readiness', methods=['GET'])
@require_officer
def api_officers_readiness():
    """Current readiness for all divisions, plus the status vocabulary and a
    short change history for the HQ panel."""
    conn = get_org_status_db()
    try:
        return jsonify({
            "divisions": org_status.get_readiness(conn),
            "statuses": [dict(s) for s in org_status.STATUSES],
            "recent": org_status.recent_changes(conn, limit=10),
        })
    finally:
        conn.close()


@app.route('/api/officers/readiness', methods=['PUT'])
@require_officer
def api_officers_readiness_save():
    """Save readiness. Accepts the whole form; writes only what changed."""
    payload = request.get_json(silent=True) or {}
    submitted = payload.get("divisions")
    if not isinstance(submitted, list):
        return jsonify({"error": "expected a 'divisions' list"}), 400

    actor_id = session.get('discord_id')
    actor_name = session.get('callsign') or session.get('username')

    conn = get_org_status_db()
    try:
        current = {d["code"]: d for d in org_status.get_readiness(conn)}
        changed = []
        for item in submitted:
            if not isinstance(item, dict):
                return jsonify({"error": "malformed division entry"}), 400
            code = item.get("code")
            status = item.get("status")
            posture = (item.get("posture") or "").strip()

            existing = current.get(code)
            if existing is None:
                return jsonify({"error": f"unknown division: {code}"}), 400

            # Only write what actually moved. Saving all four on every submit
            # would stamp every division with this officer's name and fill the
            # change log with entries that record nothing.
            if existing["status"] == status and existing["posture"] == posture:
                continue

            try:
                changed.append(org_status.set_readiness(
                    conn, code, status, posture, actor_id, actor_name))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        return jsonify({
            "saved": len(changed),
            "changed": changed,
            "divisions": org_status.get_readiness(conn),
        })
    finally:
        conn.close()


@app.route('/api/officers/tasking', methods=['GET'])
@require_officer
def api_officers_tasking():
    """The four Upcoming Tasking slots."""
    conn = get_org_status_db()
    try:
        return jsonify({"slots": org_status.get_tasking(conn),
                        "slot_count": org_status.TASKING_SLOTS})
    finally:
        conn.close()


@app.route('/api/officers/tasking', methods=['PUT'])
@require_officer
def api_officers_tasking_save():
    """Save tasking. Accepts all slots; writes only what changed.

    Same reasoning as readiness: saving every slot on each submit would stamp
    all four with this officer's name whether or not they touched them.
    """
    payload = request.get_json(silent=True) or {}
    submitted = payload.get("slots")
    if not isinstance(submitted, list):
        return jsonify({"error": "expected a 'slots' list"}), 400

    actor_id = session.get('discord_id')
    actor_name = session.get('callsign') or session.get('username')

    conn = get_org_status_db()
    try:
        current = {row["slot"]: row for row in org_status.get_tasking(conn)}
        changed = []
        for item in submitted:
            if not isinstance(item, dict):
                return jsonify({"error": "malformed slot entry"}), 400
            try:
                slot = int(item.get("slot"))
            except (TypeError, ValueError):
                return jsonify({"error": f"bad slot: {item.get('slot')!r}"}), 400

            title = (item.get("title") or "").strip()
            date = (item.get("tasking_date") or "").strip() or None

            existing = current.get(slot)
            if existing is None:
                return jsonify({"error": f"unknown slot: {slot}"}), 400
            if existing["title"] == title and existing["tasking_date"] == date:
                continue

            try:
                changed.append(org_status.set_tasking(
                    conn, slot, title, date, actor_id, actor_name))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        return jsonify({"saved": len(changed), "changed": changed,
                        "slots": org_status.get_tasking(conn)})
    finally:
        conn.close()


# ── OpOrds ────────────────────────────────────────────────────────────
# The editor is its own officer-only page; HQ Admin lists them.

@app.route('/officers/opord/new')
@app.route('/officers/opord/<int:opord_id>')
def opord_editor(opord_id=None):
    """OpOrd editor. Rank 5+ only, matching /officers itself."""
    if rank_int(session.get('rank')) < 5:
        return redirect('/')
    return render_template("opord_edit.html",
                           active_page="/officers",
                           opord_id=opord_id,
                           commanders=commander_options(),
                           signal_types=list(opord.SIGNAL_TYPES),
                           statuses=list(opord.STATUSES),
                           signal_note=opord.SIGNAL_NOTE,
                           default_muster_time=opord.DEFAULT_MUSTER_TIME,
                           default_muster_tz=opord.DEFAULT_MUSTER_TZ)


@app.route('/api/officers/opords')
@require_officer
def api_opords_list():
    conn = get_opord_db()
    try:
        return jsonify({"opords": opord.list_opords(conn),
                        "current": (opord.current_opord(conn) or {}).get("id")})
    finally:
        conn.close()


@app.route('/api/officers/opord/<int:opord_id>')
@require_officer
def api_opord_get(opord_id):
    conn = get_opord_db()
    try:
        row = opord.get(conn, opord_id)
        return (jsonify(row) if row else (jsonify({"error": "Not found"}), 404))
    finally:
        conn.close()


@app.route('/api/officers/opord', methods=['POST'])
@require_officer
def api_opord_create():
    payload = request.get_json(silent=True) or {}
    actor = session.get('callsign') or session.get('username')
    conn = get_opord_db()
    try:
        new_id = opord.create(conn, payload.get("header") or {},
                              payload.get("body"), actor=actor)
        return jsonify({"ok": True, "id": new_id})
    except opord.ValidationError as exc:
        return jsonify({"ok": False, "errors": exc.errors}), 400
    finally:
        conn.close()


@app.route('/api/officers/opord/<int:opord_id>', methods=['PUT'])
@require_officer
def api_opord_update(opord_id):
    payload = request.get_json(silent=True) or {}
    actor = session.get('callsign') or session.get('username')
    conn = get_opord_db()
    try:
        ok = opord.update(conn, opord_id, payload.get("header") or {},
                          payload.get("body"), actor=actor)
        return (jsonify({"ok": True, "id": opord_id}) if ok
                else (jsonify({"ok": False, "error": "Not found"}), 404))
    except opord.ValidationError as exc:
        return jsonify({"ok": False, "errors": exc.errors}), 400
    finally:
        conn.close()


@app.route('/api/officers/opord/<int:opord_id>/duplicate', methods=['POST'])
@require_officer
def api_opord_duplicate(opord_id):
    actor = session.get('callsign') or session.get('username')
    conn = get_opord_db()
    try:
        new_id = opord.duplicate(conn, opord_id, actor=actor)
        return (jsonify({"ok": True, "id": new_id}) if new_id
                else (jsonify({"ok": False, "error": "Not found"}), 404))
    finally:
        conn.close()


@app.route('/api/officers/opord/<int:opord_id>/post', methods=['POST'])
@require_officer
def api_opord_post(opord_id):
    """Make this the Mission Board OpOrd; whatever was posted is archived."""
    actor = session.get('callsign') or session.get('username')
    conn = get_opord_db()
    try:
        ok = opord.post(conn, opord_id, actor=actor)
        return (jsonify({"ok": True}) if ok
                else (jsonify({"ok": False, "error": "Not found"}), 404))
    finally:
        conn.close()


@app.route('/api/officers/opord/<int:opord_id>', methods=['DELETE'])
@require_officer
def api_opord_delete(opord_id):
    conn = get_opord_db()
    try:
        return jsonify({"ok": opord.delete(conn, opord_id)})
    finally:
        conn.close()


@app.route('/api/officers/applications')
@require_officer
def api_officers_applications():
    """Membership applications for the HQ review panel.

    Returns the full rows rather than a summary: there are a few dozen of them,
    so paging or a per-row detail fetch would be machinery for nothing.
    """
    limit = request.args.get('limit', default=50, type=int)
    limit = max(1, min(limit, 200))
    status = request.args.get('status') or None

    try:
        conn = get_applications_db()
    except sqlite3.OperationalError as exc:
        # The portal creates this file on its first submission. Before that it
        # legitimately does not exist — an empty panel, not an error.
        app.logger.info('applications.db unavailable (%s)', exc)
        return jsonify({'available': False, 'counts': {}, 'applications': []})

    try:
        if status and status not in applications.STATUSES:
            return jsonify({'error': f'unknown status: {status}'}), 400
        return jsonify({
            'available': True,
            'counts': applications.counts_by_status(conn),
            'statuses': list(applications.STATUSES),
            'applications': applications.list_applications(conn, status, limit),
        })
    finally:
        conn.close()


@app.route('/api/officers/members')
@require_officer
def api_officers_members():
    conn = get_db()
    patch = latest_patch(conn)
    bps = _officer_blueprints(conn, patch)
    conn.close()
    bp_cat = {row['uuid']: row['item_category'] for row in bps}

    own = get_ownership_db()
    own_rows = own.execute('''
        SELECT blueprint_uuid, discord_id
        FROM blueprint_ownership
        WHERE env = ?
    ''', (OFFICER_OWNERSHIP_ENV,)).fetchall()
    own.close()

    # member_id → {total, by_category: {...}}
    by_member = {}
    for r in own_rows:
        did = r['discord_id']
        cat = bp_cat.get(r['blueprint_uuid'])
        if not cat:
            # Ownership row points at a BP we no longer surface (template or
            # hidden top-level). Skip rather than skew the leaderboard.
            continue
        m = by_member.setdefault(did, {"total": 0, "by_category": {}})
        m["total"] += 1
        m["by_category"][cat] = m["by_category"].get(cat, 0) + 1

    name_map = _resolve_discord_display_names(list(by_member.keys()))
    members = [
        {"discord_id": did, "display_name": name_map.get(did) or did, **stats}
        for did, stats in by_member.items()
    ]
    members.sort(key=lambda m: m["total"], reverse=True)
    return jsonify({"env": OFFICER_OWNERSHIP_ENV, "patch": patch, "members": members})


# ── API: Member's full claim list ─────────────────────────────────────────────
# GET /api/officers/member/<discord_id>/claims
# Drilldown for the member-stats tile.

@app.route('/api/officers/member/<discord_id>/claims')
@require_officer
def api_officers_member_claims(discord_id):
    conn = get_db_with_ownership()
    patch = latest_patch(conn)
    placeholders = ",".join("?" for _ in HIDDEN_CRAFTING_TOP_LEVELS) or "''"
    rows = conn.execute(f'''
        SELECT
            bo.blueprint_uuid,
            bo.claimed_at,
            COALESCE(
                NULLIF(NULLIF(NULLIF(NULLIF(ic.display_name, ''), '<= PLACEHOLDER =>'), 'PLACEHOLDER'), '@LOC_PLACEHOLDER'),
                NULLIF(e.display_name, ''),
                NULLIF(b.output_display, ''),
                b.output_name,
                bo.blueprint_name
            ) AS blueprint_name,
            {ITEM_CATEGORY_CASE} AS item_category,
            c.sub_display,
            c.sub_sub_display
        FROM own.blueprint_ownership bo
        LEFT JOIN crafting_blueprints b
            ON b.uuid = bo.blueprint_uuid AND b.patch_version = ?
        LEFT JOIN crafting_categories c ON c.id = b.category_id
        LEFT JOIN entities e
            ON e.uuid = b.output_uuid AND e.patch_version = b.patch_version
        LEFT JOIN item_components ic
            ON ic.entity_name = b.output_name AND ic.patch_version = b.patch_version
        WHERE bo.discord_id = ? AND bo.env = ?
          AND (c.top_level IS NULL OR c.top_level NOT IN ({placeholders}))
        ORDER BY bo.claimed_at DESC
    ''', (patch, discord_id, OFFICER_OWNERSHIP_ENV, *HIDDEN_CRAFTING_TOP_LEVELS)).fetchall()
    conn.close()
    return jsonify({"discord_id": discord_id, "claims": [dict(r) for r in rows]})


# ── API: Shopping list (merged ingredients for N blueprints) ──────────────────
# POST /api/officers/shopping-list
# Body: { "items": [{"uuid": "...", "quantity": 5}, ...] }
# Walks each blueprint's slots/ingredients and aggregates by resource. Returns
# resources (cost_type='resource', totals in cSCU) and items (counts) split out,
# both sorted by total descending.

@app.route('/api/officers/shopping-list', methods=['POST'])
@require_officer
def api_officers_shopping_list():
    body = request.get_json(silent=True) or {}
    items = body.get('items') or []
    if not items:
        return jsonify({"resources": [], "items": []})

    conn = get_db()
    patch = latest_patch(conn)

    # key = (cost_type, resource_uuid OR resource_name) — uuid is preferred since
    # CIG sometimes has multiple resource_name spellings that share a uuid.
    merged = {}
    for entry in items:
        uuid = (entry.get('uuid') or '').strip()
        try:
            qty = int(entry.get('quantity') or 1)
        except (TypeError, ValueError):
            qty = 1
        if not uuid or qty <= 0:
            continue

        ing_rows = conn.execute('''
            SELECT
                ci.cost_type,
                ci.resource_uuid,
                ci.resource_name,
                COALESCE(e.display_name, ci.display_name, ci.resource_name) AS display_name,
                ci.quantity
            FROM crafting_slots s
            JOIN crafting_ingredients ci ON ci.slot_id = s.id
            LEFT JOIN entities e
                ON e.uuid = ci.resource_uuid AND e.patch_version = ci.patch_version
            WHERE s.blueprint_uuid = ? AND s.patch_version = ?
        ''', (uuid, patch)).fetchall()

        for ing in ing_rows:
            key = (ing['cost_type'], ing['resource_uuid'] or ing['resource_name'])
            if key not in merged:
                merged[key] = {
                    "cost_type":    ing['cost_type'],
                    "resource_uuid": ing['resource_uuid'],
                    "resource_name": ing['resource_name'],
                    "display_name": ing['display_name'] or ing['resource_name'] or '—',
                    "total_qty":    0,
                }
            merged[key]["total_qty"] += (ing['quantity'] or 0) * qty

    conn.close()

    resources, plain_items = [], []
    for m in merged.values():
        if m["cost_type"] == "resource":
            # Game stores resource qty in SCU; cards show cSCU (×100). Stick to
            # cSCU here so officers can compare against in-game shop displays.
            resources.append({
                "display_name": m["display_name"],
                "resource_name": m["resource_name"],
                "resource_uuid": m["resource_uuid"],
                "total_qty":   m["total_qty"],
                "total_cscu":  m["total_qty"] * 100,
            })
        else:
            plain_items.append({
                "display_name": m["display_name"],
                "resource_name": m["resource_name"],
                "resource_uuid": m["resource_uuid"],
                "total_qty": m["total_qty"],
            })
    resources.sort(key=lambda r: r["total_qty"], reverse=True)
    plain_items.sort(key=lambda r: r["total_qty"], reverse=True)
    return jsonify({"resources": resources, "items": plain_items})


# ── API: Org fleet composition ────────────────────────────────────────────────
# GET /api/officers/fleet
# Every prod ship claim, joined to the catalog and rolled up two ways: by hull
# (what the org flies) and by member (who brings what). Built for Org Night
# planning, so the headline numbers are crew seats and cargo, not just hulls.
#
# Returns the whole fleet in one payload and lets the client filter. The org's
# claim count is in the hundreds at most, so paging/server-side filtering would
# cost a round trip per keystroke to save nothing.

# Catalog `size` is a word, not a number — order it by hull scale for display
# rather than the alphabetical order SQL would give.
FLEET_SIZE_ORDER = ['snub', 'small', 'medium', 'large', 'capital', 'vehicle']


def _fleet_claims(own_conn):
    """Deduped prod ship claims, oldest first.

    Dedup is on (discord_id, rsi_id) because the table's legacy UNIQUE is
    (discord_id, ship_entity, patch_version) — a member who claimed the same
    hull under two patches has two rows, and counting both would inflate the
    fleet. Claiming is one-per-member-per-hull (server.py api_ship_claim
    dedups on rsi_id), so distinct pairs is the true hull count.

    Oldest row wins: it carries the original claimed_at.
    """
    rows = own_conn.execute('''
        SELECT discord_id, rsi_id, ship_name, source, claimed_at
        FROM ship_ownership
        WHERE env = ? AND rsi_id IS NOT NULL
        ORDER BY claimed_at
    ''', (OFFICER_OWNERSHIP_ENV,)).fetchall()
    seen, claims = set(), []
    for r in rows:
        key = (r['discord_id'], r['rsi_id'])
        if key in seen:
            continue
        seen.add(key)
        claims.append(r)
    return claims


@app.route('/api/officers/fleet')
@require_officer
def api_officers_fleet():
    # Legacy claims predate the rsi_id column; backfill before reading or they
    # drop out of the join. Idempotent and self-guarding.
    _migrate_claims_rsi_id()

    conn = get_db()
    patch = latest_patch(conn)
    # Same notion of "an ownable ship" as /api/ships and the claim endpoint:
    # every ship_catalog row at this patch, concepts included. focus is TRIMmed
    # because the source data has both 'Racing' and 'Racing ' as distinct values.
    cat_rows = conn.execute('''
        SELECT rsi_id, rsi_name, rsi_slug, manufacturer, manufacturer_code,
               TRIM(COALESCE(focus, '')) AS focus, type, size,
               production_status, flyable, cargo_scu, max_crew, min_crew
        FROM ship_catalog
        WHERE patch_version = ?
    ''', (patch,)).fetchall()
    conn.close()
    catalog = {r['rsi_id']: r for r in cat_rows}

    own = get_ship_ownership_db()
    claims = _fleet_claims(own)
    own.close()

    names = _resolve_discord_display_names(
        sorted({c['discord_id'] for c in claims}))

    by_ship, by_member, stale = {}, {}, 0
    for c in claims:
        cat = catalog.get(c['rsi_id'])
        if not cat:
            # Claim on a hull the current patch no longer lists. Counted only
            # as a stale tally — mirrors how the coverage tile drops claims on
            # blueprints that fell out of the catalog.
            stale += 1
            continue

        crew  = cat['max_crew'] or 0
        cargo = cat['cargo_scu'] or 0

        ship = by_ship.get(c['rsi_id'])
        if not ship:
            ship = by_ship[c['rsi_id']] = {
                'rsi_id':            c['rsi_id'],
                'name':              cat['rsi_name'] or cat['rsi_slug'] or c['ship_name'],
                'manufacturer':      cat['manufacturer'] or '—',
                'manufacturer_code': cat['manufacturer_code'] or '',
                'type':              cat['type'] or 'unknown',
                'focus':             cat['focus'] or '',
                'size':              cat['size'] or 'unknown',
                'production_status': cat['production_status'] or '',
                'flyable':           bool(cat['flyable']),
                'max_crew':          crew,
                'cargo_scu':         cargo,
                'count':             0,
                'owners':            [],
            }
        ship['count'] += 1
        ship['owners'].append({
            'discord_id':   c['discord_id'],
            'display_name': names.get(c['discord_id']) or c['discord_id'],
            'source':       c['source'],
            'claimed_at':   c['claimed_at'],
        })

        m = by_member.get(c['discord_id'])
        if not m:
            m = by_member[c['discord_id']] = {
                'discord_id':   c['discord_id'],
                'display_name': names.get(c['discord_id']) or c['discord_id'],
                'ships': 0, 'crew_seats': 0, 'cargo_scu': 0,
                'pledge': 0, 'in_game': 0, 'types': {},
            }
        m['ships']      += 1
        m['crew_seats'] += crew
        m['cargo_scu']  += cargo
        if c['source'] == 'pledge':
            m['pledge'] += 1
        elif c['source'] == 'in-game':
            m['in_game'] += 1
        t = cat['type'] or 'unknown'
        m['types'][t] = m['types'].get(t, 0) + 1

    ships = sorted(by_ship.values(), key=lambda s: (-s['count'], s['name']))
    for s in ships:
        s['owners'].sort(key=lambda o: (o['display_name'] or '').lower())
    members = sorted(by_member.values(),
                     key=lambda m: (-m['ships'], (m['display_name'] or '').lower()))

    def breakdown(key, order=None):
        """Hull counts grouped by a catalog facet, biggest first (or in the
        given display order when one is supplied)."""
        agg = {}
        for s in ships:
            agg[s[key]] = agg.get(s[key], 0) + s['count']
        items = [{'key': k, 'count': v} for k, v in agg.items()]
        if order:
            items.sort(key=lambda i: order.index(i['key'])
                       if i['key'] in order else len(order))
        else:
            items.sort(key=lambda i: -i['count'])
        return items

    total_hulls = sum(s['count'] for s in ships)
    return jsonify({
        'env':   OFFICER_OWNERSHIP_ENV,
        'patch': patch,
        'totals': {
            'hulls':          total_hulls,
            'models':         len(ships),
            'owners':         len(members),
            'crew_seats':     sum(m['crew_seats'] for m in members),
            'cargo_scu':      sum(m['cargo_scu'] for m in members),
            'catalog_models': len(catalog),
            'concepts':       sum(s['count'] for s in ships if not s['flyable']),
            'stale_claims':   stale,
        },
        'breakdowns': {
            'type': breakdown('type'),
            'size': breakdown('size', FLEET_SIZE_ORDER),
        },
        'ships':   ships,
        'members': members,
    })


# ── API: Warehouse ore inventory ──────────────────────────────────────────────
# GET /api/officers/warehouse
# Org ore reserves, mirrored from the warehouse Google Sheet by the cron pull
# script. Read-only — returns the current snapshot plus freshness metadata so
# the dashboard can flag stale data.

@app.route('/api/officers/warehouse')
@require_officer
def api_officers_warehouse():
    conn = get_warehouse_db()
    rows = conn.execute(
        "SELECT material, qty, quality, location "
        "FROM warehouse_inventory ORDER BY row_index, material"
    ).fetchall()
    meta = {r['key']: r['value'] for r in conn.execute(
        "SELECT key, value FROM warehouse_meta")}
    conn.close()

    pulled_at = None
    if meta.get('pulled_at'):
        try:
            pulled_at = int(meta['pulled_at'])
        except (TypeError, ValueError):
            pulled_at = None

    return jsonify({
        "items":     [dict(r) for r in rows],
        "pulled_at": pulled_at,
        "status":    meta.get('status'),
        "source":    meta.get('source'),
    })



# ─────────────────────────────────────────────────────────────────────────────
# SHOPS — Add these routes to server.py
# ─────────────────────────────────────────────────────────────────────────────
 
# ── Page route ────────────────────────────────────────────────────────────────
#@app.route('/shops')
#def shops():
#    db   = get_db()
#    row  = db.execute(
#        "SELECT patch_version FROM patch_history ORDER BY imported_at DESC LIMIT 1"
#    ).fetchone()
#    patch_version = row['patch_version'] if row else ''
#    return render_template('shops.html', active_page='shops', patch_version=patch_version)
 
 
# ── Meta: locations + shop index for sidebar dropdowns ───────────────────────
@app.route('/api/shops/meta')
def api_shops_meta():
    patch = request.args.get('patch', '')
    db    = get_db()
 
    # Distinct locations sorted
    locs = db.execute("""
        SELECT DISTINCT location
        FROM shop_inventories
        WHERE patch_version = ?
          AND location NOT IN ('Unknown','FeatureTest')
        ORDER BY location
    """, (patch,)).fetchall()
 
    # Shop index: location → [store names]
    shops = db.execute("""
        SELECT DISTINCT location, store_name
        FROM shop_inventories
        WHERE patch_version = ?
          AND location NOT IN ('Unknown','FeatureTest')
        ORDER BY location, store_name
    """, (patch,)).fetchall()
 
    shop_index = {}
    for row in shops:
        shop_index.setdefault(row['location'], []).append(row['store_name'])
 
    return jsonify({
        'locations':  [r['location'] for r in locs],
        'shop_index': shop_index,
    })
 
 
# ── Search: find items by name/store/location ─────────────────────────────────
@app.route('/api/shops/search')
def api_shops_search():
    patch    = request.args.get('patch', '')
    q        = request.args.get('q', '').strip()
    location = request.args.get('location', '')
    txn      = request.args.get('txn', 'both')      # 'buy' | 'sell' | 'both'
    category = request.args.get('category', '')      # 'mining' | ''
 
    if not q:
        return jsonify([])
 
    db     = get_db()
    like_q = f'%{q}%'
 
    # Transaction filter clause
    if txn == 'buy':
        txn_clause = 'AND buy_price > 0'
    elif txn == 'sell':
        txn_clause = 'AND sell_price > 0'
    else:
        txn_clause = ''
 
    # Location filter
    loc_clause  = 'AND location = ?' if location else ''
    loc_params  = (location,) if location else ()
 
    # Category filter — mining = sell-only items (resources sold to shops)
    cat_clause = ''
    if category == 'mining':
        cat_clause = 'AND sell_price > 0 AND buy_price = 0'
 
    sql = f"""
        SELECT
            si.item_uuid,
            si.display_name,
            si.entity_name,
            si.store_name,
            si.location,
            si.buy_price,
            si.sell_price,
            si.current_inventory,
            si.max_inventory,
            CASE WHEN si.buy_price = 0 AND si.sell_price > 0 THEN 1 ELSE 0 END AS is_mining
        FROM shop_inventories si
        WHERE si.patch_version = ?
          AND (
              si.display_name  LIKE ? OR
              si.entity_name   LIKE ? OR
              si.store_name    LIKE ? OR
              si.location      LIKE ?
          )
          AND si.location NOT IN ('Unknown','FeatureTest')
          {txn_clause}
          {loc_clause}
          {cat_clause}
        ORDER BY si.display_name, si.store_name, si.location
        LIMIT 500
    """
 
    params = (patch, like_q, like_q, like_q, like_q) + loc_params
    rows   = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])
 
 
# ── Item detail: all listings for a specific item name ────────────────────────
@app.route('/api/shops/item')
def api_shops_item():
    patch    = request.args.get('patch', '')
    name     = request.args.get('name', '').strip()
    location = request.args.get('location', '')
    txn      = request.args.get('txn', 'both')
 
    if not name:
        return jsonify({'listings': []})
 
    db = get_db()
 
    if txn == 'buy':
        txn_clause = 'AND buy_price > 0'
    elif txn == 'sell':
        txn_clause = 'AND sell_price > 0'
    else:
        txn_clause = ''
 
    loc_clause = 'AND location = ?' if location else ''
    loc_params = (location,) if location else ()
 
    sql = f"""
        SELECT
            item_uuid, display_name, entity_name,
            store_name, location,
            buy_price, sell_price,
            current_inventory, max_inventory
        FROM shop_inventories
        WHERE patch_version = ?
          AND (display_name = ? OR entity_name = ?)
          AND location NOT IN ('Unknown','FeatureTest')
          {txn_clause}
          {loc_clause}
        ORDER BY buy_price ASC, sell_price DESC
    """
 
    params = (patch, name, name) + loc_params
    rows   = db.execute(sql, params).fetchall()
    return jsonify({'listings': [dict(r) for r in rows]})
 
 
# ── Browse: full inventory for a specific store + location ────────────────────
@app.route('/api/shops/browse')
def api_shops_browse():
    patch    = request.args.get('patch', '')
    location = request.args.get('location', '')
    store    = request.args.get('store', '')
 
    if not location or not store:
        return jsonify({'items': []})
 
    db   = get_db()
    rows = db.execute("""
        SELECT
            item_uuid, display_name, entity_name,
            store_name, location,
            buy_price, sell_price,
            current_inventory, max_inventory
        FROM shop_inventories
        WHERE patch_version = ?
          AND location      = ?
          AND store_name    = ?
        ORDER BY display_name, entity_name
    """, (patch, location, store)).fetchall()
 
    return jsonify({'items': [dict(r) for r in rows]})

# ══════════════════════════════════════════════════════════════════════
# CARGO PLANNER — reference data APIs (read-only from dataforge.db)
# ══════════════════════════════════════════════════════════════════════
# The cargo planner page lets users plan multi-mission hauling routes. These
# endpoints feed its dropdowns: ships, quantum drives (with stock default),
# systems, and locations. Plan persistence lives in cargo_planner.db (added
# separately). All reference data here is public game data — no auth needed.

CARGO_SYSTEMS = [
    {"code": "STANTON", "name": "Stanton", "star_key": "StantonStar"},
    {"code": "PYRO",    "name": "Pyro",    "star_key": "PyroStar"},
    {"code": "NYX",     "name": "Nyx",     "star_key": "NyxStar"},
]
_STAR_KEY_TO_SYSTEM = {s["star_key"]: s["code"] for s in CARGO_SYSTEMS}

# Known non-PU / leftover test assets to hide from the planner. 'Ellis3'
# ("Green") is a stray test planet CIG parented to Stanton's star — not a real
# Stanton body. Matched by location_key prefix (catches its OMs / children too).
CARGO_EXCLUDED_KEY_PREFIXES = ("Ellis",)

def _is_excluded_nav_key(key):
    return bool(key) and any(key.startswith(pre) for pre in CARGO_EXCLUDED_KEY_PREFIXES)


def _qd_display_name(entity_name):
    """Fallback prettifier for a quantum drive entity_name when the component
    record has no display_name. Drops manufacturer + size tokens, keeping just
    the model: 'qdrv_acas_s01_foxfire_scitem' → 'Foxfire'."""
    if not entity_name:
        return ""
    parts = entity_name.split("_")
    model_parts = [
        p for p in parts
        if p not in ("qdrv", "scitem")
        and p not in MFR_MAP
        and not re.fullmatch(r"s\d{1,2}", p)
    ]
    return " ".join(w.capitalize() for w in model_parts) if model_parts else entity_name


def _qd_name(comp_name, entity_name):
    """Prefer the component's localized display_name, but fall back to the
    entity-parsed name when it's an unresolved placeholder."""
    if comp_name and "PLACEHOLDER" not in comp_name and "UNINITIALIZED" not in comp_name \
            and not comp_name.startswith("<="):
        return comp_name
    return _qd_display_name(entity_name)


@app.route("/api/cargo/systems")
def api_cargo_systems():
    return jsonify([{"code": s["code"], "name": s["name"]} for s in CARGO_SYSTEMS])


@app.route("/api/cargo/ships")
def api_cargo_ships():
    """Ships for the ship dropdown. Limited to flyable/in-game ships via the
    ships_index join (same filter the main /api/ships uses). Cargo capacity is
    the RSI marketing value (rsi_cargo_scu), falling back to the in-game grid
    (cargo_scu) when RSI has none; cargo-capable ships sort first."""
    conn = get_db(); p = PATCH or latest_patch(conn)
    rows = conn.execute("""
        SELECT s.uuid, s.entity_name, s.vehicle_name, s.display_name,
               COALESCE(NULLIF(s.rsi_cargo_scu, 0), s.cargo_scu) AS cargo_scu,
               s.size_class, s.role, s.career
          FROM ships s
          JOIN ships_index si ON si.entity_name = s.entity_name
         WHERE s.patch_version = ?
         ORDER BY (COALESCE(NULLIF(s.rsi_cargo_scu, 0), s.cargo_scu) IS NULL
                   OR COALESCE(NULLIF(s.rsi_cargo_scu, 0), s.cargo_scu) = 0) ASC,
                  s.display_name, s.vehicle_name
    """, (p,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "uuid":         r["uuid"],
            "entity_name":  r["entity_name"],
            "name":         best_name(r["display_name"], r["vehicle_name"] or r["entity_name"]),
            "cargo_scu":    r["cargo_scu"] or 0,
            "size_class":   r["size_class"],
            "role":         clean_role(r["role"]),
        })
    return jsonify(out)


@app.route("/api/cargo/qdrives")
def api_cargo_qdrives():
    """Quantum drives for the QD dropdown.

    With ?ship=<entity_name>: returns only drives that match the ship's
    quantum-drive hardpoint size, marks the stock drive (is_stock), and sets
    default_uuid to it. Names come from item_components.display_name (no
    manufacturer prefix). Without a ship: returns all drives.
    """
    conn = get_db(); p = PATCH or latest_patch(conn)
    ship = request.args.get("ship")

    hp_size = None
    stock_uuid = None
    if ship:
        hp = conn.execute("""
            SELECT max_size, installed_name
              FROM ship_hardpoints
             WHERE patch_version = ? AND ship_entity_name = ?
               AND port_type = 'quantumdrive'
             ORDER BY max_size DESC
             LIMIT 1
        """, (p, ship)).fetchone()
        if hp:
            hp_size = hp["max_size"]
            if hp["installed_name"]:
                srow = conn.execute("""
                    SELECT uuid FROM item_quantum_drives
                     WHERE patch_version = ? AND LOWER(entity_name) = LOWER(?)
                """, (p, hp["installed_name"])).fetchone()
                stock_uuid = srow["uuid"] if srow else None

    sql = """
        SELECT qd.uuid, qd.entity_name, ic.display_name AS comp_name, ic.size,
               qd.drive_speed, qd.quantum_fuel_req, qd.jump_range,
               qd.spool_up_time, qd.cooldown_time, qd.engage_speed
          FROM item_quantum_drives qd
          LEFT JOIN item_components ic
            ON LOWER(ic.entity_name) = LOWER(qd.entity_name)
           AND ic.patch_version = qd.patch_version
         WHERE qd.patch_version = ?
           AND qd.entity_name NOT LIKE '%\\_template' ESCAPE '\\'
    """
    params = [p]
    if hp_size is not None:
        sql += " AND ic.size = ?"
        params.append(hp_size)
    sql += " ORDER BY ic.display_name, qd.entity_name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    drives = [{
        "uuid":             r["uuid"],
        "entity_name":      r["entity_name"],
        "name":             _qd_name(r["comp_name"], r["entity_name"]),
        "size":             r["size"],
        "is_stock":         (r["uuid"] == stock_uuid),
        "drive_speed":      r["drive_speed"],
        "quantum_fuel_req": r["quantum_fuel_req"],
        "jump_range":       r["jump_range"],
        "spool_up_time":    r["spool_up_time"],
        "cooldown_time":    r["cooldown_time"],
        "engage_speed":     r["engage_speed"],
    } for r in rows]
    # Stock drive first, then alphabetical
    drives.sort(key=lambda d: (not d["is_stock"], d["name"] or ""))
    return jsonify({"drives": drives, "default_uuid": stock_uuid, "hardpoint_size": hp_size})


# Quantum-travel route engine (Layers 1-3, see app/quantum_travel.py,
# nav_graph.py, route_planner.py). NavGraph is cached per patch — it copies the
# nav_points rows in at build time and holds no DB handle afterwards.
_NAV_GRAPH_CACHE = {}


def _get_nav_graph(conn, patch):
    g = _NAV_GRAPH_CACHE.get(patch)
    if g is None:
        from helpers.quantum_travel import NavGraph
        g = NavGraph(conn, patch)
        _NAV_GRAPH_CACHE[patch] = g
    return g


# Systems the starmap models, keyed by the lowercase slug the frontend uses.
_STARMAP_SYSTEMS = {"stanton": "Stanton", "pyro": "Pyro", "nyx": "Nyx"}
_STARMAP_BODY_KINDS = ("star", "planet", "moon", "jumppoint")
# Selectable non-body locations / triangulation references: docking stations,
# Lagrange points, and orbital markers (OMs). Outposts/landing-zones omitted —
# players set position relative to stations / L-points / OMs / planets / moons.
_STARMAP_POI_KINDS  = ("station", "lagrange", "om")
_BODY_GROUP_KINDS   = ("star", "planet", "moon")


@app.route("/api/starmap/<system>/bodies")
def api_starmap_bodies(system):
    """Per-system bodies + POIs with real heliocentric coordinates (km, star at
    origin) for the starmap. Bodies are the star/planets/moons/jump points;
    POIs are stations/landing zones/outposts/Lagrange points. Used to render the
    map in true coordinates and to drive the 'set your position' triangulation.
    """
    canon = _STARMAP_SYSTEMS.get((system or "").lower())
    if not canon:
        return jsonify({"error": f"unknown system '{system}'"}), 404

    conn = get_db()
    p = PATCH or latest_patch(conn)
    nav = _get_nav_graph(conn, p)

    # Index this system's nodes by location_key so Lagrange points can be
    # grouped under their named planet (key "Stanton3_L1" -> "Stanton3").
    by_key = {n["key"]: n for n in nav.system_nodes(canon) if n["key"]}

    def safe_node(uuid):
        try:
            return nav.node(uuid) if uuid else None
        except Exception:
            return None

    def parent_info(n):
        # Bodies group under their orbital parent (moon -> planet); L-points
        # under the planet named in their key prefix; everything else under the
        # body it belongs to (station -> planet, OM -> its moon/planet).
        if n["kind"] in _BODY_GROUP_KINDS:
            target = safe_node(n.get("parent_uuid"))
        elif n["kind"] == "lagrange" and "_L" in (n["key"] or ""):
            target = by_key.get(n["key"].split("_L")[0])
        else:
            target = safe_node(n.get("body_uuid") or n.get("parent_uuid"))
        return (target["key"], target["name"]) if target else (None, None)

    def ser(n, want_radius=False):
        pkey, pname = parent_info(n)
        d = {
            "uuid": n["uuid"],
            "key":  n["key"],
            "name": n["name"],
            "kind": n["kind"],
            "helio": list(n["helio"]) if n["helio"] else None,
            "parent_key":  pkey,
            "parent_name": pname,
        }
        if want_radius:
            d["radius_km"] = n["body_radius_km"]
            d["lat_deg"]   = n["lat_deg"]
            d["lon_deg"]   = n["lon_deg"]
        return d

    def real_name(n):
        nm = (n["name"] or "").strip()
        return nm and "UNINITIALIZED" not in nm and not nm.startswith("<=")

    bodies = [ser(n, want_radius=True)
              for n in nav.system_nodes(canon, _STARMAP_BODY_KINDS)
              if real_name(n)]
    # POIs must have a position to be a reference / marker; drop the unplaced
    # and the placeholder/uninitialised rows.
    pois = [ser(n) for n in nav.system_nodes(canon, _STARMAP_POI_KINDS)
            if n["helio"] and real_name(n)]

    return jsonify({"system": canon, "patch": p, "bodies": bodies, "pois": pois})


@app.route("/api/cargo/route", methods=["POST"])
def api_cargo_route():
    """Compute QT travel time + fuel for a sequence of origin->dest legs.

    Body: {ship_uuid?, qd_uuid (or quantum_drive_uuid), legs:[{from,to}, ...]}
    where from/to are nav_points.location_uuid. Returns the Route dict from
    plan_route (flat legs + time/distance/fuel totals + warnings).
    """
    from helpers.quantum_travel import plan_route

    body = request.get_json(silent=True) or {}
    qd_uuid = body.get("qd_uuid") or body.get("quantum_drive_uuid")
    ship_uuid = body.get("ship_uuid")
    raw_legs = body.get("legs") or []
    if not qd_uuid:
        return jsonify({"error": "qd_uuid is required"}), 400

    segments = [(l.get("from"), l.get("to")) for l in raw_legs
                if l.get("from") and l.get("to")]
    if not segments:
        return jsonify({"error": "at least one leg with from/to is required"}), 400

    conn = get_db()
    try:
        p = PATCH or latest_patch(conn)
        qd_row = conn.execute(
            "SELECT * FROM item_quantum_drives WHERE patch_version=? AND uuid=?",
            (p, qd_uuid)).fetchone()
        if not qd_row:
            return jsonify({"error": "quantum drive not found"}), 404
        qd = dict(qd_row)

        ship = {}
        if ship_uuid:
            srow = conn.execute(
                "SELECT * FROM ships WHERE patch_version=? AND uuid=?",
                (p, ship_uuid)).fetchone()
            if srow:
                ship = dict(srow)

        nav = _get_nav_graph(conn, p)
        try:
            route = plan_route(segments, ship, qd, nav)
        except Exception as e:  # unknown nav point, bad geometry, etc.
            return jsonify({"error": str(e)}), 400
        return jsonify(route)
    finally:
        conn.close()


@app.route("/api/cargo/optimize", methods=["POST"])
def api_cargo_optimize():
    """Optimise the visiting order of a set of cargo moves to minimise travel.

    Body: {qd_uuid (or quantum_drive_uuid), ship_uuid?, origin_uuid?,
           objective? ("time"|"distance"|"fuel", default "time"),
           moves: [{id?, pickup, dropoff, scu}, ...]}
    pickup/dropoff are nav_points.location_uuid; the caller resolves any
    "same as current/prev" planner flags into concrete UUIDs before sending
    (also accepts pickup_uuid/dropoff_uuid/quantity_scu and a `legs` alias).
    ship_uuid supplies the cargo_scu capacity (omit -> unlimited). Returns the
    optimize_stack result: feasibility, ordered stops, and the full route.
    """
    from helpers.route_optimizer import optimize_stack, OptimizeError

    body = request.get_json(silent=True) or {}
    qd_uuid = body.get("qd_uuid") or body.get("quantum_drive_uuid")
    ship_uuid = body.get("ship_uuid")
    origin_uuid = body.get("origin_uuid")
    objective = body.get("objective") or "time"
    raw_moves = body.get("moves") or body.get("legs") or []
    if not qd_uuid:
        return jsonify({"error": "qd_uuid is required"}), 400

    moves = []
    for m in raw_moves:
        pickup = m.get("pickup") or m.get("pickup_uuid")
        dropoff = m.get("dropoff") or m.get("dropoff_uuid")
        if not (pickup and dropoff):
            continue
        mv = {"pickup": pickup, "dropoff": dropoff,
              "scu": m.get("scu", m.get("quantity_scu")) or 0}
        if m.get("id") is not None:
            mv["id"] = m["id"]
        moves.append(mv)
    if not moves:
        return jsonify({"error": "at least one move with pickup/dropoff is required"}), 400

    conn = get_db()
    try:
        p = PATCH or latest_patch(conn)
        qd_row = conn.execute(
            "SELECT * FROM item_quantum_drives WHERE patch_version=? AND uuid=?",
            (p, qd_uuid)).fetchone()
        if not qd_row:
            return jsonify({"error": "quantum drive not found"}), 404
        qd = dict(qd_row)

        ship = {}
        if ship_uuid:
            srow = conn.execute(
                "SELECT * FROM ships WHERE patch_version=? AND uuid=?",
                (p, ship_uuid)).fetchone()
            if srow:
                ship = dict(srow)
                # Capacity = RSI marketing value, falling back to the in-game
                # grid; this is the cargo_scu the optimizer reads for capacity.
                ship["cargo_scu"] = ship.get("rsi_cargo_scu") or ship.get("cargo_scu")

        nav = _get_nav_graph(conn, p)
        try:
            result = optimize_stack(moves, ship, qd, nav,
                                    objective=objective, origin_uuid=origin_uuid)
        except OptimizeError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:  # unknown nav point, etc.
            return jsonify({"error": str(e)}), 400
        return jsonify(result)
    finally:
        conn.close()


# Ordering of dropdown-3 groups within a planetary system.
_GROUP_ORDER = {"planet": 0, "moon": 1, "lagrange": 2, "system": 3}


def _build_navpt_hierarchy(conn, patch):
    """Load all nav_points and return (by_uuid, resolver). resolver(uuid)
    returns a dict with system, planet/moon names, and the planetary-system +
    group fields the cascading dropdowns need. Cached per call.

    Hierarchy note: moons parent to their planet (clean), but Lagrange points
    parent to the star — their planet is derived from the location_key prefix
    (e.g. 'Stanton1_L1' → planet 'Stanton1')."""
    by_uuid = {}
    for r in conn.execute("""
        SELECT location_uuid, display_name, location_key, kind, parent_uuid
          FROM nav_points WHERE patch_version = ?
    """, (patch,)):
        by_uuid[r["location_uuid"]] = dict(r)

    planet_by_key = {n["location_key"]: n for n in by_uuid.values()
                     if n["kind"] == "planet"}

    cache = {}

    def resolve(uuid):
        if uuid in cache:
            return cache[uuid]
        system_code = planet = moon = lagrange = None
        cur = uuid
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            node = by_uuid.get(cur)
            if not node:
                break
            kind = node["kind"]
            if kind == "star":
                system_code = _STAR_KEY_TO_SYSTEM.get(node["location_key"])
            elif kind == "planet" and planet is None:
                planet = node
            elif kind == "moon" and moon is None:
                moon = node
            elif kind == "lagrange" and lagrange is None:
                lagrange = node
            cur = node["parent_uuid"]

        # Planetary system = the planet (directly, or derived for Lagrange points)
        ps = planet
        if ps is None and lagrange is not None:
            base = re.sub(r"_[Ll]\d+$", "", lagrange["location_key"])
            ps = planet_by_key.get(base)

        # Dropdown-3 group: moon name | "Lagrange Points" | planet name | System-wide
        if moon is not None:
            group_label, group_kind = (moon["display_name"] or moon["location_key"]), "moon"
        elif lagrange is not None:
            group_label, group_kind = "Lagrange Points", "lagrange"
        elif planet is not None:
            group_label, group_kind = (planet["display_name"] or planet["location_key"]), "planet"
        else:
            group_label, group_kind = "System-wide", "system"

        result = {
            "system":      system_code,
            "planet":      (planet["display_name"] or planet["location_key"]) if planet else None,
            "moon":        (moon["display_name"] or moon["location_key"]) if moon else None,
            "ps_name":     (ps["display_name"] or ps["location_key"]) if ps else "System-wide",
            "ps_key":      ps["location_key"] if ps else "_system",
            "group_label": group_label,
            "group_kind":  group_kind,
        }
        cache[uuid] = result
        return result

    return by_uuid, resolve


@app.route("/api/cargo/locations")
def api_cargo_locations():
    """Locations for the planner dropdowns.

    Query params:
      system=PYRO     — filter to one system (for the 'current location' list)
      cargo_only=1    — only cargo-capable nav_points (for pickup/dropoff)

    Each location carries system + parent planet/moon so the UI can group:
        System → Planet → Moon → Location.
    """
    conn = get_db(); p = PATCH or latest_patch(conn)
    system_filter = (request.args.get("system") or "").upper() or None
    cargo_only = request.args.get("cargo_only") in ("1", "true", "yes")

    by_uuid, resolve = _build_navpt_hierarchy(conn, p)

    cargo_set = set()
    if cargo_only:
        cargo_set = {
            r["location_uuid"] for r in conn.execute(
                "SELECT DISTINCT location_uuid FROM nav_point_amenities "
                "WHERE patch_version = ? AND amenity = 'cargo_lift'", (p,)
            )
        }
    conn.close()

    out = []
    for uuid, node in by_uuid.items():
        if cargo_only and uuid not in cargo_set:
            continue
        h = resolve(uuid)
        # Drop leftover test assets (e.g. Ellis 'Green') and anything whose
        # planetary system resolves to one.
        if _is_excluded_nav_key(node["location_key"]) or _is_excluded_nav_key(h["ps_key"]):
            continue
        if system_filter and h["system"] != system_filter:
            continue
        out.append({
            "location_uuid": uuid,
            "name":          node["display_name"] or node["location_key"],
            "kind":          node["kind"],
            "system":        h["system"],
            "planet":        h["planet"],         # for pickup/dropoff grouping
            "moon":          h["moon"],
            "ps_name":       h["ps_name"],         # cascade: planetary system
            "ps_key":        h["ps_key"],
            "group_label":   h["group_label"],     # cascade: dropdown-3 optgroup
            "group_kind":    h["group_kind"],
        })

    # Sort so the UI gets ready-made cascade ordering:
    #   system → planetary system → group (planet/moon/lagrange/system) → name
    out.sort(key=lambda x: (
        x["system"] or "zzz",
        x["ps_name"] or "zzz",
        _GROUP_ORDER.get(x["group_kind"], 9),
        x["group_label"] or "",
        x["name"] or "",
    ))
    return jsonify(out)


# ══════════════════════════════════════════════════════════════════════
# CARGO PLANNER — plan persistence + activity (cargo_planner.db)
# ══════════════════════════════════════════════════════════════════════
# One auto-saved "active draft" per user, keyed on discord_id (the app's
# canonical identity). Anonymous users get 401 here and the page falls back
# to localStorage. Saves use a replace strategy: the draft stack's missions
# (and their legs, via cascade) are wiped and re-inserted on every save.

@app.route('/api/cargo/plan', methods=['GET'])
def api_cargo_plan_load():
    discord_id = session.get('discord_id')
    if not discord_id:
        return jsonify({'error': 'Not authenticated'}), 401
    conn = get_cargo_db()
    try:
        stack = conn.execute(
            "SELECT * FROM mission_stacks "
            "WHERE discord_id=? AND is_active_draft=1 AND is_archived=0",
            (discord_id,)).fetchone()
        if not stack:
            return jsonify({'plan': None})
        missions = []
        for m in conn.execute(
            "SELECT * FROM missions WHERE stack_id=? ORDER BY seq", (stack['stack_id'],)):
            legs = []
            for l in conn.execute(
                "SELECT * FROM legs WHERE mission_id=? ORDER BY seq", (m['mission_id'],)):
                legs.append({
                    'pickup_uuid':            l['pickup_location_uuid'],
                    'dropoff_uuid':           l['dropoff_location_uuid'],
                    'commodity':              l['commodity'],
                    'quantity_scu':           l['quantity_scu'],
                    'pickup_same_as_current': bool(l['pickup_same_as_current']),
                    'pickup_same_as_prev':    bool(l['pickup_same_as_prev']),
                    'dropoff_same_as_prev':   bool(l['dropoff_same_as_prev']),
                })
            missions.append({'notes': m['notes'], 'legs': legs})
        plan = {
            'ship_uuid':             stack['ship_uuid'],
            'quantum_drive_uuid':    stack['quantum_drive_uuid'],
            'current_system':        stack['current_system'],
            'current_location_uuid': stack['current_location_uuid'],
            'missions':              missions,
        }
        _cargo_log(conn, discord_id, 'plan_loaded', {'stack_id': stack['stack_id']})
        conn.commit()
        return jsonify({'plan': plan})
    finally:
        conn.close()


@app.route('/api/cargo/plan', methods=['PUT'])
def api_cargo_plan_save():
    discord_id = session.get('discord_id')
    if not discord_id:
        return jsonify({'error': 'Not authenticated'}), 401
    plan = request.get_json(silent=True) or {}
    conn = get_cargo_db()
    try:
        _cargo_touch_user(conn, discord_id)
        now = _utc_now()
        row = conn.execute(
            "SELECT stack_id FROM mission_stacks "
            "WHERE discord_id=? AND is_active_draft=1 AND is_archived=0",
            (discord_id,)).fetchone()
        if row:
            stack_id = row['stack_id']
            conn.execute("""
                UPDATE mission_stacks
                   SET ship_uuid=?, quantum_drive_uuid=?, current_system=?,
                       current_location_uuid=?, updated_utc=?
                 WHERE stack_id=?
            """, (plan.get('ship_uuid'), plan.get('quantum_drive_uuid'),
                  plan.get('current_system'), plan.get('current_location_uuid'),
                  now, stack_id))
            conn.execute("DELETE FROM missions WHERE stack_id=?", (stack_id,))  # cascades to legs
        else:
            cur = conn.execute("""
                INSERT INTO mission_stacks
                    (discord_id, name, ship_uuid, quantum_drive_uuid,
                     current_system, current_location_uuid, is_active_draft,
                     created_utc, updated_utc)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (discord_id, plan.get('name'), plan.get('ship_uuid'),
                  plan.get('quantum_drive_uuid'), plan.get('current_system'),
                  plan.get('current_location_uuid'), now, now))
            stack_id = cur.lastrowid

        leg_count = 0
        for mi, m in enumerate(plan.get('missions') or []):
            mcur = conn.execute(
                "INSERT INTO missions (stack_id, seq, notes) VALUES (?, ?, ?)",
                (stack_id, mi, m.get('notes')))
            mid = mcur.lastrowid
            for li, leg in enumerate(m.get('legs') or []):
                conn.execute("""
                    INSERT INTO legs
                        (mission_id, seq, pickup_location_uuid, dropoff_location_uuid,
                         commodity, quantity_scu,
                         pickup_same_as_current, pickup_same_as_prev, dropoff_same_as_prev)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (mid, li, leg.get('pickup_uuid'), leg.get('dropoff_uuid'),
                      leg.get('commodity'), leg.get('quantity_scu'),
                      1 if leg.get('pickup_same_as_current') else 0,
                      1 if leg.get('pickup_same_as_prev') else 0,
                      1 if leg.get('dropoff_same_as_prev') else 0))
                leg_count += 1

        _cargo_log(conn, discord_id, 'plan_saved',
                   {'stack_id': stack_id,
                    'missions': len(plan.get('missions') or []),
                    'legs': leg_count})
        conn.commit()
        return jsonify({'ok': True, 'stack_id': stack_id})
    finally:
        conn.close()


@app.route('/api/cargo/activity', methods=['POST'])
def api_cargo_activity():
    """Lightweight event logger (page visits, etc.). Auth optional — anon
    visits are recorded with a null discord_id for aggregate usage stats."""
    discord_id = session.get('discord_id')
    body = request.get_json(silent=True) or {}
    event_type = body.get('event_type', 'page_visit')
    conn = get_cargo_db()
    try:
        if discord_id:
            _cargo_touch_user(conn, discord_id)
        _cargo_log(conn, discord_id, event_type, body.get('details'))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("DATAFORGE_DB", "../../shared/data/dataforge.db"))
    parser.add_argument("--port",  default=5000, type=int)
    parser.add_argument("--patch", default=None)
    parser.add_argument("--debug", action="store_true", help="Enable debug mode with auto-reload")
    args = parser.parse_args()
    DB_PATH = args.db; PATCH = args.patch
    app.config['DB_PATH'] = DB_PATH  # keep blueprint in sync with --db override
    if not Path(DB_PATH).exists(): print(f"ERROR: DB not found: {DB_PATH}"); exit(1)
    ensure_columns(DB_PATH)
    ensure_indexes(DB_PATH)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    p = PATCH or latest_patch(conn)
    n = conn.execute("SELECT COUNT(*) as n FROM ships WHERE patch_version=?", (p,)).fetchone()["n"]
    conn.close()
    print(f"Sol Provision Ship Database  |  Patch: {p}  |  {n} ships  |  http://localhost:{args.port}")
    app.run(debug=args.debug, port=args.port, host="0.0.0.0")
