// Scene-space scale.
// The map is logarithmic-ish (sqrt) so closer-in bodies don't crowd the star.
// Numbers are chosen for visual readability, not physical accuracy.

// Radial compression of star-direct orbits. 0.5 = sqrt (heavily compressed,
// outer orbits bunch up); 1.0 = linear (scene radius ∝ true distance, so e.g.
// ArcCorp reads ~2.25× Hurston as in reality). This is the orbit-spacing knob.
export const ORBIT_SCALE_EXP = 1.0;
// 1050 mirrors SCENE_BASE_R in starmap.js (the outermost star-direct scene radius).
export function auToScene(au, refAU)     { return Math.pow(au / refAU, ORBIT_SCALE_EXP) * 1050; }

// Legacy moon exaggeration — still used for captured planets (Pyro IV), which
// want a wider parent-relative band than a moon.
export function moonAuToScene(au, refAU) { return Math.sqrt(au / refAU) * 160; }

// Moon placement: seat moons in a tight band hugging the planet — just outside
// its surface (MOON_BAND_GAP) and sqrt-spread by real distance over a small
// span (MOON_BAND_SPREAD). Anchored to the planet's render radius, NOT its
// orbit, so a moon system reads as a compact cluster (CIG-style) and never
// bleeds across a neighbouring planet's ring. These two are the moon-spacing
// knobs.
const MOON_BAND_GAP    = 5;    // clearance from planet surface to the nearest moon
const MOON_BAND_SPREAD = 14;   // scene span from nearest to farthest moon
export function moonOrbitRadius(planetR, dist, refDist) {
  const t = refDist > 0 ? Math.sqrt(Math.max(dist, 0) / refDist) : 0;
  return planetR + MOON_BAND_GAP + t * MOON_BAND_SPREAD;
}
