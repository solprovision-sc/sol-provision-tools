"""
trade_routes.py — Greedy max-profit trade-route optimizer for the Trade page.

Pure, dependency-free (no Flask, no DB) so it can be unit-tested with dict
fixtures. The Flask endpoint marshals rows out of uex_feed.db, calls these
functions, and (optionally) enriches each leg with Quantum Travel time using the
existing QT engine. See app/server.py /api/trade/route.

Pipeline:
    rows (latest snapshot per commodity×terminal, joined to terminal metadata)
      -> build_market(rows)          # group co-located terminals into locations
      -> plan_trade_route(market, p) # greedy: buy cheap here, sell dear there

A "location" groups co-located terminals (a station/city/outpost/moon) so a stop
can both RECEIVE your cargo (sell to a sell-terminal) and RESUPPLY you (buy from
a buy-terminal) — UEX terminals are individually buy-only or sell-only.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence


# ── Location grouping ───────────────────────────────────────────────────────

def _g(t: Mapping, key: str):
    """Mapping.get that also works on sqlite3.Row (which has no .get)."""
    try:
        return t[key]
    except (KeyError, IndexError):
        return None


def location_key(t: Mapping) -> str:
    """Stable key grouping terminals that share a physical place.

    NULL-safe ladder: prefer the finest populated id_* (stable across renames),
    fall back to the matching *_name, and finally isolate a metadata-less
    terminal as its own node so unknowns never wrongly merge into one bucket."""
    sysid = _g(t, "id_star_system") or _g(t, "star_system_name") or "?"
    pairs = (
        ("outpost", _g(t, "outpost_name")),
        ("station", _g(t, "space_station_name")),
        ("city",    _g(t, "city_name")),
        ("moon",    _g(t, "id_moon") or _g(t, "moon_name")),
        ("orbit",   _g(t, "id_orbit") or _g(t, "orbit_name")),
        ("planet",  _g(t, "id_planet") or _g(t, "planet_name")),
    )
    for kind, val in pairs:
        if val:
            return f"sys{sysid}:{kind}:{val}"
    if _g(t, "id_star_system") or _g(t, "star_system_name"):
        return f"sys{sysid}:system"
    return f"terminal:{_g(t, 'id_terminal')}"


def location_label(t: Mapping) -> str:
    """Readable label like 'Stanton · Hurston · Lorville' (falls back gracefully
    to the terminal name when no location metadata is present)."""
    place = (_g(t, "space_station_name") or _g(t, "city_name") or _g(t, "outpost_name")
             or _g(t, "moon_name") or _g(t, "orbit_name") or _g(t, "planet_name")
             or _g(t, "star_system_name") or _g(t, "terminal_name") or _g(t, "name")
             or f"#{_g(t, 'id_terminal')}")
    parts = []
    sysn = _g(t, "star_system_name")
    if sysn:
        parts.append(sysn)
    sub = _g(t, "moon_name") or _g(t, "orbit_name") or _g(t, "planet_name")
    if sub and sub != place:
        parts.append(sub)
    parts.append(place)
    return " · ".join(str(p) for p in parts if p)


def build_market(rows: Sequence[Mapping]) -> dict:
    """Group latest per-(commodity, terminal) snapshots into per-location option
    sets.

    Each row must carry: id_commodity, id_terminal, price_buy, price_sell,
    scu_buy, scu_sell, commodity_code, commodity_name, terminal_name + the
    terminal location columns (star_system_name, planet_name, ...).

    Returns { location_key: {
        key, label, system, planet, moon, place,   # location identity
        terminals: set[int],
        buys:  { id_commodity: {price, scu_buy,  terminal_id, code, name} },
        sells: { id_commodity: {price, scu_sell, terminal_id, code, name} },
    } }
    A buy option exists where price_buy>0 (you source the goods); a sell option
    where price_sell>0 (you offload them). When a location has two buyers of a
    commodity we keep the cheapest; two sellers, the dearest. scu_buy is source
    stock; scu_sell is destination demand (mirrors the detail endpoint)."""
    market: dict = {}
    for r in rows:
        cid = _g(r, "id_commodity")
        if cid is None:
            continue
        key = location_key(r)
        loc = market.get(key)
        if loc is None:
            loc = market[key] = {
                "key": key,
                "label": location_label(r),
                "system": _g(r, "star_system_name"),
                "planet": _g(r, "planet_name"),
                "moon": _g(r, "moon_name"),
                "place": (_g(r, "space_station_name") or _g(r, "city_name")
                          or _g(r, "outpost_name")),
                "terminals": set(),
                "buys": {},
                "sells": {},
            }
        loc["terminals"].add(_g(r, "id_terminal"))

        code = _g(r, "commodity_code")
        name = _g(r, "commodity_name")

        pb = _g(r, "price_buy")
        if pb and pb > 0:
            cur = loc["buys"].get(cid)
            if cur is None or pb < cur["price"]:        # keep the cheapest source
                loc["buys"][cid] = {
                    "price": pb, "scu_buy": _g(r, "scu_buy"),
                    "terminal_id": _g(r, "id_terminal"), "code": code, "name": name,
                }

        ps = _g(r, "price_sell")
        if ps and ps > 0:
            cur = loc["sells"].get(cid)
            if cur is None or ps > cur["price"]:        # keep the dearest buyer
                loc["sells"][cid] = {
                    "price": ps, "scu_sell": _g(r, "scu_sell"),
                    "terminal_id": _g(r, "id_terminal"), "code": code, "name": name,
                }
    return market


# ── Greedy optimizer ────────────────────────────────────────────────────────

@dataclass
class RouteParams:
    cargo_scu: float
    capital: float
    start_key: str
    stops: int
    end_key: str | None = None      # only the FINAL leg is forced to end here


def _units_and_bound(capital: float, buy: Mapping, sell: Mapping,
                     cargo_scu: float) -> tuple[int, str, list]:
    """Largest whole-SCU haul and which constraint bound it.

    Limits: capital (afford), cargo (hold), stock (source scu_buy), demand
    (destination scu_sell). UEX often reports 0/NULL stock or demand where a
    trade is still possible, so treat those as unbounded but flag them."""
    flags = []
    limits = {
        "capital": capital / buy["price"] if buy["price"] > 0 else math.inf,
        "cargo": cargo_scu,
    }
    if buy.get("scu_buy"):
        limits["stock"] = buy["scu_buy"]
    else:
        flags.append("stock_unknown")
    if sell.get("scu_sell"):
        limits["demand"] = sell["scu_sell"]
    else:
        flags.append("demand_unknown")

    bound = min(limits, key=limits.get)
    units = int(math.floor(min(limits.values())))
    return units, bound, flags


def _candidate(cur_loc: Mapping, dest: Mapping, cid, buy: Mapping, sell: Mapping,
               units: int, bound: str, flags: list, capital_before: float) -> dict:
    spend = units * buy["price"]
    revenue = units * sell["price"]
    profit = revenue - spend
    return {
        "from_key": cur_loc["key"], "from_label": cur_loc["label"],
        "from_system": cur_loc["system"],
        "to_key": dest["key"], "to_label": dest["label"], "to_system": dest["system"],
        "commodity_id": cid, "code": buy["code"], "name": buy["name"],
        "buy_price": buy["price"], "sell_price": sell["price"],
        "units": units, "spend": round(spend, 2), "revenue": round(revenue, 2),
        "profit": round(profit, 2), "margin": round(sell["price"] - buy["price"], 2),
        "capital_before": round(capital_before, 2),
        "capital_after": round(capital_before + profit, 2),
        "bound_by": bound, "flags": flags,
        "qt": None,                                 # filled in by the endpoint
    }


def plan_trade_route(market: dict, params: RouteParams) -> dict:
    """Greedy: at each location buy the commodity whose best elsewhere-sale yields
    the most profit (bounded by capital, cargo, stock, demand); reinvest proceeds
    into the next leg. Only the final leg is constrained to end_key. Stops early
    when no profitable move exists, returning the legs found so far."""
    cur = params.start_key
    capital = params.capital
    legs: list = []
    warnings: list = []
    reason = "completed"

    for i in range(params.stops):
        last = (i == params.stops - 1)
        loc = market.get(cur)
        if loc is None:
            warnings.append(f"unknown location: {cur}")
            reason = "unknown_location"
            break

        # Candidate destinations: any location for a normal leg; only end_key for
        # the final leg when an ending location was requested.
        if last and params.end_key:
            end_loc = market.get(params.end_key)
            dests = [end_loc] if end_loc else []
        else:
            dests = market.values()

        best = None  # (profit, candidate)
        for cid, buy in loc["buys"].items():
            for d in dests:
                if d is None or d["key"] == cur:
                    continue
                sell = d["sells"].get(cid)
                if not sell or sell["price"] <= buy["price"]:
                    continue
                units, bound, flags = _units_and_bound(capital, buy, sell, params.cargo_scu)
                if units <= 0:
                    continue
                cand = _candidate(loc, d, cid, buy, sell, units, bound, flags, capital)
                if best is None or cand["profit"] > best[0]:
                    best = (cand["profit"], cand)

        if best is None:
            reason = "no_route_to_end" if (last and params.end_key) else "no_profitable_move"
            break

        leg = best[1]
        legs.append(leg)
        capital = leg["capital_after"]
        cur = leg["to_key"]

    return {
        "legs": legs,
        "total_profit": round(sum(l["profit"] for l in legs), 2),
        "final_capital": round(capital, 2),
        "starting_capital": round(params.capital, 2),
        "stops_requested": params.stops,
        "stops_planned": len(legs),
        "stopped_reason": reason,
        "warnings": warnings,
    }


# ── Terminal → nav_point matching (Quantum Travel bonus) ─────────────────────

_SYSTEM_ALIAS = {"STANTON": "STANTON", "PYRO": "PYRO", "NYX": "NYX"}


def _norm(s) -> str:
    """Lowercase and strip punctuation/whitespace: 'Grim HEX' -> 'grimhex'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def build_navpoint_index(by_uuid: Mapping, resolve) -> dict:
    """Index nav_points by (system_code, normalized display_name) -> location_uuid
    so a UEX location can be matched to coordinates for QT timing.

    by_uuid/resolve come from server._build_navpt_hierarchy(): by_uuid[uuid] has
    display_name/location_key/kind; resolve(uuid) yields {system, planet, moon}.
    First writer wins for a given (system, name) so a primary body/station keeps
    the slot over an obscure duplicate."""
    index: dict = {}
    for uuid, node in by_uuid.items():
        h = resolve(uuid)
        system = h.get("system")
        if not system:
            continue
        for nm in (node.get("display_name"), node.get("location_key"),
                   h.get("moon"), h.get("planet")):
            k = (system, _norm(nm))
            if k[1] and k not in index:
                index[k] = uuid
    return index


def match_location_to_navpoint(loc: Mapping, index: Mapping) -> str | None:
    """Best-effort match of a market location to a nav_point uuid. Tries the
    specific place name first, then the moon, then the planet. Returns None when
    nothing resolves (the leg's QT is then reported as unknown)."""
    sysname = loc.get("system")
    if not sysname:
        return None
    system = _SYSTEM_ALIAS.get(_norm(sysname).upper(), _norm(sysname).upper())
    for nm in (loc.get("place"), loc.get("moon"), loc.get("planet")):
        if not nm:
            continue
        uuid = index.get((system, _norm(nm)))
        if uuid:
            return uuid
    return None
