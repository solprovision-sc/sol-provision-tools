"""Route optimizer — sequences cargo pickups/dropoffs to minimize a chosen cost.

This sits *on top of* the quantum-travel engine (``helpers.quantum_travel``),
which it treats purely as a cost oracle: it asks "what does leg A->B cost?" and
never re-derives nav/QT physics itself. All the cargo domain logic lives here:

  * precedence  — every pickup must be visited before its own dropoff,
  * capacity    — onboard cargo never exceeds the ship's ``cargo_scu``,
  * objective   — minimise total "time", "distance", or "fuel".

Problem class: a single-vehicle **Pickup-and-Delivery Problem** (a capacitated
TSP with precedence constraints), starting from an optional origin (the ship's
current location). It is NOT a DFS/BFS path search — that lives one level down
in the QT engine. Here we choose the best *ordering* of stops.

Solver: for the small stop counts a cargo stack produces, an exact Held-Karp
subset DP (with precedence + capacity baked into state feasibility). Above a
node-count threshold it falls back to a precedence/capacity-aware greedy
nearest-feasible heuristic.

Input obligations (one per cargo move; map straight from missions[].legs[])::

    [{"id": <any>, "pickup": <location_uuid>, "dropoff": <location_uuid>,
      "scu": <float>}, ...]
"""

from __future__ import annotations

import math
from typing import Mapping, Optional, Sequence

from helpers.quantum_travel import plan_leg, plan_route, CrossSystemError

__all__ = ["optimize_stack", "OptimizeError", "OBJECTIVES"]

OBJECTIVES = ("time", "distance", "fuel")
_METRIC_KEY = {"time": "time_s", "distance": "distance_m", "fuel": "fuel_scu"}

# Exact DP is used while the number of task-nodes (2 * obligations) is <= this;
# beyond it, the greedy heuristic takes over.
_EXACT_MAX_NODES = 14


class OptimizeError(ValueError):
    """Bad optimizer input (unknown objective, malformed obligation, ...)."""


# --- cost oracle (memoised plan_leg over the chosen objective) --------------

def _leg_cost(origin, dest, ship, qd, nav, objective, cache) -> float:
    """Objective cost of going origin->dest, via the QT engine. inf if the
    pair can't be routed (e.g. no jump link). Memoised per optimize_stack call."""
    if origin is None or origin == dest:
        return 0.0
    key = (origin, dest)
    if key in cache:
        return cache[key]
    try:
        legs = plan_leg(origin, dest, ship, qd, nav)
        mk = _METRIC_KEY[objective]
        val = sum(l.get(mk, 0.0) for l in legs)
    except CrossSystemError:
        val = math.inf
    cache[key] = val
    return val


# --- exact solver: Held-Karp DP with precedence + capacity ------------------

def _solve_exact(N, K, uuid_of, scu_of, cap, origin, ship, qd, nav, objective, cache):
    """Return (ordered task-node list, objective value) or (None, inf) if no
    feasible ordering exists. Task-node t: even=pickup, odd=dropoff; obligation
    i has pickup 2i, dropoff 2i+1."""
    FULL = (1 << N) - 1
    INF = math.inf

    def load(mask):
        tot = 0.0
        for i in range(K):
            if (mask >> (2 * i)) & 1:
                tot += scu_of(i)
            if (mask >> (2 * i + 1)) & 1:
                tot -= scu_of(i)
        return tot

    def feasible(mask):
        for i in range(K):
            if (mask >> (2 * i + 1)) & 1 and not (mask >> (2 * i)) & 1:
                return False  # dropped before picked up
        return cap is None or load(mask) <= cap + 1e-9

    def cost(a_node, b_node):
        a = origin if a_node == -1 else uuid_of(a_node)
        return _leg_cost(a, uuid_of(b_node), ship, qd, nav, objective, cache)

    dp = {}          # (mask, last) -> best cost
    par = {}         # (mask, last) -> previous task-node (or -1 for depot)
    for j in range(0, N, 2):          # first stop must be a pickup
        m = 1 << j
        if feasible(m):
            dp[(m, j)] = cost(-1, j)
            par[(m, j)] = -1

    # Adding a bit always increases the mask integer, so ascending mask order
    # guarantees dp[mask] is final before it seeds larger masks.
    for mask in range(1, 1 << N):
        if not feasible(mask):
            continue
        for last in range(N):
            if not (mask >> last) & 1:
                continue
            cur = dp.get((mask, last))
            if cur is None or cur == INF:
                continue
            for j in range(N):
                if (mask >> j) & 1:
                    continue
                if j % 2 == 1 and not (mask >> (2 * (j // 2))) & 1:
                    continue  # can't drop before pickup
                nmask = mask | (1 << j)
                if not feasible(nmask):
                    continue
                c = cost(last, j)
                if c == INF:
                    continue
                nc = cur + c
                if nc < dp.get((nmask, j), INF):
                    dp[(nmask, j)] = nc
                    par[(nmask, j)] = last

    best, best_last = INF, None
    for last in range(N):
        v = dp.get((FULL, last), INF)
        if v < best:
            best, best_last = v, last
    if best_last is None or best == INF:
        return None, INF

    order, mask, last = [], FULL, best_last
    while last != -1:
        order.append(last)
        prev = par[(mask, last)]
        mask ^= (1 << last)
        last = prev
    order.reverse()
    return order, best


# --- greedy fallback: nearest feasible next stop ----------------------------

def _solve_greedy(N, K, uuid_of, scu_of, cap, origin, ship, qd, nav, objective, cache):
    visited, order, cur, total = 0, [], origin, 0.0

    def load(mask):
        tot = 0.0
        for i in range(K):
            if (mask >> (2 * i)) & 1:
                tot += scu_of(i)
            if (mask >> (2 * i + 1)) & 1:
                tot -= scu_of(i)
        return tot

    while bin(visited).count("1") < N:
        best_j, best_c = None, math.inf
        for j in range(N):
            if (visited >> j) & 1:
                continue
            if j % 2 == 1 and not (visited >> (2 * (j // 2))) & 1:
                continue
            nmask = visited | (1 << j)
            if cap is not None and load(nmask) > cap + 1e-9:
                continue
            c = _leg_cost(cur, uuid_of(j), ship, qd, nav, objective, cache)
            if c < best_c:
                best_c, best_j = c, j
        if best_j is None or best_c == math.inf:
            return None, math.inf
        visited |= (1 << best_j)
        order.append(best_j)
        cur = uuid_of(best_j)
        total += best_c
    return order, total


# --- public entry point -----------------------------------------------------

def optimize_stack(obligations: Sequence[Mapping], ship: Mapping, qd: Mapping,
                   nav, *, objective: str = "time",
                   origin_uuid: Optional[str] = None) -> dict:
    """Find the cheapest feasible visiting order for a set of cargo obligations.

    obligations: ``[{"id", "pickup", "dropoff", "scu"}, ...]`` (location_uuids).
    ship: mapping; ``cargo_scu`` caps onboard load (missing/0 -> unlimited).
    qd / nav: the QT-engine inputs (drive row, NavGraph).
    objective: "time" | "distance" | "fuel".
    origin_uuid: where the ship starts (cost of reaching the first stop is
        counted from here). If None, the trip is costed from the first stop on.

    Returns::

        {
          "objective": str, "feasible": bool, "method": "exact"|"greedy",
          "order": [location_uuid, ...],     # incl. origin first when given
          "stops": [{node, name, action, obligation_id, scu, load_after_scu}, ...],
          "route": <plan_route output for the ordered stops, or None>,
          "value": float|None,               # objective total of the route
          "warnings": [str, ...],
        }
    """
    if objective not in OBJECTIVES:
        raise OptimizeError(f"objective must be one of {OBJECTIVES}")

    tasks = []  # (pickup, dropoff, scu, id)
    for idx, o in enumerate(obligations):
        p, d = o.get("pickup"), o.get("dropoff")
        if not p or not d:
            raise OptimizeError(f"obligation {idx} needs 'pickup' and 'dropoff'")
        nav.node(p); nav.node(d)  # validate (raises NavError if unknown)
        tasks.append((p, d, float(o.get("scu") or 0.0), o.get("id", idx)))
    if origin_uuid is not None:
        nav.node(origin_uuid)

    K = len(tasks)
    cap = None
    if ship and ship.get("cargo_scu"):
        cap = float(ship["cargo_scu"])

    def _result(order_uuids, route, value, feasible, method, warnings, stops):
        return {"objective": objective, "feasible": feasible, "method": method,
                "order": order_uuids, "stops": stops, "route": route,
                "value": value, "warnings": warnings}

    if K == 0:
        return _result([origin_uuid] if origin_uuid else [],
                       plan_route([], ship, qd, nav), 0.0, True, "trivial", [], [])

    # A single obligation heavier than the hold can never be carried.
    if cap is not None:
        over = [t[3] for t in tasks if t[2] > cap + 1e-9]
        if over:
            return _result([], None, None, False, "exact",
                           [f"obligation {o} exceeds cargo capacity {cap} SCU" for o in over], [])

    N = 2 * K
    uuid_of = lambda t: tasks[t // 2][0] if t % 2 == 0 else tasks[t // 2][1]
    scu_of = lambda i: tasks[i][2]
    cache: dict = {}

    solver = _solve_exact if N <= _EXACT_MAX_NODES else _solve_greedy
    method = "exact" if N <= _EXACT_MAX_NODES else "greedy"
    order_nodes, _val = solver(N, K, uuid_of, scu_of, cap, origin_uuid,
                               ship, qd, nav, objective, cache)

    if order_nodes is None:
        return _result([], None, None, False, method,
                       ["no feasible route (capacity or unreachable systems)"], [])

    # Build the ordered stop list (with running load) and the detailed route.
    stops, load = [], 0.0
    for t in order_nodes:
        i = t // 2
        if t % 2 == 0:
            action, load = "pickup", load + scu_of(i)
        else:
            action, load = "dropoff", load - scu_of(i)
        node = uuid_of(t)
        stops.append({"node": node, "name": nav.node(node)["name"],
                      "action": action, "obligation_id": tasks[i][3],
                      "scu": scu_of(i), "load_after_scu": load})

    order_uuids = ([origin_uuid] if origin_uuid else []) + [s["node"] for s in stops]
    segments = list(zip(order_uuids, order_uuids[1:]))
    route = plan_route(segments, ship, qd, nav)
    value = {"time": route["total_time_s"],
             "distance": route["total_distance_m"],
             "fuel": route["total_fuel_scu"]}[objective]

    return _result(order_uuids, route, value, True, method,
                   route.get("warnings", []), stops)
