# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Sol Provision Tools — a Flask data-intelligence site for the Sol Provision *Star Citizen* org (live at https://tools.solprovision.com). It surfaces ship/component/crafting/mining/cargo data mined from the game files, plus org-member features (ownership claims, officer dashboards) gated behind Discord login.

The app reads almost everything from `dataforge.db`, a SQLite snapshot produced by a **separate** extraction pipeline (the `star-citizen-tools` / DataForge repo on the extraction machine). That DB is **not** version-controlled here — it's SCP'd to the VPS independently and the extractor **replaces it wholesale every patch**. This single fact drives much of the architecture (see Databases below).

## Running locally

```powershell
# Easiest: double-click run_local.bat, or
python tools/run_local.py                 # http://localhost:5000, fake officer session
python tools/run_local.py --logged-out    # preview signed-out state
python tools/run_local.py --port 8080
python tools/run_local.py --db path/to/other.db
```

`tools/run_local.py` is the only correct way to run locally. It **stubs out `firebase_admin`** (so you don't need the SDK or a credentials file), points the app at `app/dataforge.db` + repo-root user/ownership DBs, runs the startup migrations, and **injects a fake rank-5 session** so auth-gated UI renders. It does this without editing `server.py`. Note: `tools/run_local.py` is **gitignored** — it exists locally but won't show in git status.

Auth-gated **API** calls (claim buttons, `/api/officers/*`) still hit the real membership check and will 403 for the fake user — that's expected. Page layout and all public data work.

There are no tests, linters, or build step. It's Flask + vanilla JS + Jinja2 served directly.

## Architecture

**`app/server.py`** (~5300 lines) is the monolith — almost every route, all DB helpers, all data-shaping logic. Routes split into:
- **Page routes** (`/ships`, `/crafting`, `/cargo-planner`, `/starmap`, …) → render a Jinja template, mostly thin.
- **`/api/*` routes** → the real work; query `dataforge.db`, shape JSON, return it. The frontend JS fetches these.

**`app/officer_db.py`** is a Flask **Blueprint** (registered in `server.py`) — an officer-only ad-hoc SQL query tool over `dataforge.db`. It's the one place arbitrary SQL runs, so it uses a read-only connection + `validate_query()` allowlisting + a statement-timeout watchdog. Keep that gating intact.

**`app/helpers/`** — two pure-ish compute modules, layered and importable standalone:
- `quantum_travel.py` — the quantum-travel (QT) physics + nav-graph + routing engine. Three layers: Layer 1 pure physics (white-paper travel-time model + fuel), Layer 2 nav graph (reads `nav_points`, line-of-sight/OM detour routing), Layer 3 stitches sublight→QT→sublight legs. Used by the cargo route endpoints.
- `route_optimizer.py` — sits *on top of* the QT engine, treating it purely as a cost oracle. Solves the cargo pickup-and-delivery ordering problem (Held-Karp exact DP for small stop counts, greedy fallback above a threshold). It chooses the *order* of stops; QT physics lives one level down.

**Frontend** — `app/templates/*.html` (Jinja, extending `base.html` / `base_builder.html`) + `app/static/js/common.js` (shared `api()` fetch helper + name/stat formatters). No framework, no bundler. Each page's logic is mostly inline `<script>` in its template.

**Starmap** (`app/static/starmap/`) is the exception: a real ES-module Three.js app. `starmap.js` is the orchestrator; modules split into `core/` (renderer, camera, loop), `scene/` (planets, moons, jump points, belts, bloom, ore-heat), `data/` (systems, coords, textures), `ui/`, `util/`. It pulls body positions from `/api/starmap/<system>/bodies`. There is significant accumulated design context in the memory files (starmap_* and qt_*) — consult them before touching coordinate scaling, bloom, or QT physics.

### Patch versioning — read this before writing any data query

`dataforge.db` holds only the latest patch to keep the database 'nimble'.  However, server.py and other files likely still contain queries/filters for 'latest patch'.  Previous patches are archived to `dataforge_archive.db` before being removed from the primary db.

```python
conn = get_db(); p = PATCH or latest_patch(conn)
# ... then every query filters WHERE patch_version = ?  with p
```

`PATCH` is a module global (normally `None` → resolve latest). When you add a data query, you almost always need to scope it to `p` the same way, or you'll bleed rows across patches.

`EXCLUDE` (a SQL fragment near the top) filters out AI/template/non-flyable ship variants — reuse it in ship queries rather than re-deriving the list.

## Databases (and why there are so many)

| File | Holds | Notes |
|---|---|---|
| `dataforge.db` | All mined game data (ships, components, crafting, mining, nav, missions, loc) | **Not in git.** Replaced wholesale each patch by the extractor. ~200 tables, patch-versioned. |
| `blueprint_ownership.db` | Crafting blueprint claims | Standalone **so it survives the dataforge swap.** Auto-created. `env` column ('prod'/'dev') keeps dev-tool claims out of prod. |
| `ship_ownership.db` | Ship claims + saved loadouts | Same standalone-survival design as above. |
| `cargo_planner.db` | Saved cargo plans + activity | Schema applied from `tools/init_cargo_planner_db.py`. |
| `mee6_snapshots.db` | Discord member roster (rank, division) from SPARQy | The user/auth DB. On Linux deploys the canonical copy is under `/var/www/sparqy/data/`. |

**Key rule:** anything the app *writes* must live in its own DB file, never in `dataforge.db` — the next patch import would erase it. The ownership/cargo helpers (`get_ownership_db`, `get_ship_ownership_db`, etc.) auto-create their schema and run inline column migrations on connect, so a fresh deploy needs no manual setup. `get_db_with_ownership()` ATTACHes the ownership DB as `own.` for JOINs against crafting blueprints.

All paths resolve from env vars first (`DATAFORGE_DB`, `OWNERSHIP_DB`, `SHIP_OWNERSHIP_DB`, `MEE6_DB`, …), then fall back to locations relative to `DB_PATH`. `run_local.py` sets these to repo-root copies.

`ensure_columns()` / `ensure_indexes()` run at startup (and in `run_local.py`) to add localization columns and the `idx_item_components_entity` index the crafting page's hot join needs — idempotent, safe every boot.

## Auth & environment detection

Login is **Discord OAuth via Firebase**. The frontend gets a Firebase ID token; `POST /api/auth/verify` verifies it server-side, extracts the Discord ID from `identities['oidc.discord']`, looks the member up in `mee6_snapshots.db`, and sets a Flask session (`discord_id`, `username`, `callsign`, `rank`, `division`). Decorators enforce access:
- `require_org_member` — must be a current member (re-checks the roster).
- `require_officer` — rank ≥ 5.
- `require_page_login` — UI pages; bounces anonymous users to `/`; on **dev** also refuses rank < 4.

**Three environments**, detected by the process's **own install path** (`server.py` top):
- `is_local` — Windows (`os.name == 'nt'`) → uses the **dev** Firebase project.
- `is_dev` — Linux under `/var/www/sol-provision-tools-dev` → dev Firebase, **gated to ranks 4+**.
- prod — Linux otherwise → prod Firebase.

⚠️ Dev and prod are **co-located on the same VPS**, so dev/prod must be distinguished by *this process's* `__file__` path, **not** by whether the dev directory exists on disk. Getting that wrong previously locked all of prod out (commit `685b55c`). Don't reintroduce existence-based env checks.

## Conventions

- **PowerShell** is the default shell (Windows main machine). The Bash tool exists for POSIX scripts but can't see just-written files immediately (sandbox FS lag) — run freshly written scripts via PowerShell.
- Name cleanup is duplicated intentionally on both sides: Python (`clean_career`, `clean_role`, `best_name`, `_prettify_faction` in `server.py`) and JS (`cleanName`, `cleanCareer`, `getMfr` in `common.js`). Keep the manufacturer-prefix map (`getMfr`) in sync when adding ships.
- Display capacity logic: prefer `rsi_cargo_scu` (authoritative published value) over in-game grid sum `cargo_scu` — see `docs/cargo_validation.md`.
- `POWER_MANAGEMENT_HANDOVER.md` documents the ship-detail power-management panel and the cross-repo thermal-extraction work (the heat model is a per-component `<temperature>` sim, **not** the dormant coolant-resource fields). Read it before touching power/thermal display.
