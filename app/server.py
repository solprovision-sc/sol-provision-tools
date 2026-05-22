#!/usr/bin/env python3
import os, json, argparse, sqlite3, re
from pathlib import Path
from flask import Flask, jsonify, request, render_template

app = Flask(__name__, template_folder="templates", static_folder="static")
DB_PATH = os.environ.get("DATAFORGE_DB", "../../shared/data/dataforge.db")
PATCH = None

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def latest_patch(conn):
    row = conn.execute("SELECT patch_version FROM patch_history ORDER BY imported_at DESC LIMIT 1").fetchone()
    return row["patch_version"] if row else "4.6"

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

# ── Page routes ───────────────────────────────────────────────────────────────

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

#@app.route("/cargo-planner")
#def cargo_planner_page():
#    from flask import send_from_directory
#    return send_from_directory("templates", "cargo_planner.html")

@app.route("/ledger")
def ledger(): return render_template("ledger.html", active_page="ledger")

@app.route("/item_collection")
def item_collection_page(): return render_template("item_collection.html", active_page="/item_collection")

# ── Meta / counts ─────────────────────────────────────────────────────────────
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
    if sort_by not in {"entity_name","cargo_scu","crew_size","length_m","career","display_name"}:
        sort_by = "entity_name"
    sort_by = f"s.{sort_by}"

    sql = f"""SELECT s.uuid, s.entity_name, s.display_name, s.vehicle_name, 
                 s.career, COALESCE(vr.display_name, s.role) AS role, s.crew_size, s.cargo_scu, 
                 s.length_m, s.beam_m, s.height_m
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
               t.power_draw, t.em_signature, t.health
        {join("item_shields")}""")

    coolers = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, 
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.power_draw, t.cooling_output,
               t.em_signature, t.ir_signature, t.health
        {join("item_coolers")}""")

    powerplants = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, 
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.power_output, t.em_signature, t.health
        {join("item_powerplants")}""")

    quantum_drives = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, 
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.drive_speed / 1000 as drive_speed, t.stage_one_accel_mps2 as accel1,
               t.stage_two_accel_mps2 as accel2,t.spool_up_time, t.cooldown_time,
               t.calibration_rate, t.calibration_delay,
               t.fuel_per_gm_mscu, t.power_draw, 
               t.em_signature, t.health
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
               t.ab_accel_mult_fwd, t.spool_up_time, t.power_draw
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

    # Weapons: join fire modes as nested list
    weapons_base = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               ic.grade_letter, ic.class, ic.description, ic.item_sub_type,
               t.heat_rate_online, t.power_active_cooldown,
               t.overheat_temperature, t.cooling_per_second,
               t.time_till_cooling_starts, t.overheat_fix_time,
               t.max_ammo_load, t.max_regen_per_sec,
               t.regen_cooldown, t.regen_cost_per_bullet
        {join("item_weapons")}
        ORDER BY ic.size DESC, ic.entity_name""")

    weapons = []
    for w in weapons_base:
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
        """, (w["entity_name"], patch)).fetchall()]
        weapons.append({**w, "fire_modes": fire_modes})

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
               t.shutdown_time_sec, t.ir_sensitivity, t.em_sensitivity, t.cs_sensitivity, t.db_sensitivity, t.rs_sensitivity
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
    for hp in hps: hardpoints.setdefault(hp["port_type"], []).append(dict(hp))
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
            
            print(f"[DEBUG] Ingredient: display_name={ing.get('display_name')}, resource_name={ing.get('resource_name')}")
            
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

# ── Entry point ───────────────────────────────────────────────────────────────
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
