// ═══════════════════════════════════════════════════════════════════
//  Real heliocentric coordinates from the DB, per system.
//
//  Fetches /api/starmap/<system>/bodies — the authoritative game geometry
//  (km, star at origin). The map renders body POSITIONS from this; the
//  curated display metadata (colour/subtype/description) still comes from
//  data/systems.js, merged by name. Cached per system; null on failure so
//  the caller can fall back to the systems.js approximation.
// ═══════════════════════════════════════════════════════════════════

const cache = new Map();

export function loadSystemCoords(systemKey) {
  if (cache.has(systemKey)) return cache.get(systemKey);
  const p = fetch(`/api/starmap/${systemKey}/bodies`)
    .then(r => (r.ok ? r.json() : null))
    .catch(err => { console.warn('starmap: coords fetch failed', err); return null; });
  cache.set(systemKey, p);
  return p;
}
