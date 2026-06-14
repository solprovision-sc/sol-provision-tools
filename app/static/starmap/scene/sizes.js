// ═══════════════════════════════════════════════════════════════════
//  Body render radii (scene units) — shared by the body builders AND the
//  placement engine (so moons can be seated just outside their planet).
//
//  Deliberately compact, to read like CIG's Arkmap: planets are small dots
//  on clean orbit rings and moons hug their planet as a tight cluster you
//  resolve by zooming in — not giant spheres that swallow neighbouring
//  orbits. Texture detail comes back on fly-to (the camera moves in close),
//  CIG-style, rather than from bloating the overview.
//
//  These are the primary knobs for the overview's sense of scale.
// ═══════════════════════════════════════════════════════════════════

export const PLANET_R = {
  'Super-Earth':       6.6,
  'Gas Giant':        10.8,
  'Terrestrial Rocky': 5.4,
  'Smog Planet':       5.4,
  'Ice Giant':         8.4,
  'Protoplanet':       4.2,
  default:             6,
};

// Captured planets (e.g. Pyro IV around Pyro V) — small enough not to warrant
// per-subtype tuning.
export const CAPTURED_PLANET_R = 4.8;

export const MOON_R = 2.625;

export function planetRadiusFor(body) {
  return (body && PLANET_R[body.subtype]) || PLANET_R.default;
}
