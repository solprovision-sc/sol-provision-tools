#!/usr/bin/env python3
import os, json, argparse, sqlite3
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
    "vncl":"Vanduul","xian":"Xi'an","csin":"Preacher","taln":"Talon",
}
def get_mfr(n): return MFR_MAP.get((n or "").split("_")[0].lower(), (n or "").split("_")[0].upper())
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


COMP_PARAMS = {
    "Shield":       "SCItemShieldGeneratorParams",
    "Cooler":       "SCItemCoolerParams",
    "QuantumDrive": "SCItemQuantumDriveParams",
    "PowerPlant":   "SCItemPowerPlantParams",
}

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
def crafting(): return render_template("crafting.html", active_page="/crafting")
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
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, ic.item_sub_type,
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
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, ic.item_sub_type,
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
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, ic.item_sub_type,
               t.power_draw, t.cooling_output,
               t.em_signature, t.ir_signature, t.health
        {join("item_coolers")}""")

    powerplants = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, ic.item_sub_type,
               t.power_output, t.em_signature, t.health
        {join("item_powerplants")}""")

    quantum_drives = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, ic.item_sub_type,
               t.drive_speed / 1000 as drive_speed, t.spool_up_time, t.cooldown_time,
               t.calibration_rate, t.calibration_delay,
               t.fuel_per_gm_mscu,t.power_draw, 
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
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade, ic.item_sub_type,
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
               t.launch_delay, t.detach_velocity_forward,
               t.detach_velocity_right, t.detach_velocity_up,
               t.rack_tag
        {join("item_missile_racks")}""")

    # Missiles installed directly (GMISL_ entities on hardpoints)
    missiles = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size,
               t.arm_time, t.max_lifetime,
               t.dmg_physical, t.dmg_energy, t.dmg_distortion,
               t.dmg_thermal, t.dmg_biochemical, t.dmg_stun,
               t.linear_speed, t.fuel_tank_size,
               t.lock_range_max, t.lock_range_min,
               t.lock_time, t.locking_angle, t.tracking_signal_type
        {join("item_missiles")}""")

    radars = q(f"""
        SELECT ic.entity_name, ic.display_name, ic.size, ic.grade,
               t.power_draw, t.em_signature, t.health
        {join("item_radars")}""")

    return {
        "armor":              armor,
        "shields":            shields,
        "coolers":            coolers,
        "powerplants":        powerplants,
        "quantum_drives":     quantum_drives,
        "fuel_tanks":         fuel_tanks,
        "quantum_fuel_tanks": quantum_fuel_tanks,
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

    grids = conn.execute(
        """SELECT cg.entity_name, cg.scu, cg.dim_x, cg.dim_y, cg.dim_z,
                  cg.is_external, cg.is_personal, ic.entity_name as container_name
           FROM cargo_grids cg
           LEFT JOIN inventory_containers ic ON ic.uuid=cg.container_uuid
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
        "mass_kg":                ship["mass_kg"],
        "size":                ship["size_class"],
        "cargo_grids":            [dict(g) for g in grids],
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

@app.route("/api/component/<entity_name>")
def api_component_detail(entity_name):
    conn = get_db(); p = PATCH or latest_patch(conn)
    row = conn.execute(
        "SELECT * FROM entities WHERE entity_name=? AND patch_version=?", (entity_name, p)
    ).fetchone()
    conn.close()
    if not row: return jsonify({"error":"Not found"}), 404

    d = json.loads(row["data"])
    param_key = COMP_PARAMS.get(row["item_type"], "")
    stats = d.get(param_key, {}) or {}
    power = d.get("power", {}) or {}

    return jsonify({
        "entity_name":  entity_name,
        "display_name": best_name(row["display_name"], entity_name),
        "description":  row["description"],
        "item_type":    row["item_type"],
        "item_subtype": row["item_subtype"],
        "grade":        row["grade"],
        "size":         row["size"],
        "stats":        {k:v for k,v in stats.items() if not k.startswith("__")},
        "power":        power,
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
    
# ── API: Categories ───────────────────────────────────────────────────────────
#
# Returns distinct categories with blueprint counts.
# Joins crafting_blueprints with itself to get latest patch data.
#
# GET /api/crafting/categories
# Response: [{uuid, name, count}]

@app.route("/api/crafting/categories")
def api_crafting_categories():
    db    = get_db()
    patch = latest_patch(db)
    if not patch:
        return jsonify([])

    rows = db.execute("""
        SELECT
            cb.category_uuid,
            cb.category_name,
            CASE
                WHEN cb.entity_name LIKE '%mag' AND cb.entity_name LIKE '%craft%'
                THEN 'Ammo'
                ELSE cb.category_name
            END AS display_category,
            COUNT(*) AS count
        FROM crafting_blueprints cb
        WHERE cb.patch_version = ?
          AND cb.category_uuid IS NOT NULL
        GROUP BY cb.category_uuid, display_category
        ORDER BY display_category ASC
    """, (patch,)).fetchall()

    result = []
    for r in rows:
        result.append({
            "uuid":        r["category_uuid"],
            "name":        r["display_category"],
            "filter_type": "ammo" if r["display_category"] == "Ammo" else "weapons",
            "count":       r["count"],
        })

    return jsonify(result)


# ── API: Blueprint List ───────────────────────────────────────────────────────
#
# Returns all blueprints for a given category UUID (latest patch).
# Joins entities table on output_uuid to get the authoritative display name.
#
# GET /api/crafting/blueprints?category_uuid=<uuid>
# Response: [{uuid, patch_version, entity_name, display_name, category_name,
#             output_uuid, output_name, output_display, craft_time_sec, slots_required}]

@app.route("/api/crafting/blueprints")
def api_crafting_blueprints():
    db           = get_db()
    patch        = latest_patch(db)
    category_uuid = request.args.get("category_uuid", "").strip()
    subcategory   = request.args.get("subcategory", "").strip()  # 'ammo' or ''

    query = """
        SELECT
            cb.uuid,
            cb.patch_version,
            cb.entity_name,
            cb.category_name,
            cb.output_uuid,
            cb.output_name,
            COALESCE(e.display_name, cb.output_display, cb.output_name, cb.entity_name) AS output_display,
            cb.craft_time_sec,
            cb.slots_required
        FROM crafting_blueprints cb
        LEFT JOIN entities e
            ON e.uuid = cb.output_uuid
           AND e.patch_version = cb.patch_version
        WHERE cb.patch_version = ?
    """
    params = [patch]

    if category_uuid:
        query += " AND cb.category_uuid = ?"
        params.append(category_uuid)

    # Split ammo out from weapons within the same category UUID
    if subcategory == "ammo":
        query += " AND cb.entity_name LIKE '%mag' AND cb.entity_name LIKE '%craft%'"
    elif category_uuid:
        query += " AND NOT (cb.entity_name LIKE '%mag' AND cb.entity_name LIKE '%craft%')"

    query += " ORDER BY COALESCE(e.display_name, cb.output_display, cb.output_name, cb.entity_name) ASC"

    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


# ── API: Blueprint Detail ─────────────────────────────────────────────────────
#
# Returns full blueprint detail including all slots and ingredients.
# All ingredient display names are resolved via entities join.
#
# GET /api/crafting/blueprint/<uuid>?patch=<patch_version>
# Response: {uuid, entity_name, output_display, category_name, craft_time_sec,
#            slots_required, slots: [{slot_debug_name, slot_display, ingredients: [...]}]}

@app.route("/api/crafting/blueprint/<uuid>")
def api_crafting_blueprint_detail(uuid):
    db    = get_db()
    patch = request.args.get("patch") or latest_patch(db)

    if not patch:
        return jsonify({"error": "No patch data available"}), 404

    # Blueprint header — join entities for authoritative output display name
    bp = db.execute("""
        SELECT
            cb.uuid,
            cb.patch_version,
            cb.entity_name,
            cb.category_name,
            cb.category_uuid,
            cb.output_uuid,
            cb.output_name,
            COALESCE(e.display_name, cb.output_display, cb.output_name, cb.entity_name) AS output_display,
            cb.craft_time_sec,
            cb.slots_required,
            cb.has_optional
        FROM crafting_blueprints cb
        LEFT JOIN entities e
            ON e.uuid = cb.output_uuid
           AND e.patch_version = cb.patch_version
        WHERE cb.uuid = ? AND cb.patch_version = ?
    """, (uuid, patch)).fetchone()

    if not bp:
        return jsonify({"error": "Blueprint not found"}), 404

    result = dict(bp)

    # Slots
    slots = db.execute("""
        SELECT id, slot_index, slot_debug_name, slot_display
        FROM crafting_slots
        WHERE blueprint_uuid = ? AND patch_version = ?
        ORDER BY slot_index ASC
    """, (uuid, patch)).fetchall()

    result["slots"] = []
    for slot in slots:
        # Ingredients — join entities for display name resolution
        ingredients = db.execute("""
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

        result["slots"].append({
            "slot_index":      slot["slot_index"],
            "slot_debug_name": slot["slot_debug_name"],
            "slot_display":    slot["slot_display"],
            "ingredients":     [dict(i) for i in ingredients],
        })

    return jsonify(result)
    
# GET /api/crafting/blueprint/<uuid>/missions?patch=
# Returns all mission pools that can drop this blueprint
@app.route("/api/crafting/blueprint/<uuid>/missions")
def api_crafting_blueprint_missions(uuid):
    db    = get_db()
    patch = request.args.get("patch") or latest_patch(db)
    if not patch:
        return jsonify([])

    rows = db.execute("""
        SELECT DISTINCT
            pool_uuid,
            mission_name,
            faction,
            COUNT(*) OVER (PARTITION BY pool_uuid) as pool_size
        FROM crafting_mission_pools
        WHERE blueprint_uuid = ? AND patch_version = ?
        ORDER BY faction NULLS LAST, mission_name
    """, (uuid, patch)).fetchall()

    return jsonify([dict(r) for r in rows])


# GET /api/crafting/mission/<mission_name>?patch=
# Returns all blueprints in a mission pool
@app.route("/api/crafting/mission/<mission_name>")
def api_crafting_mission_detail(mission_name):
    db    = get_db()
    patch = request.args.get("patch") or latest_patch(db)
    if not patch:
        return jsonify({})

    # Get pool metadata
    pool = db.execute("""
        SELECT pool_uuid, mission_name, faction
        FROM crafting_mission_pools
        WHERE mission_name = ? AND patch_version = ?
        LIMIT 1
    """, (mission_name, patch)).fetchone()

    if not pool:
        return jsonify({"error": "Mission not found"}), 404

    # Get all blueprints in this pool with resolved display names
    blueprints = db.execute("""
        SELECT
            cmp.blueprint_uuid,
            cb.entity_name,
            COALESCE(e.display_name, cb.output_display, cb.output_name) AS output_display,
            cb.output_name,
            cb.craft_time_sec,
            cb.slots_required,
            cb.category_name
        FROM crafting_mission_pools cmp
        LEFT JOIN crafting_blueprints cb
            ON cb.uuid = cmp.blueprint_uuid
            AND cb.patch_version = cmp.patch_version
        LEFT JOIN entities e
            ON e.uuid = cb.output_uuid
            AND e.patch_version = cb.patch_version
        WHERE cmp.mission_name = ? AND cmp.patch_version = ?
        ORDER BY COALESCE(e.display_name, cb.output_display, cb.output_name) ASC
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
    args = parser.parse_args()
    DB_PATH = args.db; PATCH = args.patch
    if not Path(DB_PATH).exists(): print(f"ERROR: DB not found: {DB_PATH}"); exit(1)
    ensure_columns(DB_PATH)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    p = PATCH or latest_patch(conn)
    n = conn.execute("SELECT COUNT(*) as n FROM ships WHERE patch_version=?", (p,)).fetchone()["n"]
    conn.close()
    print(f"Sol Provision Ship Database  |  Patch: {p}  |  {n} ships  |  http://localhost:{args.port}")
    app.run(debug=False, port=args.port, host="0.0.0.0")
