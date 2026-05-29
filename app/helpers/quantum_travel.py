"""Quantum travel (QT) engine — standalone, pluggable.

A single self-contained module other tools can import to turn an origin and a
destination into a quantum-travel duration (+ fuel), including obstruction
routing and placeholder atmospheric departure/arrival legs.

Fuel uses the drive's fuel_per_gm_mscu rate, which arrives as a field on the
``qd`` input row (alongside drive_speed and the accel stats).

It is organised in three layers; each layer's public symbols are importable on
their own, so a consumer that only needs the pure calculator can use Layer 1
without touching the nav graph or the database:

    Layer 1 — PHYSICS (pure, no deps)
        qt_travel_time, qt_fuel_scu, qt_leg_cost
        The white-paper travel-time model + fuel. Zero DB dependency.

    Layer 2 — NAV GRAPH (reads nav_points)
        line_of_sight_clear, segment_blocked_by, NavGraph, build_nav_graph
        Line-of-sight geometry and OM detour routing within a system.

    Layer 3 — ROUTING
        plan_route, plan_leg, sublight_transit_cost, has_atmosphere
        Stitches departure sublight -> QT hop(s) -> arrival sublight.

Typical use::

    from helpers.quantum_travel import NavGraph, plan_route
    nav = NavGraph(conn, patch)                       # build once, reuse
    route = plan_route([(origin_uuid, dest_uuid)], ship, qd, nav)
    # route = {legs:[...], total_time_s, total_fuel_scu, warnings}

Pure-calculator-only use::

    from helpers.quantum_travel import qt_leg_cost
    cost = qt_leg_cost(distance_m, quantum_drive_row)

Units: the physics runs in native SI (m, m/s, m/s^2, s); the nav graph runs in
km (matching nav_points.helio_* / body_radius_km). Conversions happen only at
the Layer-3 boundary (km -> m when feeding a distance into the physics).

Provenance: travel-time model from Erec's "A study on Quantum Travel time"
(Alpha 3.12, 7 Apr 2021); fuel from item_quantum_drives.fuel_per_gm_mscu;
QT-engage gates (2 km altitude / 0.4 atm) from quantumdriveglobalparams.
"""

from __future__ import annotations

import heapq
import math
import sqlite3
from collections import deque
from typing import Iterable, Mapping, Optional, Sequence

__all__ = [
    # Layer 1 — physics
    "qt_travel_time", "qt_fuel_scu", "qt_leg_cost", "QtInputError",
    # Layer 2 — nav graph
    "line_of_sight_clear", "segment_blocked_by", "distance",
    "NavGraph", "build_nav_graph", "NavError", "CrossSystemError",
    # Layer 3 — routing
    "plan_route", "plan_leg", "sublight_transit_cost",
    "has_atmosphere", "ATMOSPHERIC_BODY_KEYS",
    # tunable placeholders
    "SUBLIGHT_AIRLESS_S", "SUBLIGHT_ATMOSPHERIC_S", "JUMP_POINT_TRAVERSAL_S",
]


# ════════════════════════════════════════════════════════════════════════
# TUNABLE PLACEHOLDER CONSTANTS  — revisit when the real models are studied.
# ════════════════════════════════════════════════════════════════════════
# Sublight departure/arrival: flat seconds to climb from a surface POI to an
# altitude where QT can engage (and the reverse on arrival). Not yet a real
# climb model — see the atmospheric findings note (gates: 2 km / 0.4 atm).
SUBLIGHT_AIRLESS_S = 20.0        # body with no (QT-gating) atmosphere
SUBLIGHT_ATMOSPHERIC_S = 45.0    # body with an atmosphere

# Jump-point traversal: flat seconds to cross a jump tunnel between systems.
# Placeholder until jump tunnels are studied. (Cross-system routing not yet
# wired; this constant is defined here ready for it.)
JUMP_POINT_TRAVERSAL_S = 60.0


# ════════════════════════════════════════════════════════════════════════
# LAYER 1 — PHYSICS (pure functions; the white-paper travel-time model)
# ════════════════════════════════════════════════════════════════════════
#
# Travel-time model from Erec's white paper. The QT motion is a symmetric
# three-phase profile: accelerate (Ta) -> cruise (Tc) -> decelerate (Ta), with
# acceleration ramping linearly a1->a2; Ttot = 2*Ta + Tc. Only vmax(=drive_speed),
# a1 and a2 matter. Short jumps that never reach vmax use the closed-form cubic
# solution (eq. 22) with a real-domain hyperbolic bypass (eq. 23).
#
# NOT covered by the paper (handled separately / flagged):
#   * Fuel — uses fuel_per_gm_mscu (the project's source of truth).
#   * Spool / calibration / cooldown — additive overhead from the QD stats.
#   * Obstruction routing, jump tunnels, atmospheric flight — Layers 2-3.
#   * Orbital ("spline") quantum — explicitly out of scope of the paper.

class QtInputError(ValueError):
    """Raised when a quantum-drive row is missing a parameter the math needs."""


# Accept either a raw `item_quantum_drives` sqlite row (dict-like) or any
# mapping that carries the same column names. We look up by the canonical DB
# column name first, then a couple of friendly aliases used elsewhere in the
# app (e.g. get_ship_components() aliases them to accel1/accel2).
_FIELD_ALIASES = {
    "drive_speed":          ("drive_speed", "vmax"),
    "stage_one_accel_mps2": ("stage_one_accel_mps2", "accel1", "a1"),
    "stage_two_accel_mps2": ("stage_two_accel_mps2", "accel2", "a2"),
    "spool_up_time":        ("spool_up_time",),
    "calibration_rate":     ("calibration_rate",),
    "calibration_delay":    ("calibration_delay",),
    "cooldown_time":        ("cooldown_time",),
    "fuel_per_gm_mscu":     ("fuel_per_gm_mscu",),
}


def _get(qd: Mapping, canonical: str, required: bool = False,
         default: Optional[float] = None) -> Optional[float]:
    for key in _FIELD_ALIASES[canonical]:
        if key in qd and qd[key] is not None:
            return float(qd[key])
    if required:
        raise QtInputError(
            f"quantum drive is missing required field '{canonical}' "
            f"(looked for {_FIELD_ALIASES[canonical]})"
        )
    return default


def qt_travel_time(distance_m: float, vmax: float, a1: float, a2: float) -> dict:
    """Flight time for a straight-line QT jump of ``distance_m`` metres.

    This is the white-paper model and *only* the flight portion (accelerate
    -> cruise -> decelerate). No spool / calibration / cooldown.

    Parameters are in SI: vmax in m/s, a1/a2 in m/s^2, distance in m.

    Returns a dict::

        {
          "travel_time_s": float,   # 2*Ta + Tc
          "accel_s": float,         # Ta
          "cruise_s": float,        # Tc  (0 for short jumps)
          "decel_s": float,         # Ta
          "reaches_vmax": bool,
        }
    """
    if a1 <= 0 or a2 <= 0 or vmax <= 0:
        raise QtInputError("vmax, a1 and a2 must all be positive")
    if a2 <= a1:
        # The model derives ta = (a2-a1)/ma and divides by (a2^2 - a1^2);
        # a2 must exceed a1 for the accel ramp (and the formula) to be valid.
        raise QtInputError(f"stage-two accel ({a2}) must exceed stage-one ({a1})")

    if distance_m <= 0:
        return {"travel_time_s": 0.0, "accel_s": 0.0, "cruise_s": 0.0,
                "decel_s": 0.0, "reaches_vmax": False}

    d = float(distance_m)

    # Cruise-distance discriminant (paper eq. 14). dc >= 0 => the drive
    # reaches vmax and has a genuine cruise phase.
    dc = d - (4.0 * vmax**2 * (2.0 * a1 + a2)) / (3.0 * (a1 + a2) ** 2)

    if dc >= 0.0:
        # Long jump — reaches vmax (eq. 16).
        # Ta = 2*vmax/(a1+a2)  (eq. 12);  Tc = dc / vmax.
        ta = 2.0 * vmax / (a1 + a2)
        tc = dc / vmax
        total = 2.0 * ta + tc
        return {"travel_time_s": total, "accel_s": ta, "cruise_s": tc,
                "decel_s": ta, "reaches_vmax": True}

    # Short jump — never reaches vmax (eq. 22 / 23). Tc = 0; the profile is a
    # symmetric accel/decel about the midpoint tx, so travel_time = 2*tx.
    z = (3.0 * (a2 - a1) ** 2 * (a1 + a2) ** 2 * d) / (8.0 * vmax**2 * a1**3) - 1.0
    pre = 4.0 * a1 * vmax / (a2**2 - a1**2)
    if z > 1.0:
        # acos(z) leaves the real domain; use the hyperbolic bypass (eq. 23).
        total = pre * (2.0 * math.cosh(math.log(z - math.sqrt(z**2 - 1.0)) / -3.0) - 1.0)
    else:
        # z may dip to -1 for tiny jumps; acos handles [-1, 1] (eq. 22).
        total = pre * (2.0 * math.cos(math.acos(z) / 3.0) - 1.0)

    tx = total / 2.0
    return {"travel_time_s": total, "accel_s": tx, "cruise_s": 0.0,
            "decel_s": tx, "reaches_vmax": False}


def qt_fuel_scu(distance_m: float, fuel_per_gm_mscu: float) -> float:
    """Quantum fuel burned over ``distance_m`` metres, in SCU.

    fuel_per_gm_mscu is milli-SCU consumed per gigametre of QT (the project's
    confirmed source of truth for QT fuel). 1 Gm = 1e9 m; 1 SCU = 1000 mSCU.
    """
    if distance_m <= 0:
        return 0.0
    gigametres = distance_m / 1.0e9
    return fuel_per_gm_mscu * gigametres / 1000.0


# CALIBRATION note: the white paper does not model calibration time. In game
# the pre-jump calibration grows with distance, but the exact relation to
# `calibration_rate` is not pinned down by any source we trust yet. We model
# it as the fixed `calibration_delay` only (a lower bound) and leave the
# distance-dependent term off by default so every number we report is either
# validated or an explicit, documented assumption. Pass
# include_calibration_ramp=True to add a first-order ramp once it is verified.
def _calibration_time(distance_m: float, qd: Mapping,
                      include_calibration_ramp: bool) -> float:
    delay = _get(qd, "calibration_delay", default=0.0) or 0.0
    if not include_calibration_ramp:
        return delay
    rate = _get(qd, "calibration_rate", default=0.0) or 0.0
    if rate <= 0 or distance_m <= 0:
        return delay
    # First-order, UNVERIFIED: percent calibrated accrues at `rate` per second
    # and a full jump needs 100%, scaled by distance in gigametres. Treat as a
    # placeholder until cross-checked in game.
    gigametres = distance_m / 1.0e9
    return delay + (100.0 * gigametres) / rate


def qt_leg_cost(distance_m: float, qd: Mapping, *,
                include_overhead: bool = True,
                include_calibration_ramp: bool = False) -> dict:
    """Full cost of a single straight-line QT leg.

    ``qd`` is an ``item_quantum_drives`` row (or any mapping with the same
    column names; common app aliases like accel1/accel2 are also accepted).

    Returns::

        {
          "distance_m": float,
          "time_s": float,            # total incl. overhead (if requested)
          "travel_time_s": float,     # validated flight time only
          "fuel_scu": float,
          "reaches_vmax": bool,
          "phases": {
            "spool": float, "calibration": float,
            "accel": float, "cruise": float, "decel": float,
            "cooldown": float,
          },
        }

    The flight portion (accel/cruise/decel) is the white-paper model and is
    the validated quantity. spool/calibration/cooldown are additive overhead;
    set include_overhead=False to get the flight + fuel only. Fuel uses the
    drive's fuel_per_gm_mscu field on ``qd``.
    """
    vmax = _get(qd, "drive_speed", required=True)
    a1 = _get(qd, "stage_one_accel_mps2", required=True)
    a2 = _get(qd, "stage_two_accel_mps2", required=True)

    flight = qt_travel_time(distance_m, vmax, a1, a2)
    fuel = qt_fuel_scu(distance_m, _get(qd, "fuel_per_gm_mscu", default=0.0) or 0.0)

    if include_overhead and distance_m > 0:
        spool = _get(qd, "spool_up_time", default=0.0) or 0.0
        calibration = _calibration_time(distance_m, qd, include_calibration_ramp)
        cooldown = _get(qd, "cooldown_time", default=0.0) or 0.0
    else:
        spool = calibration = cooldown = 0.0

    phases = {
        "spool": spool,
        "calibration": calibration,
        "accel": flight["accel_s"],
        "cruise": flight["cruise_s"],
        "decel": flight["decel_s"],
        "cooldown": cooldown,
    }
    total = sum(phases.values())

    return {
        "distance_m": float(distance_m) if distance_m > 0 else 0.0,
        "time_s": total,
        "travel_time_s": flight["travel_time_s"],
        "fuel_scu": fuel,
        "reaches_vmax": flight["reaches_vmax"],
        "phases": phases,
    }


# ════════════════════════════════════════════════════════════════════════
# LAYER 2 — NAV GRAPH (line-of-sight geometry + OM detour routing)
# ════════════════════════════════════════════════════════════════════════
#
# Coordinate frame: nav_points.helio_* is heliocentric PER SYSTEM (origin = that
# system's star), so coordinates are only comparable within one system. Layer 2
# is within-system; inter-system trips go through a jump point (Layer 3).
#
# Obstruction model: a straight segment A->B is blocked by a body of radius R
# (+ optional margin) iff the segment's *interior* passes within R+margin of the
# body centre. If the nearest approach is at an endpoint, that body is the
# origin/destination body, not a mid-path obstruction — so it doesn't block.
# Units here are kilometres (matching helio_* / body_radius_km).

# Leftover test assets that may still be present in a DB copy (the handoff
# notes "Green"/Ellis3 should be excluded). Match on location_key prefix.
EXCLUDE_KEY_PREFIXES = ("Ellis",)

# Star location_key -> system code.
_STAR_KEY_TO_SYSTEM = {
    "StantonStar": "Stanton",
    "PyroStar": "Pyro",
    "NyxStar": "Nyx",
}

Vec = Sequence[float]


class NavError(Exception):
    """Base error for nav-graph operations."""


class CrossSystemError(NavError):
    """Raised when a within-system operation spans two systems."""


def _sub(a: Vec, b: Vec):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def distance(a: Vec, b: Vec) -> float:
    """Euclidean distance between two helio points (km)."""
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _segment_blocks(center: Vec, a: Vec, b: Vec, radius: float,
                    margin: float) -> bool:
    """True iff sphere(center, radius+margin) intersects the *interior* of
    segment a->b (closest approach strictly between the endpoints)."""
    ab = _sub(b, a)
    l2 = _dot(ab, ab)
    if l2 == 0.0:
        return False  # degenerate segment (a == b): nothing to occlude
    # Parameter of the foot of the perpendicular from center onto line a->b.
    t = _dot(_sub(center, a), ab) / l2
    if not (0.0 < t < 1.0):
        # Nearest approach is at an endpoint -> this is the origin/destination
        # body, not a body sitting between A and B. Not an obstruction.
        return False
    foot = (a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2])
    return distance(center, foot) < (radius + margin)


def segment_blocked_by(a_helio: Vec, b_helio: Vec,
                       bodies: Iterable, margin_km: float = 0.0) -> list:
    """Return the list of bodies that block segment a->b.

    ``bodies`` is an iterable of ``(center_helio, radius_km)`` or
    ``(center_helio, radius_km, key)`` tuples; whatever each item is, it is
    returned as-is for any body that blocks. Useful for picking which bodies'
    OMs to consider when routing around an obstruction.
    """
    out = []
    for body in bodies:
        center, radius = body[0], body[1]
        if _segment_blocks(center, a_helio, b_helio, radius, margin_km):
            out.append(body)
    return out


def line_of_sight_clear(a_helio: Vec, b_helio: Vec,
                        bodies: Iterable, margin_km: float = 0.0) -> bool:
    """True if no body blocks the straight segment a->b.

    ``bodies`` is an iterable of ``(center_helio, radius_km[, ...])``.
    """
    for body in bodies:
        center, radius = body[0], body[1]
        if _segment_blocks(center, a_helio, b_helio, radius, margin_km):
            return False
    return True


class NavGraph:
    """In-memory view of ``nav_points`` for one patch, with LoS + OM routing.

    Build once and reuse; loading touches the DB, everything else is pure
    geometry over the cached rows.
    """

    # kinds that physically obstruct a QT segment
    _BODY_KINDS = ("star", "planet", "moon")

    def __init__(self, conn: sqlite3.Connection, patch: str,
                 exclude_key_prefixes: Sequence[str] = EXCLUDE_KEY_PREFIXES):
        self.patch = patch
        self._nodes: dict[str, dict] = {}
        self._oms_by_body: dict[str, list[str]] = {}
        self._bodies_by_system: dict[str, list[dict]] = {}
        self._system_links: dict[str, dict[str, str]] = {}
        self._load(conn, patch, tuple(exclude_key_prefixes))

    # -- loading --
    def _load(self, conn: sqlite3.Connection, patch: str,
              exclude_prefixes: tuple) -> None:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT location_uuid, display_name, location_key, kind,
                      parent_uuid, parent_body_uuid,
                      helio_x_km, helio_y_km, helio_z_km,
                      body_radius_km, arrival_radius_km, om_radius_km,
                      lat_deg, lon_deg
                 FROM nav_points WHERE patch_version = ?""", (patch,)
        ).fetchall()

        raw: dict[str, sqlite3.Row] = {}
        for r in rows:
            key = r["location_key"] or ""
            if any(key.startswith(p) for p in exclude_prefixes):
                continue
            raw[r["location_uuid"]] = r

        def helio(r):
            x, y, z = r["helio_x_km"], r["helio_y_km"], r["helio_z_km"]
            if x is None or y is None or z is None:
                return None
            return (x, y, z)

        # System resolution by walking parent_uuid up to a star.
        sys_cache: dict[str, Optional[str]] = {}

        def system_of(uuid):
            if uuid in sys_cache:
                return sys_cache[uuid]
            chain, cur, seen = [], uuid, set()
            result = None
            while cur and cur not in seen and cur in raw:
                seen.add(cur)
                chain.append(cur)
                node = raw[cur]
                if node["kind"] == "star":
                    result = _STAR_KEY_TO_SYSTEM.get(node["location_key"])
                    break
                cur = node["parent_uuid"]
            for u in chain:
                sys_cache[u] = result
            return result

        # Body resolution: a node's obstructing body / orbital home.
        def body_of(r):
            if r["kind"] in self._BODY_KINDS:
                return r["location_uuid"]
            pb = r["parent_body_uuid"]
            if pb and pb in raw and raw[pb]["kind"] in self._BODY_KINDS:
                return pb
            pu = r["parent_uuid"]
            if pu and pu in raw and raw[pu]["kind"] in self._BODY_KINDS:
                return pu
            return None

        for uuid, r in raw.items():
            self._nodes[uuid] = {
                "uuid": uuid,
                "name": r["display_name"] or r["location_key"],
                "key": r["location_key"],
                "kind": r["kind"],
                "helio": helio(r),
                "system": system_of(uuid),
                "body_uuid": body_of(r),
                "body_radius_km": r["body_radius_km"],
                "arrival_radius_km": r["arrival_radius_km"],
                "om_radius_km": r["om_radius_km"],
                "lat_deg": r["lat_deg"],
            }

        for uuid, n in self._nodes.items():
            if n["kind"] == "om" and n["body_uuid"] and n["helio"]:
                self._oms_by_body.setdefault(n["body_uuid"], []).append(uuid)
            if n["kind"] in self._BODY_KINDS and n["helio"] \
                    and n["body_radius_km"] is not None:
                self._bodies_by_system.setdefault(n["system"], []).append(n)

        # Inter-system jump-point links. A clean cross-system JP has a two-token
        # key "JumpPoint_<ownSystem>_<targetSystem>" and a helio position;
        # suffixed variants (_Wrecksite / _SecurityDock) and links to systems we
        # don't model are skipped. _system_links[S][T] = JP node in S leading to T.
        present = {n["system"] for n in self._nodes.values() if n["system"]}
        for uuid, n in self._nodes.items():
            if n["kind"] != "jumppoint" or not n["helio"]:
                continue
            key = n["key"] or ""
            if not key.startswith("JumpPoint_"):
                continue
            toks = key[len("JumpPoint_"):].split("_")
            if len(toks) != 2:
                continue
            own, target = toks
            if n["system"] != own or target not in present or target == own:
                continue
            self._system_links.setdefault(own, {})[target] = uuid

    # -- accessors --
    def node(self, uuid: str) -> dict:
        try:
            return self._nodes[uuid]
        except KeyError:
            raise NavError(f"unknown nav point: {uuid}")

    def helio(self, uuid: str):
        h = self.node(uuid)["helio"]
        if h is None:
            raise NavError(f"nav point {uuid} has no helio position")
        return h

    def system_of(self, uuid: str):
        return self.node(uuid)["system"]

    def body_of(self, uuid: str):
        return self.node(uuid)["body_uuid"]

    def oms_of_body(self, body_uuid: str) -> list:
        return list(self._oms_by_body.get(body_uuid, []))

    def is_planetside(self, uuid: str) -> bool:
        """True if the nav point sits on a body's surface — i.e. a sublight climb
        is needed before QT. Surface kinds are outposts and landing zones;
        stations/OMs/body/Lagrange nodes are space (orbital stations carry a
        lat/lon too, so latitude alone is not a surface signal)."""
        return self.node(uuid)["kind"] in ("outpost", "landing_zone")

    def nearest_om(self, body_uuid: str, toward_uuid: str):
        """The OM of ``body_uuid`` closest to ``toward_uuid`` — the natural
        QT engage/exit marker on the target-facing hemisphere. None if the body
        has no OMs."""
        oms = self._oms_by_body.get(body_uuid)
        if not oms:
            return None
        target = self.helio(toward_uuid)
        return min(oms, key=lambda u: distance(self.helio(u), target))

    def bodies_in_system(self, system: str) -> list:
        return list(self._bodies_by_system.get(system, []))

    # -- inter-system jump points --
    def jump_link(self, from_system: str, to_system: str):
        """The ``(origin_side_jp, dest_side_jp)`` nav points for a direct
        system->system jump, or None if the two systems aren't directly linked
        (both sides must be positioned JP nodes we model)."""
        out = self._system_links.get(from_system, {}).get(to_system)
        inb = self._system_links.get(to_system, {}).get(from_system)
        return (out, inb) if out and inb else None

    def system_jump_path(self, from_system: str, to_system: str):
        """Shortest list of systems linking from_system to to_system over the
        jump-point network (e.g. ['Stanton','Pyro','Nyx']), or None if
        unreachable. Returns [S] when both are the same system S."""
        if from_system == to_system:
            return [from_system] if from_system else None
        if not from_system or not to_system:
            return None
        adj: dict[str, set] = {}
        for a, targets in self._system_links.items():
            for b in targets:
                if self.jump_link(a, b):  # require both directions to exist
                    adj.setdefault(a, set()).add(b)
        queue = deque([[from_system]])
        seen = {from_system}
        while queue:
            path = queue.popleft()
            if path[-1] == to_system:
                return path
            for nb in adj.get(path[-1], ()):
                if nb not in seen:
                    seen.add(nb)
                    queue.append(path + [nb])
        return None

    def _obstruction_bodies(self, system: str):
        """(center, radius, uuid) tuples for the system's obstructing bodies."""
        return [(b["helio"], b["body_radius_km"], b["uuid"])
                for b in self._bodies_by_system.get(system, [])]

    # -- line of sight --
    def los_clear(self, a_uuid: str, b_uuid: str, margin_km: float = 0.0,
                  ignore_bodies: Sequence[str] = ()) -> bool:
        """Clear LoS between two nav points (must be in the same system)."""
        sa, sb = self.system_of(a_uuid), self.system_of(b_uuid)
        if sa != sb:
            raise CrossSystemError(
                f"{a_uuid} ({sa}) and {b_uuid} ({sb}) are in different systems"
            )
        bodies = [b for b in self._obstruction_bodies(sa)
                  if b[2] not in ignore_bodies]
        return line_of_sight_clear(self.helio(a_uuid), self.helio(b_uuid),
                                   bodies, margin_km)

    # -- routing around obstructions via OMs --
    def required_oms(self, origin_uuid: str, dest_uuid: str,
                     margin_km: float = 0.0) -> list:
        """OM hops needed to get clear LoS from origin to dest, within a system.

        Returns an ordered list of OM ``location_uuid``s to visit between
        origin and dest (empty if the direct jump already has LoS). Raises
        ``CrossSystemError`` for inter-system pairs (use a jump point) and
        ``NavError`` if no OM route can be found.
        """
        sa, sb = self.system_of(origin_uuid), self.system_of(dest_uuid)
        if sa != sb:
            raise CrossSystemError(
                "required_oms is within-system only; route via a jump point"
            )
        a_h, b_h = self.helio(origin_uuid), self.helio(dest_uuid)
        bodies = self._obstruction_bodies(sa)

        if line_of_sight_clear(a_h, b_h, bodies, margin_km):
            return []

        # Candidate OMs: those of the origin & dest bodies, plus the bodies
        # that actually block the direct segment. (Most routes clear with one
        # of these; we fall back to all system OMs if not.)
        blockers = segment_blocked_by(a_h, b_h, bodies, margin_km)
        seed_bodies = {self.body_of(origin_uuid), self.body_of(dest_uuid)}
        seed_bodies |= {b[2] for b in blockers}
        seed_bodies.discard(None)

        cand = set()
        for body in seed_bodies:
            cand.update(self.oms_of_body(body))

        path = self._shortest_visibility_path(
            origin_uuid, dest_uuid, cand, bodies, margin_km)
        if path is None:
            # Fall back to every OM in the system.
            all_oms = [u for u, n in self._nodes.items()
                       if n["kind"] == "om" and n["system"] == sa and n["helio"]]
            path = self._shortest_visibility_path(
                origin_uuid, dest_uuid, set(all_oms), bodies, margin_km)
        if path is None:
            raise NavError(
                f"no clear OM route from {origin_uuid} to {dest_uuid}")
        return path  # intermediate OMs only (origin/dest stripped)

    def route_waypoints(self, origin_uuid: str, dest_uuid: str,
                        margin_km: float = 0.0) -> list:
        """Full ordered waypoint list [origin, *oms, dest] for a within-system
        leg — the input Layer 3 turns into QT legs."""
        return [origin_uuid, *self.required_oms(origin_uuid, dest_uuid, margin_km),
                dest_uuid]

    # -- visibility-graph Dijkstra --
    def _shortest_visibility_path(self, origin: str, dest: str,
                                  candidates: set, bodies, margin_km: float):
        """Shortest (by distance) origin->dest path whose every hop has clear
        LoS, using ``candidates`` as the only allowed intermediate nodes.
        Returns the intermediate node list (excluding origin/dest), or None."""
        nodes = {origin, dest, *candidates}
        nodes.discard(None)
        pos = {u: self.helio(u) for u in nodes}

        def clear(u, v):
            return line_of_sight_clear(pos[u], pos[v], bodies, margin_km)

        # Dijkstra.
        dist_to = {origin: 0.0}
        prev: dict[str, Optional[str]] = {origin: None}
        pq = [(0.0, origin)]
        visited = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            if u == dest:
                break
            for v in nodes:
                if v in visited or v == u:
                    continue
                if not clear(u, v):
                    continue
                nd = d + distance(pos[u], pos[v])
                if nd < dist_to.get(v, math.inf):
                    dist_to[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))

        if dest not in prev:
            return None
        # Reconstruct, then strip origin/dest.
        chain, cur = [], dest
        while cur is not None:
            chain.append(cur)
            cur = prev[cur]
        chain.reverse()
        return chain[1:-1]


def build_nav_graph(db_path: str, patch: Optional[str] = None) -> "NavGraph":
    """Convenience: open ``db_path`` and build a NavGraph (latest patch if
    ``patch`` is None)."""
    conn = sqlite3.connect(db_path)
    try:
        if patch is None:
            patch = conn.execute(
                "SELECT patch_version FROM nav_points "
                "ORDER BY patch_version DESC LIMIT 1").fetchone()[0]
        return NavGraph(conn, patch)
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════
# LAYER 3 — ROUTING (stitches sublight + QT legs into a full trip)
# ════════════════════════════════════════════════════════════════════════
#
#     departure sublight -> QT hop(s) routed around obstructions -> arrival sublight
#
# Cross-system trips route to the origin-side jump point, traverse the tunnel
# (flat JUMP_POINT_TRAVERSAL_S, negligible distance, fuel TBD), then continue
# from the destination-side jump point — repeated per system hop, so multi-hop
# routes (e.g. Stanton->Pyro->Nyx) work. CrossSystemError is raised only when no
# jump route between the systems is known.

# Curated atmosphere table (scaffold; refine later).
# CURATED, NOT DATAMINED. The per-body surface-pressure / scale-height needed to
# compute the real QT-engage altitude (pressure <= 0.4 atm) lives in planet
# object containers and isn't in the flat records yet. Until a datamining pass
# recovers it, this set marks the bodies whose atmosphere is dense enough to
# matter for a planetside departure/arrival. Keyed by nav_points.location_key.
ATMOSPHERIC_BODY_KEYS = {
    # Stanton
    "Stanton1",   # Hurston
    "Stanton2",   # Crusader (gas giant)
    "Stanton3",   # ArcCorp
    "Stanton4",   # microTech
    # Pyro
    "Pyro1",      # Pyro I
    "Pyro3",      # Bloom
    "Pyro5",      # Pyro V (gas giant)
}


def has_atmosphere(location_key) -> bool:
    """Whether a body (by location_key) has a QT-gating atmosphere. Curated;
    unknown / not listed -> treated as airless."""
    return location_key in ATMOSPHERIC_BODY_KEYS


def sublight_transit_cost(distance_m: float, ship: Mapping,
                          atmosphere: Optional[Mapping] = None) -> dict:
    """Surface<->space sublight portion of a trip (climb to QT engage / descend
    on arrival).

    PLACEHOLDER: returns a fixed per-class time (see SUBLIGHT_*_S at the top of
    this file); it does NOT yet use distance, ship thrust/mass, or a real
    speed-cap curve. ``atmosphere`` is a dict like ``{"has_atmosphere": bool}``
    (or None -> airless). Returns ``{"time_s": float, "placeholder": True}``.
    """
    atmo = bool(atmosphere and atmosphere.get("has_atmosphere"))
    t = SUBLIGHT_ATMOSPHERIC_S if atmo else SUBLIGHT_AIRLESS_S
    return {"time_s": t, "placeholder": True}


def _atmo_for_body(nav: NavGraph, body_uuid):
    if not body_uuid:
        return None
    key = nav.node(body_uuid)["key"]
    return {"has_atmosphere": has_atmosphere(key)}


def _sublight_leg(nav, a_uuid, b_uuid, ship, body_uuid, phase) -> dict:
    d_m = distance(nav.helio(a_uuid), nav.helio(b_uuid)) * 1000.0
    cost = sublight_transit_cost(d_m, ship, _atmo_for_body(nav, body_uuid))
    return {
        "kind": "sublight", "phase": phase,
        "from": a_uuid, "to": b_uuid,
        "from_name": nav.node(a_uuid)["name"], "to_name": nav.node(b_uuid)["name"],
        "distance_m": d_m,
        "time_s": cost["time_s"], "fuel_scu": 0.0,
        "placeholder": cost.get("placeholder", False),
    }


def _qt_leg(nav, a_uuid, b_uuid, qd) -> dict:
    d_m = distance(nav.helio(a_uuid), nav.helio(b_uuid)) * 1000.0
    cost = qt_leg_cost(d_m, qd)
    return {
        "kind": "qt",
        "from": a_uuid, "to": b_uuid,
        "from_name": nav.node(a_uuid)["name"], "to_name": nav.node(b_uuid)["name"],
        "distance_m": d_m,
        "time_s": cost["time_s"], "travel_time_s": cost["travel_time_s"],
        "fuel_scu": cost["fuel_scu"], "reaches_vmax": cost["reaches_vmax"],
        "phases": cost["phases"],
    }


def _jump_leg(nav, a_uuid, b_uuid) -> dict:
    """Jump-tunnel traversal between the two sides of a jump point. Flat
    placeholder time (JUMP_POINT_TRAVERSAL_S); distance is negligible and the
    tunnel's fuel cost is not modelled yet (fuel_pending)."""
    return {
        "kind": "jump",
        "from": a_uuid, "to": b_uuid,
        "from_name": nav.node(a_uuid)["name"], "to_name": nav.node(b_uuid)["name"],
        "distance_m": 0.0,
        "time_s": JUMP_POINT_TRAVERSAL_S,
        "fuel_scu": 0.0,          # tunnel fuel TBD — revisit
        "fuel_pending": True,
    }


def _plan_within_system(origin_uuid, dest_uuid, ship, qd, nav) -> list:
    """Sub-legs for an origin->dest pair already known to be in one system:
    departure sublight -> QT hop(s) routed around obstructions -> arrival
    sublight."""
    if origin_uuid == dest_uuid:
        return []

    a_body, b_body = nav.body_of(origin_uuid), nav.body_of(dest_uuid)
    a_side, b_side = nav.is_planetside(origin_uuid), nav.is_planetside(dest_uuid)

    # Two surface points on the same body: a single suborbital sublight hop.
    if a_body and a_body == b_body and a_side and b_side:
        return [_sublight_leg(nav, origin_uuid, dest_uuid, ship, a_body, "suborbital")]

    # QT engage / exit markers: climb to an OM if planetside, else use the
    # point itself (station / OM / body / Lagrange / JP already in usable space).
    a_qt = origin_uuid
    if a_side and a_body and nav.oms_of_body(a_body):
        a_qt = nav.nearest_om(a_body, dest_uuid) or origin_uuid
    b_qt = dest_uuid
    if b_side and b_body and nav.oms_of_body(b_body):
        b_qt = nav.nearest_om(b_body, a_qt) or dest_uuid

    legs = []
    if a_qt != origin_uuid:
        legs.append(_sublight_leg(nav, origin_uuid, a_qt, ship, a_body, "departure"))

    # QT hops between the two markers, routed around obstructions.
    waypoints = nav.route_waypoints(a_qt, b_qt)
    for i in range(len(waypoints) - 1):
        legs.append(_qt_leg(nav, waypoints[i], waypoints[i + 1], qd))

    if b_qt != dest_uuid:
        legs.append(_sublight_leg(nav, b_qt, dest_uuid, ship, b_body, "arrival"))

    return legs


def plan_leg(origin_uuid: str, dest_uuid: str, ship: Mapping, qd: Mapping,
             nav: NavGraph) -> list:
    """Decompose one origin->dest leg into ordered sub-legs.

    Within a system: departure sublight -> QT hop(s) -> arrival sublight.
    Across systems: for each system hop, route to the origin-side jump point,
    traverse the tunnel (``_jump_leg``), then continue from the destination-side
    jump point. Raises CrossSystemError if no jump route links the systems.
    """
    if origin_uuid == dest_uuid:
        return []

    so, sd = nav.system_of(origin_uuid), nav.system_of(dest_uuid)
    if so == sd:
        return _plan_within_system(origin_uuid, dest_uuid, ship, qd, nav)

    syspath = nav.system_jump_path(so, sd)
    if not syspath or len(syspath) < 2:
        raise CrossSystemError(f"no jump route from system {so} to {sd}")

    legs = []
    cur = origin_uuid
    for i in range(len(syspath) - 1):
        link = nav.jump_link(syspath[i], syspath[i + 1])
        if not link:
            raise CrossSystemError(
                f"no jump link {syspath[i]} -> {syspath[i + 1]}")
        jp_out, jp_in = link
        legs.extend(_plan_within_system(cur, jp_out, ship, qd, nav))
        legs.append(_jump_leg(nav, jp_out, jp_in))
        cur = jp_in
    legs.extend(_plan_within_system(cur, dest_uuid, ship, qd, nav))
    return legs


def plan_route(segments: Sequence, ship: Mapping, qd: Mapping,
               nav: NavGraph) -> dict:
    """Plan a full route across a sequence of origin->dest segments.

    ``segments`` is an iterable of ``(origin_uuid, dest_uuid)`` pairs or dicts
    with ``from``/``to`` keys. Returns the flat, ordered ``legs`` plus summary
    aggregates::

        {
          "legs": [ <sub-leg>, ... ],
          "warnings": [str, ...],

          # counts
          "segments_requested": int,     # origin->dest pairs passed in
          "segments_planned": int,       # of those, how many were routed
                                         # (only unroutable pairs are skipped -> warning)
          "qt_jumps": int,               # number of QT hops (spool/cooldown cycles)
          "jump_count": int,             # number of jump-point (inter-system) traversals

          # time (seconds)
          "qt_time_s": float,            # QT legs incl. spool/calibration/cooldown
          "qt_flight_time_s": float,     # QT flight only (accel+cruise+decel)
          "atmospheric_time_s": float,   # sublight departure/arrival/suborbital
          "jump_time_s": float,          # jump-tunnel traversals (flat constant)
          "total_time_s": float,         # ALL legs (qt + atmospheric + jump)

          # distance (metres)
          "qt_distance_m": float,
          "atmospheric_distance_m": float,
          "total_distance_m": float,     # qt + atmospheric (+ jump, ~0)

          # fuel
          "total_fuel_scu": float,       # QT fuel; jump-tunnel fuel not yet modelled
        }

    Note: total_time_s / total_distance_m sum *every* leg, so qt_* +
    atmospheric_* + jump_* reconcile to the totals. Atmospheric time is still
    the placeholder constant (not derived from atmospheric_distance_m), jump
    time is the flat JUMP_POINT_TRAVERSAL_S, and jump-tunnel fuel is not yet
    modelled (so total_fuel_scu is QT fuel only).
    """
    legs, warnings = [], []
    requested = 0
    for seg in segments:
        requested += 1
        if isinstance(seg, Mapping):
            o, d = seg.get("from"), seg.get("to")
        else:
            o, d = seg[0], seg[1]
        try:
            legs.extend(plan_leg(o, d, ship, qd, nav))
        except CrossSystemError as e:
            warnings.append(str(e))

    qt = [l for l in legs if l["kind"] == "qt"]
    atmo = [l for l in legs if l["kind"] == "sublight"]
    jumps = [l for l in legs if l["kind"] == "jump"]

    return {
        "legs": legs,
        "warnings": warnings,

        "segments_requested": requested,
        "segments_planned": requested - len(warnings),
        "qt_jumps": len(qt),
        "jump_count": len(jumps),

        "qt_time_s": sum(l["time_s"] for l in qt),
        "qt_flight_time_s": sum(l.get("travel_time_s", 0.0) for l in qt),
        "atmospheric_time_s": sum(l["time_s"] for l in atmo),
        "jump_time_s": sum(l["time_s"] for l in jumps),
        "total_time_s": sum(l["time_s"] for l in legs),

        "qt_distance_m": sum(l["distance_m"] for l in qt),
        "atmospheric_distance_m": sum(l["distance_m"] for l in atmo),
        "total_distance_m": sum(l["distance_m"] for l in legs),

        "total_fuel_scu": sum(l.get("fuel_scu", 0.0) for l in legs),
    }
