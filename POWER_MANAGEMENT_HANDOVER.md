# Ship Power Management Display — Handover

_Last updated: 2026-06-14_

This document captures the multi-session effort to build a SPViewer-style
**power management panel** on the ship detail page, plus all the supporting
data-extraction work. Read the "Current State / Where Things Live" section
first — the work is split across two repos and two git branches.

---

## 1. Goal

Replace the old "At a Glance" sidebar on `ship_detail.html` with:

1. A **power-distribution grid** mirroring [spviewer.eu](https://spviewer.eu/performance)
   — one column per powered component category, with per-segment toggles.
2. A **performance meta-stats panel** below it (DPS, shield HP, regen, power,
   cooling, signatures, mass, etc.) that recomputes live as components are
   swapped or power is toggled.

The 3D ship-dimensions box and the ship-identity header were kept; everything
between them was replaced.

---

## 2. Current State / Where Things Live

⚠️ **The work is preserved but split across branches. Confirm before continuing.**

| Layer | Repo | Branch | Status |
|---|---|---|---|
| App (server + UI) | `sol-provision-tools` | `dev` | ✅ committed & working |
| Extractor (pipeline) | `star-citizen-tools` | **`feature/ship-dimensions-rsi`** | ✅ committed — **NOT on `main`** |
| Database (`dataforge.db`) | both repos (copied) | n/a (not version-controlled) | ✅ generated with all new tables |

**Critical gotcha:** `star-citizen-tools` is currently checked out on `main`,
which **predates all of our extractor work**. The current `dataforge_scitems.py`
on `main` does NOT contain the parsers/routes for lifesupport, salvage, ammo,
EMP, QED, tool arms, wheels controllers, or the power columns. Those live on the
**`feature/ship-dimensions-rsi`** branch. If you re-run the extraction pipeline
from `main`, the new tables will not be regenerated. Check out / merge that
branch before extracting a new patch.

The `dataforge.db` currently deployed already has all the data (it was generated
from the feature branch before `main` was checked back out), so the live site
works today — but the extractor source must be reconciled before the next patch.

---

## 3. Data Layer — DB tables (all in `dataforge.db`)

### New tables
| Table | Rows (4.8.0) | Purpose |
|---|---:|---|
| `item_ammo` | 175 | Projectile damage per ammo record (Bullet/Tachyon/CounterMeasure). Linked to weapons by `item_weapons.ammo_uuid`. Enables real ship-weapon DPS. |
| `item_lifesupport` | 12 | Life support generators. power_draw + lifesupport_output. |
| `item_salvage` | 32 | Salvage heads, scraper/buff modifiers, filler stations. Also catches `wep_tractorbeam_*`/`wep_towingbeam_*` (typed SalvageHead, used as utility tractors). `salvage_type` discriminates. |
| `item_emp` | 7 | EMP devices (Mantis, Hawk, Sentinel, Scorpius…). chargeTime, empRadius, distortionDamage. |
| `item_qed` | 5 | Quantum interdiction generators. radius_meters, charge/discharge/cooldown. |
| `item_tool_arms` | 7 | Tractor/mining arm **mounts** (structural; no direct power). `tool_kind` = tractor\|mining. |
| `item_wheels_controllers` | 14 | Ground-vehicle wheels controllers (analog to flight controllers). |

### Power columns added to existing item tables
On `item_shields / coolers / powerplants / quantum_drives / radars / flight_controllers / weapons`:

- `power_draw` — segment count (`SPowerSegmentResourceUnit units`) or, for ship
  weapons/EMP, fractional standard units (`SStandardResourceUnit`).
- `power_low / power_medium / power_high` — output multiplier at each power level
  (e.g. a shield is 0.7 / 0.85 / 1.0).
- `power_low_start / power_medium_start / power_high_start` — segment positions
  where each level begins. **`high_start − medium_start` = the "mandatory" block
  size**; the rest are adjustable segments.

`item_weapons` also gained: `ammo_uuid` (FK to item_ammo), and the power columns.

### Thermal columns on `item_components` (added 2026-06-14)
The cooling display's real heat data. Populated from each scitem's `<temperature>`
block by `tools/backfill_thermal_params.py` (parse logic in `parse_temperature_params`):

- `heat_gen_rate` — `baselineTemperatureChange` (°C/s while powered, resource=Power).
- `overheat_temperature / overheat_warning_temp / overheat_recovery_temp`.
- `min_cooling_temperature` — the thermal floor the item cools toward.
- `cooling_equalization_rate / cooling_equalization_tdiff` — passive cooling (rate
  quoted at that temperature difference; `rate/tdiff` ≈ a universal 3.75/400).
- `powered_ambient_cool_mult`, `overheat_enabled`, `thermal_enabled`,
  `temperature_to_ir`, `min_temperature_for_ir`.

Coverage in 4.8.1: shields/powerplants/QDs/radars/lifesupport 100%; coolers carry
none (they don't self-heat); weapons use their own `item_weapons` heat fields.

✅ **Now in the extractor pipeline** (star-citizen-tools, branch
`feature/component-thermal-params`, off `main`): `parse_temperature_params` added to
`dataforge_scitems.py` (SCHEMA + parse_identity) and the columns to
`dataforge_db._MIGRATIONS`. Once merged to `main` and re-extracted, `dataforge.db`
carries the thermal data natively and `backfill_thermal_params.py` can be retired.
Until that merge+extract, fresh DBs still need the backfill (the app self-heals the
*columns* — empty — via `get_db`→`ensure_columns`, so it won't 500 meanwhile).

### Where dimensions come from (separate earlier task)
- `ships.length_m / beam_m / height_m` — fixed to use **sorted bbox** (largest=length).
- `ships.length_rsi_m / beam_rsi_m / height_rsi_m / rsi_name / rsi_url` — joined
  from the RSI Ship Matrix API by `rsi_ship_matrix.py`.

---

## 4. Extractor changes (`star-citizen-tools`, branch `feature/ship-dimensions-rsi`)

- **`dataforge_scitems.py`** — added parsers `parse_emp`, `parse_qed`,
  `parse_tractor_arm`, `parse_mining_arm`, `parse_wheels_controller`,
  `parse_lifesupport`, `parse_salvage`; new tables in `SCHEMA`; new `SUBDIR_ROUTES`;
  `get_active_state()` (Online→Idle fallback for EMPs); `apply_power_ranges()`
  helper (modifiers + start positions); **`parse_weapon` dispatches** SalvageHead→
  salvage and EMP→emp; **`parse_flight_controller` dispatches** WheeledController→
  wheels. Ammo parsing (`parse_ammo_record` / `parse_ammo_dir`) + weapon→ammo link.
- **`dataforge_db.py`** — migrations for the power columns, `ammo_uuid`, RSI dims.
- **`dataforge_foundry_loadouts.py`** (NEW module) — backfills `ship_hardpoints`
  from each ship's foundry-record `<SItemPortLoadoutEntryParams>` so life support /
  salvage / nested-turret items (which aren't in the vehicle Script XMLs) get a
  hardpoint row. Walks nested entries with parent-port dedup keys.
- **`rsi_ship_matrix.py`** (NEW module) — fetches RSI ship matrix, token-matches to
  our ships, writes marketing dims. `rsi_overrides.json` for edge cases.
- **`run_patch_extraction.py`** — wired in the new step (foundry loadouts) and the
  rsi step; renumbered to 14 steps. `config.ini` output_root → `…\patches`.

There is a throwaway diagnostic at `sol-provision-tools/tools/rsi_join_probe.py`.

---

## 5. App layer (`sol-provision-tools`, branch `dev`)

### `app/server.py` — `get_ship_components()`
`/api/ship/<entity_name>` now returns these component buckets (each a list of
installed items with their power columns):
`shields, coolers, powerplants, quantum_drives, flight_controllers, thrusters,
weapons, missiles, missile_racks, radars, lifesupport, salvage, emp, qed,
tool_arms, wheels_controllers`.

- Weapons carry a nested `ammo: {dmg_physical, dmg_energy, …}` object (looked up
  per-weapon by `ammo_uuid`) so the frontend can compute DPS.
- The hardpoints dict buckets NULL `port_type` rows (from the foundry backfill)
  under `"misc"` — otherwise Flask's `sort_keys=True` JSON encoder crashes on
  mixed str/None keys. **Keep that guard.**

### `app/templates/ship_detail.html`
This is the heart of the feature. See the next section.

---

## 6. ⭐ THE POWER MANAGEMENT DISPLAY — exactly what we have

### 6.1 The 13-slot model
SPViewer renders a fixed 13-slot grid; we mirror it. `POWER_CATEGORIES` (JS array
near the sidebar code) defines the slots in SPViewer order:

| # | key | label | compKey (loadout bucket) | filter |
|---|---|---|---|---|
| 1 | weapons | Weapons | weapons | `item_type==='WeaponGun'` |
| 2 | boost | Boost | flight_controllers | — |
| 3 | wheels | Wheels | wheels_controllers | — |
| 4 | shields | Shields | shields | — |
| 5 | quantum | Quantum | quantum_drives | — |
| 6 | emp | EMP | emp | — |
| 7 | qed | QED | qed | — |
| 8 | mining | Mining | weapons | `item_type==='WeaponMining'` |
| 9 | salvage | Salvage | salvage | not `wep_*` |
| 10 | utility | Utility | weapons **+** salvage (`multiCompKey`) | TractorBeam/TowingBeam OR `wep_*` |
| 11 | radar | Radar | radars | — |
| 12 | lifesupport | Life Supp. | lifesupport | — |
| 13 | cooler | Cooler | coolers | — |

`componentsForCategory(catKey)` resolves a slot's installed components from the
loadout (honoring `compKey`/`multiCompKey` + `filter`), keeping only items with
`power_draw > 0`. **Columns with zero matching components are hidden** — so a
fighter shows ~7 columns, the Reclaimer ~9, a ground vehicle uses the Wheels slot
instead of Boost, etc.

Icons are CSS masks from `/static/img/icons/Item*.svg`. Wheels/EMP/QED currently
reuse placeholder icons (Thrusters / Countermeasure / QuantumDrive) — **TODO: get
bespoke icons** (SPViewer uses `icon_common_vehicle_wheel_control`, `icon_common_EMP`,
`icon_common_QED`).

### 6.2 Segment model — mandatory block + adjustable segments
`powerBarBreakdown(comp)` splits each component's `power_draw` into:
- **mandatory** = `power_high_start − power_medium_start` → the always-on minimum,
  rendered as one solid block sized `mandatory × --pm-seg` px.
- **adjustable** = `power_draw − mandatory` → individually toggleable segments
  stacked above the mandatory block.

Example: Reclaimer S4 shield (`power_draw=6, medium_start=1, high_start=6`) → a
big block of 5 + 1 adjustable segment. Hornet shield (`draw=3, med=2, high=3`) →
block of 1 + 2 adjustable.

### 6.3 Cells — how columns are built
`cellsForCategory(cat, comps)` decides how many **columns** a category produces:
- **Cooler is the only category that splits**: with ≥2 coolers it produces one
  column per cooler (`cooler-0`, `cooler-1`), each independently operable with its
  own icon.
- Every other category = **one column** containing all its components. Multiple
  shields therefore stack **multiple mandatory blocks** (one per generator) in a
  single column, with shared adjustable segments above them.

### 6.4 State — `CELL_STATE`
Replaces the old `POWER_STATE`. One entry per rendered column:
```js
CELL_STATE[cellKey] = {
  catKey,                 // which category
  compEnabled: [bool,…],  // per-component on/off (one per mandatory block)
  adjLit:      [bool,…],  // per-adjustable-segment on/off (shared across the cell)
}
```
`ensureCellState()` preserves state across re-renders **when the shape is
unchanged** — so toggles survive a component swap, but a swap that changes segment
counts resets that cell. State seeds to all-on.

### 6.5 Interactions (matches SPViewer)
- **Click the icon** → toggle the whole cell (all components on, or all off).
- **Click a mandatory block** → toggle that one component on/off.
- **Click an adjustable segment** → toggle that single segment.
- **Shield gating** (`GATED_BY_ALL_ON = new Set(['shields'])`): adjustable
  segments are **dimmed + unclickable** unless every shield generator is on. This
  implements "additional single segments can only be activated if both shields are
  on" (e.g. Idris-P).

### 6.6 Visual / CSS
- `--pm-seg: 18px` — every segment unit is this tall; the mandatory block is
  `calc(var(--pm-seg) * mandatory)`. Columns therefore **grow in height** with
  segment count (no more squish-to-fit). `.pm-grid { align-items: end }` floors them.
- `.pm-bars` is `flex-direction: column-reverse` so mandatory anchors the bottom and
  adjustable stacks upward.
- Segment classes: `.on` (lit), `.gated` (shield lockout), `.off` on the column.
- Header text per column: `N/M` for multi-component cells (active/total), else a
  rough `%`, else `Off`.

### 6.7 Meta-stats math — `powerModifier(catKey, comp)`
Used by `recomputeSidebarStats()` to scale each component's contribution:
- Component off → returns **0**.
- On → blends `comp.power_medium` (mandatory-only) and `comp.power_high`
  (all-segments) by the fraction of adjustable segments lit. Falls back to
  `POWER_LEVEL_FALLBACK` (0.7/0.85/1.0) when a component has no curve.

⚠️ **This is an approximation.** SPViewer's exact per-segment→multiplier curve is
not fully reverse-engineered. The current blend is "good enough" but is the most
likely thing to need refinement (see TODO).

### 6.8 Auto-update wiring
- `populateSidebar(ship)` → `renderPowerGrid()` + `recomputeSidebarStats(ship)`.
- `applyComponentSwap()` (swap modal) → re-renders the grid + recomputes stats.
- Every power click → `recomputeSidebarStats(window.__currentShip)`.
- `sidebarLoadout()` merges the swap-modal's mutable `shipLoadout.current` over the
  raw `ship.components`, so swaps reflect immediately while non-swappable buckets
  still read from the payload.

### 6.9 ⭐ Thermal model — the cooling display (rewritten 2026-06-14)

**The old cooling bar was broken at the data level.** It computed a single
`demand / supply` ratio where demand came from `item_components.heat_baseline` /
`coolant_consumption` — columns that are **100% NULL**. Investigation of the raw
4.8.1 records showed why: the coolant-as-a-resource model is **dormant** — every
component declares `<consumption resource="Coolant"> standardResourceUnits="0"`.
So the bar only ever moved from weapon-fire heat, reading a misleading ~15% on a
fully-powered ship (e.g. Avenger Stalker) because the powerplant / shields / QD /
radar contributed nothing.

**The real 4.8.x heat model is a per-component temperature sim** (`<temperature>`
block): each powered item heats at `baselineTemperatureChange` °C/s, cools toward
a `minCoolingTemperature` floor via `coolingEqualization`, and overheats past
`overheatTemperature`. We now extract those fields (see §3 / `backfill_thermal_params.py`)
into `item_components` and model steady-state temperature per subsystem.

JS lives in `ship_detail.html` (search `computeThermalModel`):
- `thermalUnits()` — gathers every powered heat-generating unit (grid categories
  with `heat_gen_rate`, plus the power plant scaled by load fraction) and the ship
  coolant budget (`Σ cooling_output × cooler throttle`). The quantum drive defaults
  **off** in the grid (not quantum-traveling), so an idle QD correctly adds no heat.
- `computeThermalModel()` — `adequacy = PM_COOLING_GAIN × shipCoolant / totalHeat`;
  each unit's temp above floor = `rawRise / (1 + adequacy)` where
  `rawRise = heatIn / passiveK`. Multiplicative (not subtractive) so a well-cooled
  capital ship stays differentiated instead of collapsing to a flat 0%. Groups by
  subsystem (hottest wins) → `% to overheat`.
- `updateCoolingSummary()` — renders the per-subsystem `.pmt-row` bars (header =
  hottest subsystem) with cyan→amber→orange→red tiers + an overheat redline.

**`PM_COOLING_GAIN` (currently 0.14) is the ONE calibrated constant** — the game's
coolant→cooling conversion is engine-internal and not in the records. Tuned so the
Avenger Stalker (all systems on) sits ~43% to overheat; capital ships read low
(Idris-P ~4%), small/utility ships higher (MOLE ~30%). Reactive: shedding a cooler
or adding hotter (stealth) parts climbs the bars; killing all coolers overheats.

Verified end-to-end (DB → API → headless-Edge render) across 9 ships, no JS errors.

---

## 7. How the 13 slots were discovered
We fed real SPViewer rendered HTML (it's a Vue SPA — WebFetch can't see it, so the
HTML was pasted in manually) for ~12 ships and catalogued the `<!---->` placeholders
vs active columns. Confirmed mapping is in section 6.1. Notable quirks found:
- Mining lasers on a **pilot hardpoint** (Prospector) render in slot 1, not slot 8.
- Vulture's salvage tool rendered under the **Mining** icon (SPViewer quirk).
- Tractor beams are typed `SalvageHead` in the game data but used as Utility (slot 10).

---

## 8. Remaining work / TODO
1. **Reconcile the extractor branch** — merge `feature/ship-dimensions-rsi` into the
   star-citizen-tools `main`/`dev` line so future patch extraction regenerates the
   new tables. (Highest priority — without this the data is a one-off.)
   **Now includes folding `parse_temperature_params` (from `tools/backfill_thermal_params.py`)
   into `dataforge_scitems.py` + the thermal columns into `item_components` SCHEMA, so
   the thermal data regenerates with the pipeline instead of needing the backfill.**
   The backfill script also added `heat_gen_rate` etc. via ALTER on the deployed DB,
   which the feature-branch SCHEMA doesn't declare — reconcile both.
2. **Bespoke icons** for Wheels, EMP, QED slots (currently placeholders).
3. **Refine `powerModifier` math** — validate the segment→multiplier blend against
   SPViewer's actual numbers; the real curve may be stepped, not linear.
3b. **Weapon firing heat in the thermal model** — the steady-state thermal sim
   (§6.9) currently covers avionics/shields/power only. Weapons self-cool via their
   own `item_weapons.cooling_per_second` / `heat_per_shot`, independent of ship
   coolers; add a Weapons thermal entry (the legacy `cellHeatDemand`/
   `computeCoolingSummary` fns kept in `ship_detail.html` have the fire-heat math).
   This is what pushes a ship toward overheat in real gameplay.
3c. **Tune `PM_COOLING_GAIN`** — the one calibrated constant (0.14). If we ever get
   ground-truth in-game temperatures, fit it; for now it's eyeballed off the Stalker.
4. **Verify multi-shield gating visually** on a real 2-shield ship (Idris-P) — the
   gating logic is written but should be eyeballed in the browser.
5. **Mining column power** — MOLE shows 3 mining-head bars in SPViewer; confirm our
   `WeaponMining` rows carry segment power_draw so they render (vs the structural
   `item_tool_arms` which have no direct power).
6. **Stale-row cleanup** — a few SalvageHead/TractorBeam rows may linger in
   `item_weapons` from pre-dispatch pipeline runs; harmless but worth a clean
   re-extract once the branch is merged.
7. **Weapon DPS** is wired (ammo join exists) but the meta-stat shows "data gap"
   for weapons without resolvable ammo — spot-check coverage.

---

## 9. Quick reference — key files
- `sol-provision-tools/app/server.py` → `get_ship_components()` (~line 1100–1262), `api_ship_detail()`
- `sol-provision-tools/app/templates/ship_detail.html` → power grid JS:
  `POWER_CATEGORIES`, `CELL_STATE`, `componentsForCategory`, `powerBarBreakdown`,
  `cellsForCategory`, `ensureCellState`, `renderPowerGrid`, `repaintCell`,
  `powerModifier`, `recomputeSidebarStats`; CSS `.pm-*` rules.
- `star-citizen-tools/extractor/dataforge_scitems.py` (feature branch) — item parsers
- `star-citizen-tools/extractor/dataforge_foundry_loadouts.py` — hardpoint backfill
- `star-citizen-tools/extractor/rsi_ship_matrix.py` — RSI dims
