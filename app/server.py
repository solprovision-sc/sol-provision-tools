# ══════════════════════════════════════════════════════════════════════
# IMPORTS
# ══════════════════════════════════════════════════════════════════════
import os, json, argparse, sqlite3, re, uuid
from pathlib import Path
from flask import Flask, jsonify, request, render_template, session
import requests
import time
from datetime import timedelta, datetime, timezone
from functools import wraps


# ══════════════════════════════════════════════════════════════════════
# APP SETUP
# ══════════════════════════════════════════════════════════════════════
# Install: pip install firebase-admin --break-system-packages
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth

# Create flask app
app = Flask(__name__, template_folder="templates", static_folder="static")
DB_PATH = os.environ.get("DATAFORGE_DB", "../../shared/data/dataforge.db")
PATCH = None

# Detect environment. Three cases:
#   - Linux + /var/www/sol-provision-tools-dev exists → 'dev'
#   - Linux otherwise                                  → 'prod'
#   - Windows (local dev)                              → 'local' (uses dev Firebase project)
is_local = os.name == 'nt'
is_dev   = (not is_local) and os.path.exists('/var/www/sol-provision-tools-dev')

# ✅ SESSION CONFIG - GOES HERE (before any routes)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-prod')
# SESSION_COOKIE_SECURE requires HTTPS — locally we serve http://localhost,
# so disable secure-only there or the session cookie is never set and the
# login flow silently fails with a 401 on every authed request.
app.config['SESSION_COOKIE_SECURE'] = not is_local
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

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

def latest_patch(conn):
    row = conn.execute("SELECT patch_version FROM patch_history ORDER BY imported_at DESC LIMIT 1").fetchone()
    return row["patch_version"] if row else "4.6"


# ── Cargo planner user DB (saved plans + activity) ────────────────────────────
_cargo_schema_ready = False

def _resolve_cargo_db_path():
    """cargo_planner.db lives alongside dataforge.db unless overridden."""
    return os.environ.get('CARGO_PLANNER_DB') or \
        str(Path(DB_PATH).resolve().parent / 'cargo_planner.db')

def _ensure_cargo_schema(conn):
    """Apply the canonical schema from tools/init_cargo_planner_db.py so the
    server is self-sufficient (no separate init step needed in deploys)."""
    import importlib.util
    init_path = Path(__file__).resolve().parent.parent / "tools" / "init_cargo_planner_db.py"
    spec = importlib.util.spec_from_file_location("init_cargo_planner_db", init_path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    conn.executescript(mod.SCHEMA)
    conn.commit()

def get_cargo_db():
    global _cargo_schema_ready
    conn = sqlite3.connect(_resolve_cargo_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not _cargo_schema_ready:
        _ensure_cargo_schema(conn)
        _cargo_schema_ready = True
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
def index(): return render_template("index.html", active_page="/")

@app.route("/ships")
def ships_page():
    return render_template("ships.html", active_page="/ships")

@app.route("/ships/<entity_name>")
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
def crafting():
    return render_template("crafting.html", active_page="/crafting")

@app.route("/mission-rep")
def mission_rep():
    return render_template("mission_rep.html", active_page="/mission-rep")

@app.route("/mining-signatures")
def mining_signatures_page():
    return render_template("mining_signatures.html", active_page="/mining-signatures")

@app.route("/cargo-planner")
def cargo_planner_page():
    return render_template("cargo_planner.html", active_page="/cargo-planner")

@app.route("/ledger")
def ledger(): return render_template("ledger.html", active_page="ledger")

@app.route("/item_collection")
def item_collection_page(): return render_template("item_collection.html", active_page="/item_collection")

@app.route("/base-builder")
def base_builder_page(): return render_template("base_builder.html", active_page="/base-builder")

@app.route("/starmap")
@app.route("/starmap/<system>")
@app.route("/starmap/<system>/<body>")
def starmap_page(system=None, body=None):
    # JS reads the path off window.location and applies system + body focus.
    return render_template("starmap.html", active_page="/starmap")


# ══════════════════════════════════════════════════════════════════════
# AUTH API ROUTES
# ══════════════════════════════════════════════════════════════════════

# Mock auth for local development
@app.before_request
def mock_auth():
    if 'user' not in session and request.host.startswith('localhost'):
        session['user'] = {
            'discord_id': '123456789',
            'username': 'TestUser',
            'discriminator': '0001'
        }
            
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

    sql = f"""SELECT s.uuid, s.entity_name, s.display_name, s.vehicle_name,
                 s.career, COALESCE(vr.display_name, s.role) AS role, s.crew_size, s.cargo_scu,
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
            "crew_size":    r["crew_size"],
            "cargo_scu":    r["cargo_scu"],
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
        return [dict(r) for r in conn.execute(sql, {"ship": ship_entity, "patch": patch}).fetchall()]

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
               t.max_shield_health, t.max_shield_regen,
               t.damaged_regen_delay, t.downed_regen_delay,
               t.decay_ratio, t.reserve_drain_ratio,
               t.absorb_physical_min, t.absorb_physical_max,
               t.absorb_energy_min,   t.absorb_energy_max,
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
               t.power_draw, t.cooling_output,
               t.em_signature, t.ir_signature, t.health,
               t.power_low, t.power_medium, t.power_high
        {join("item_coolers")}""")

    powerplants = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.power_output, t.em_signature, t.health,
               t.power_low, t.power_medium, t.power_high
        {join("item_powerplants")}""")

    quantum_drives = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
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
               t.heat_rate_online, t.power_active_cooldown,
               t.overheat_temperature, t.cooling_per_second,
               t.time_till_cooling_starts, t.overheat_fix_time,
               t.max_ammo_load, t.max_regen_per_sec,
               t.regen_cooldown, t.regen_cost_per_bullet,
               t.power_draw, t.power_low, t.power_medium, t.power_high,
               t.ammo_uuid
        {join("item_weapons")}
        ORDER BY ic.size DESC, ic.entity_name""")

    weapons = []
    for w in weapons_base:
        weapon = dict(w)
        fire_modes = [dict(r) for r in conn.execute("""
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
        weapon["fire_modes"] = fire_modes

        # Attach the AmmoParams damage block (single row, by UUID lookup) so
        # the client can compute DPS = total damage × pellet_count × fire_rate.
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

        weapons.append(weapon)

    # Missile racks with their missile type looked up from item_missiles
    missile_racks = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.launch_delay, t.detach_velocity_forward,
               t.detach_velocity_right, t.detach_velocity_up,
               t.rack_tag
        {join("item_missile_racks")}""")

    # Missiles installed directly (GMISL_ entities on hardpoints)
    missiles = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.arm_time, t.max_lifetime,
               t.dmg_physical, t.dmg_energy, t.dmg_distortion,
               t.dmg_thermal, t.dmg_biochemical, t.dmg_stun,
               t.linear_speed, t.fuel_tank_size,
               t.lock_range_max, t.lock_range_min,
               t.lock_time, t.locking_angle, t.tracking_signal_type
        {join("item_missiles")}""")

    radars = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
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
               t.tool_kind, t.ignore_warmup_cooldown,
               t.em_signature, t.ir_signature, t.health,
               t.power_draw,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_tool_arms")}""")

    # Ground-vehicle wheels controllers (analog to flight_controllers for ships).
    wheels_controllers = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.minimum_power_amount,
               t.em_signature, t.ir_signature, t.health,
               t.power_draw,
               t.power_low, t.power_medium, t.power_high,
               t.power_low_start, t.power_medium_start, t.power_high_start
        {join("item_wheels_controllers")}""")

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
        "missile_racks":      missile_racks,
        "missiles":           missiles,
        "radars":             radars,
        "lifesupport":        lifesupport,
        "salvage":            salvage,
        "emp":                emp,
        "qed":                qed,
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
    conn.close()

    return jsonify({
        "uuid":                   ship["uuid"],
        "entity_name":            entity_name,
        "display_name":           best_name(ship["display_name"], entity_name),
        "manufacturer":           get_mfr(entity_name),
        "career":                 clean_career(ship["career"] or ""),
        "role": ship["role_display"] or clean_role(ship["role"] or ""),
        "crew_size":              ship["crew_size"],
        "cargo_scu":              ship["cargo_scu"],
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
    
    # Query the dedicated component table joined with entities for size
    query = f"""
        SELECT 
            c.*,
            e.size,
            e.grade
        FROM {table_name} c
        JOIN entities e ON c.entity_name = e.entity_name AND c.patch_version = e.patch_version
        WHERE c.patch_version = ?
          AND e.size = ?
        ORDER BY c.entity_name
    """
    
    rows = conn.execute(query, (patch, size)).fetchall()
    
    components = []
    for row in rows:
        # Convert row to dict
        comp = dict(row)
        
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
        
        display_name = loc_result['value'] if loc_result else comp.get('display_name')
        
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
            stats['quantum_fuel_requirement'] = comp.get('quantum_fuel_requirement', 0)
            stats['speed_mps'] = comp.get('speed', 0)
            stats['cooldown_time'] = comp.get('cooldown_time', 0)
            stats['spool_up_time'] = comp.get('spool_up_time', 0)
            stats['em_signature'] = comp.get('em_signature', 0)
            stats['power_draw'] = comp.get('power_draw', 0)
            
        elif comp_type == 'Radar':
            stats['detection_range'] = comp.get('detection_lifetime_max', 0)
            stats['em_signature'] = comp.get('em_signature', 0)
            stats['power_draw'] = comp.get('power_draw', 0)
        
        # Build simplified response
        component = {
            'uuid': comp.get('uuid'),
            'entity_name': comp.get('entity_name'),
            'display_name': display_name,  # ← Use the localized display_name
            'manufacturer': comp['manufacturer'],
            'size': comp.get('size'),
            'grade': comp.get('grade'),
            'grade_letter': comp['grade_letter'],
            'class': comp.get('class'),
            'item_type': comp_type,
            'stats': stats
        }
        
        components.append(component)
    
    return jsonify({
        'type': comp_type,
        'size': size,
        'patch_version': patch,
        'count': len(components),
        'components': components
    })


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
            ic.size AS item_size,
            ic.item_type AS item_type,
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
    query += " ORDER BY COALESCE(e.display_name, b.output_display, b.output_name, b.entity_name) ASC"
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

    for bp in results:
        f = facets.get(bp["uuid"])
        bp["mission_types"] = sorted(f["mission_types"]) if f else []
        bp["factions"]      = sorted(f["factions"])      if f else []
        bp["tiers"]         = sorted(f["tiers"])         if f else []
        bp["legality"]      = sorted(f["legality"])      if f else []

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
    conn = get_db()
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
            INSERT INTO blueprint_ownership
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
    conn = get_db()
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
    conn = get_db()
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
    conn = get_db()
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
    conn = get_db()
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
        FROM blueprint_ownership bo
        LEFT JOIN crafting_blueprints cb
            ON cb.uuid = bo.blueprint_uuid
            AND cb.patch_version = bo.patch_version
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
    ''', (discord_id, env)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


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
    ships_index join (same filter the main /api/ships uses). Includes
    cargo_scu so the UI can show capacity; cargo-capable ships sort first."""
    conn = get_db(); p = PATCH or latest_patch(conn)
    rows = conn.execute("""
        SELECT s.uuid, s.entity_name, s.vehicle_name, s.display_name, s.cargo_scu,
               s.size_class, s.role, s.career
          FROM ships s
          JOIN ships_index si ON si.entity_name = s.entity_name
         WHERE s.patch_version = ?
         ORDER BY (s.cargo_scu IS NULL OR s.cargo_scu = 0) ASC,
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
    if not Path(DB_PATH).exists(): print(f"ERROR: DB not found: {DB_PATH}"); exit(1)
    ensure_columns(DB_PATH)
    ensure_indexes(DB_PATH)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    p = PATCH or latest_patch(conn)
    n = conn.execute("SELECT COUNT(*) as n FROM ships WHERE patch_version=?", (p,)).fetchone()["n"]
    conn.close()
    print(f"Sol Provision Ship Database  |  Patch: {p}  |  {n} ships  |  http://localhost:{args.port}")
    app.run(debug=args.debug, port=args.port, host="0.0.0.0")
