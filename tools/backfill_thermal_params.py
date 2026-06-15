"""
Backfill per-component thermal parameters into item_components.

WHY THIS EXISTS
---------------
The power-management cooling display was built on item_components.heat_baseline /
coolant_consumption, which turned out to be 100% NULL: the coolant-resource model
is dormant in 4.8.x (every component declares `consumption resource="Coolant" = 0`).
The REAL heat model is the per-component <temperature> simulation:

    <temperature enable="1" internalTemperatureGeneration="0">
      <coolingEqualizationParams>
        <CoolingEqualizationRateAtTemperatureDifference
            coolingEqualizationRate="3.75" temperatureDifference="400"/>
      </coolingEqualizationParams>
      <signatureParams minimumTemperatureForIR="313" temperatureToIR="10"/>
      <itemResourceParams poweredAmbientCoolingMultiplier="1" minCoolingTemperature="308"
          enableOverheat="1" overheatTemperature="373"
          overheatWarningTemperature="363" overheatRecoveryTemperature="368">
        <stateHeatGenerationValues>
          <EntityTemperatureResourceHeatGeneration resource="Power"
              baselineTemperatureChange="0.9"/>
        </stateHeatGenerationValues>
      </itemResourceParams>
    </temperature>

This script reads those fields straight from the already-extracted scitem records
and UPDATEs item_components in place. It is idempotent (ADD COLUMN guarded, UPDATE
keyed by uuid). The same parse logic should be folded into the pipeline
(dataforge_scitems.parse_temperature_params) so future patches carry it forward.

USAGE
-----
    python tools/backfill_thermal_params.py [--db PATH] [--records PATH] [--dry-run]
"""
import argparse
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "app", "dataforge.db")
DEFAULT_RECORDS = (
    r"D:\StarCitizen\OtherStuff\DataMining\patches\4.8.1-live.11952564"
    r"\Data\Libs\Foundry\Records\entities\scitem\ships"
)

# (column, sql_type) — added to item_components if missing.
THERMAL_COLUMNS = [
    ("heat_gen_rate",                "REAL"),  # baselineTemperatureChange (deg/s, resource=Power)
    ("overheat_temperature",        "REAL"),
    ("overheat_warning_temp",       "REAL"),
    ("overheat_recovery_temp",      "REAL"),
    ("min_cooling_temperature",     "REAL"),   # thermal floor the item cools toward
    ("cooling_equalization_rate",   "REAL"),   # passive cooling rate at temperatureDifference
    ("cooling_equalization_tdiff",  "REAL"),   # the temperatureDifference that rate is quoted at
    ("powered_ambient_cool_mult",   "REAL"),
    ("overheat_enabled",            "INTEGER"),
    ("thermal_enabled",             "INTEGER"),
    ("temperature_to_ir",           "REAL"),
    ("min_temperature_for_ir",      "REAL"),
]


def _f(el, attr):
    if el is None:
        return None
    v = el.get(attr)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _i(el, attr):
    v = _f(el, attr)
    return None if v is None else int(v)


def parse_temperature_params(root):
    """Pull the <temperature> thermal block from a scitem record root.
    Returns a dict of THERMAL_COLUMNS keys (all None if no thermal block)."""
    out = {c: None for c, _ in THERMAL_COLUMNS}

    # The thermal block lives under SEntityPhysicsControllerParams. Take the first
    # <temperature> that actually carries an <itemResourceParams> (the real one).
    temp = None
    for cand in root.iter("temperature"):
        if cand.find("itemResourceParams") is not None or cand.get("enable") is not None:
            temp = cand
            break
    if temp is None:
        return out

    out["thermal_enabled"] = _i(temp, "enable")

    ceq = temp.find(".//CoolingEqualizationRateAtTemperatureDifference")
    out["cooling_equalization_rate"] = _f(ceq, "coolingEqualizationRate")
    out["cooling_equalization_tdiff"] = _f(ceq, "temperatureDifference")

    sig = temp.find("signatureParams")
    out["temperature_to_ir"] = _f(sig, "temperatureToIR")
    out["min_temperature_for_ir"] = _f(sig, "minimumTemperatureForIR")

    irp = temp.find("itemResourceParams")
    if irp is not None:
        out["powered_ambient_cool_mult"] = _f(irp, "poweredAmbientCoolingMultiplier")
        out["min_cooling_temperature"]   = _f(irp, "minCoolingTemperature")
        out["overheat_enabled"]          = _i(irp, "enableOverheat")
        out["overheat_temperature"]      = _f(irp, "overheatTemperature")
        out["overheat_warning_temp"]     = _f(irp, "overheatWarningTemperature")
        out["overheat_recovery_temp"]    = _f(irp, "overheatRecoveryTemperature")

        # Heat generation: prefer the resource="Power" entry.
        chosen = None
        for hg in irp.iter("EntityTemperatureResourceHeatGeneration"):
            if hg.get("resource") == "Power":
                chosen = hg
                break
            if chosen is None:
                chosen = hg
        out["heat_gen_rate"] = _f(chosen, "baselineTemperatureChange")

    return out


def record_identity(root):
    """(uuid, entity_name_lower) from a scitem record root."""
    uuid = root.get("__ref") or root.get("__id") or ""
    tag = root.tag
    entity = tag.split(".", 1)[1] if "." in tag else ""
    return uuid, entity.lower()


def ensure_columns(conn):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(item_components)")}
    added = []
    for col, typ in THERMAL_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE item_components ADD COLUMN {col} {typ}")
            added.append(col)
    if added:
        print(f"  added columns: {', '.join(added)}")
    else:
        print("  all thermal columns already present")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--records", default=DEFAULT_RECORDS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        sys.exit(f"DB not found: {db_path}")
    if not os.path.isdir(args.records):
        sys.exit(f"records dir not found: {args.records}")

    print(f"DB:      {db_path}")
    print(f"records: {args.records}")

    conn = sqlite3.connect(db_path)
    ensure_columns(conn)

    # Map known item_components rows so we only update real components.
    known_uuid = {r[0] for r in conn.execute("SELECT uuid FROM item_components")}
    known_name = {r[0]: r[1] for r in conn.execute(
        "SELECT LOWER(entity_name), uuid FROM item_components")}

    scanned = thermal = matched = with_heat = 0
    updates = []
    for dirpath, _dirs, files in os.walk(args.records):
        for fn in files:
            if not fn.endswith(".xml"):
                continue
            scanned += 1
            try:
                root = ET.parse(os.path.join(dirpath, fn)).getroot()
            except ET.ParseError:
                continue
            params = parse_temperature_params(root)
            if all(v is None for v in params.values()):
                continue
            thermal += 1
            uuid, name = record_identity(root)
            target = uuid if uuid in known_uuid else known_name.get(name)
            if not target:
                continue
            matched += 1
            if params.get("heat_gen_rate"):
                with_heat += 1
            updates.append((target, params))

    print(f"\nscanned={scanned}  thermal_blocks={thermal}  "
          f"matched_components={matched}  with_heat_gen={with_heat}")

    if args.dry_run:
        print("dry-run: no writes")
        # Show a few samples
        for target, params in updates[:8]:
            print(f"  {target[:8]}  heat={params['heat_gen_rate']}  "
                  f"overheat={params['overheat_temperature']}")
        return

    cols = [c for c, _ in THERMAL_COLUMNS]
    set_clause = ", ".join(f"{c}=?" for c in cols)
    sql = f"UPDATE item_components SET {set_clause} WHERE uuid=?"
    for target, params in updates:
        conn.execute(sql, [params[c] for c in cols] + [target])
    conn.commit()
    print(f"updated {len(updates)} item_components rows")

    # Post-check
    n = conn.execute("SELECT COUNT(*) FROM item_components WHERE heat_gen_rate>0").fetchone()[0]
    print(f"item_components with heat_gen_rate>0: {n}")


if __name__ == "__main__":
    main()
