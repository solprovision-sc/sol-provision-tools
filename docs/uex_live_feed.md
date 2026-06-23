# UEX Live Commodity Feed — Design & Handover

> **Status:** Planned, not yet built (scoped 2026-06-22).
> **Target repo:** `sol-provision-tools` — this doc lives at `sol-provision-tools/docs/uex_live_feed.md`.
> **Why this doc exists:** Claude's memory is project-scoped to `star-citizen-tools`; it won't auto-load in a `sol-provision-tools` session. This doc is the portable spec/handover.

## Goal

Pull live Star Citizen commodity prices from the [UEX API](https://uexcorp.space/api/documentation/) every 15 minutes into a dedicated SQLite database, and display them on a web page in the `sol-provision-tools` Flask app — always live within 15 minutes, with full price history for trending.

This is the **live economic overlay** that complements the static catalog produced by `star-citizen-tools` (`dataforge.db`): the extractor gives authoritative ship/item/component specs from the game files; UEX gives crowdsourced, in-game-observed prices the game files don't contain.

## Terms of Use / attribution (read before shipping)

Reviewed <https://uexcorp.space/about/terms> and the API docs. Key findings:

- **Free to use** via a registered app + Bearer token.
- **Third-party republishing is not explicitly addressed** in the written Terms — neither permitted nor prohibited (genuine gray area).
- Terms restrict website use to **"personal, non-commercial purposes only."** This project is non-commercial → on the right side of it. Revisit if the site ever takes ads/donations.
- **Attribution is not contractually mandated**, but the API docs provide a **"Powered by UEX" badge** that is encouraged. The existence of a public API + badge strongly signals UEX wants third parties displaying their data with credit.

**Decision:** proceed as a non-commercial tool, display the "Powered by UEX" attribution prominently on the page, and post a one-line confirmation request in the UEX Discord that third-party display-with-credit is welcome (removes the only real ambiguity since the page is outward-facing).

## Data source

- **Base URL:** `https://api.uexcorp.uk/2.0/`
- **Primary endpoint:** `GET /commodities_prices_all` — returns **every commodity × terminal price row in a single call**.
- **Auth:** Bearer token from a free app registered on UEX "My Apps". Send `Authorization: Bearer <token>` (+ optional `X-Client-Version` header).
- **Rate limit:** 120 req/min, 172,800 req/day. We use ~96 calls/day → non-issue.
- **Source cadence (important):** the bulk endpoint has a **30-min cache TTL and refreshes ~hourly** on UEX's side. So a row's data only changes ~hourly even though we poll every 15 min. This is why we de-dup on `date_modified` (below) rather than blindly inserting every poll.

### `commodities_prices_all` response fields

Response: `{ "status": "ok", "data": [ { ...row... }, ... ] }`

| Field | Type | Meaning |
|---|---|---|
| `id` | int | UEX price record id |
| `id_commodity` | int | FK → commodity |
| `id_terminal` | int | FK → terminal |
| `price_buy` | float | Latest buy price (per SCU) |
| `price_buy_avg` | float | Average buy price |
| `price_sell` | float | Latest sell price (per SCU) |
| `price_sell_avg` | float | Average sell price |
| `scu_buy` | float | Latest buy volume |
| `scu_buy_avg` | float | Average buy volume |
| `scu_sell_stock` | float | Latest stock available |
| `scu_sell_stock_avg` | float | Average stock |
| `scu_sell` | float | Sell volume |
| `scu_sell_avg` | float | Average sell volume |
| `status_buy` | int\|null | Buy status indicator |
| `status_sell` | int\|null | Sell status indicator |
| `container_sizes` | str\|null | Supported container sizes (CSV) |
| `quality` | int\|null | Report quality (0–1000) |
| `date_added` | int | Record creation (unix) |
| `date_modified` | int | Last modification (unix) — **change-detection key** |
| `commodity_name` / `commodity_code` / `commodity_slug` | str | Denormalized commodity ident |
| `terminal_name` / `terminal_code` / `terminal_slug` | str | Denormalized terminal ident |

## Architecture

- **One standalone SQLite db** dedicated to this feed (e.g. `uex_feed.db`), separate from `dataforge.db`.
- **Dev and prod both run on the OCI VPS and share the same db file** — single cron writer + page readers on one machine (no cross-machine sync).
- **Enable WAL mode** (`PRAGMA journal_mode=WAL`) so the 15-min writer never blocks page reads.
- **Cron every 15 min** on the VPS runs the pull script.
- **Flask page** in `sol-provision-tools` reads the db and renders current prices + trend views, with the UEX attribution.

## Write strategy — append-only, changed rows only

Always **insert, never upsert** — we want full history for trending. But because the source only changes ~hourly, we insert a row **only when its `date_modified` advanced**, so we don't store 4 identical snapshots per hour.

**Implementation trick:** a `UNIQUE(id_commodity, id_terminal, date_modified)` constraint + `INSERT OR IGNORE` makes the de-dup automatic — every poll attempts to insert all rows; unchanged ones (same `date_modified`) are silently ignored; only genuinely new observations land. No manual last-seen tracking needed.

**Null `date_modified` guard:** crowdsourced rows occasionally arrive with a null `date_modified`. Since the column is `NOT NULL` *and* part of the de-dup key (and SQLite treats every NULL as distinct, which would defeat `INSERT OR IGNORE`), coalesce per row before insert — fall back to `date_added`, then to `pulled_at` — so one bad row neither aborts the batch nor spams duplicate history. Log any coalesced/skipped rows to `pull_log.note`.

## Schema sketch

```sql
PRAGMA journal_mode = WAL;

-- Append-only price history. One row per (commodity, terminal, observation).
CREATE TABLE IF NOT EXISTS price_snapshots (
    uex_id            INTEGER,           -- UEX's own price record id; stable handle back to their row for tracing (nullable, non-unique)
    id_commodity      INTEGER NOT NULL,
    id_terminal       INTEGER NOT NULL,
    price_buy         REAL,
    price_buy_avg     REAL,
    price_sell        REAL,
    price_sell_avg    REAL,
    scu_buy           REAL,
    scu_buy_avg       REAL,
    scu_sell_stock    REAL,
    scu_sell_stock_avg REAL,
    scu_sell          REAL,
    scu_sell_avg      REAL,
    status_buy        INTEGER,
    status_sell       INTEGER,
    quality           INTEGER,
    date_modified     INTEGER NOT NULL,  -- UEX source timestamp (change key)
    pulled_at         INTEGER NOT NULL,  -- our ingest unix time
    UNIQUE (id_commodity, id_terminal, date_modified)  -- auto de-dup via INSERT OR IGNORE
);

-- Powers the homepage "latest row per (commodity, terminal)" query.
CREATE INDEX IF NOT EXISTS idx_snap_latest    ON price_snapshots (id_commodity, id_terminal, date_modified DESC);
-- Single-axis trend views.
CREATE INDEX IF NOT EXISTS idx_snap_commodity ON price_snapshots (id_commodity, date_modified);
CREATE INDEX IF NOT EXISTS idx_snap_terminal  ON price_snapshots (id_terminal, date_modified);

-- Reference tables (names/codes change rarely; refresh occasionally, e.g. daily).
-- Also upserted on-the-fly during each pull (see write strategy) so newly-seen ids never render as null names.
CREATE TABLE IF NOT EXISTS commodities (
    id_commodity INTEGER PRIMARY KEY,
    name TEXT, code TEXT, slug TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS terminals (
    id_terminal INTEGER PRIMARY KEY,
    name TEXT, code TEXT, slug TEXT,
    updated_at INTEGER
);

-- Pull log for observability. Autoincrement PK so two runs in the same second
-- (e.g. a manual backfill alongside the cron run) never collide on pulled_at.
CREATE TABLE IF NOT EXISTS pull_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pulled_at INTEGER NOT NULL,
    rows_fetched INTEGER,
    rows_inserted INTEGER,
    http_status TEXT,
    note TEXT
);
```

> Snapshots store **ids only** (lean) to keep history small; names live in the reference tables and are joined at query time. Denormalizing names into snapshots is acceptable to start but bloats history — prefer the reference-table split.

## Build checklist

1. [ ] Register a UEX app → obtain Bearer token; store it in config/env (never commit it; mirror the `config.ini` / `config.example.ini` pattern from `star-citizen-tools`).
2. [ ] Confirm third-party display-with-credit in UEX Discord (terms are silent).
3. [ ] Create `uex_feed.db` with the schema above (WAL on).
4. [ ] Write the pull script: one `commodities_prices_all` call → `INSERT OR IGNORE` all rows into `price_snapshots` with `pulled_at` (coalescing null `date_modified`); upsert any unseen commodity/terminal ids into the reference tables in the same run using the denormalized names in the payload; write a `pull_log` row each run. On non-200 or empty `data`, log it and insert nothing (no partial batch).
5. [ ] Cron the script every 15 min on the OCI VPS.
6. [ ] Build the Flask page: current prices (latest row per commodity/terminal) + trend view; include "Powered by UEX" attribution + logo.
7. [ ] Decide retention/aggregation policy once history volume is known (rows ≈ commodities × terminals × hourly changes).

## Open questions

- Token storage / config pattern in `sol-provision-tools` (match `config.ini` convention?).
- Exact db filename + path on the VPS.
- Retention: keep full history forever, or roll up older snapshots to daily/hourly aggregates after N months?
- Do we eventually map UEX `id_commodity` / `id_terminal` to our `dataforge.db` UUIDs to join live prices against static specs? (Future enhancement.)
