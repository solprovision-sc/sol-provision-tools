// ═══════════════════════════════════════════════════════════════════
//  2D trilateration on the ecliptic plane.
//
//  All catalogued bodies sit at z=0 (verified in the DB), so a player's
//  position is solved in (x,y) from ≥3 distance measurements; the sign of
//  z (height above/below the plane) is unresolvable from coplanar
//  references and is reported, not placed.
//
//  Method: subtract the first reference's sphere equation from the others
//  to linearise, then least-squares solve the (over-)determined system.
//  Pure math, km in / km out — no Three.js, no DOM.
// ═══════════════════════════════════════════════════════════════════

// refs: [{ x, y, d }]  (x,y = body helio km; d = measured distance km)
// returns { ok, x, y, zAbs, residualKm, quality, warnings[] }
export function trilaterate2D(refs) {
  const warnings = [];
  if (!refs || refs.length < 3) {
    return { ok: false, warnings: ['Need at least 3 references.'] };
  }

  // Linearise around refs[0]: 2(xi-x0)x + 2(yi-y0)y = (xi²-x0²)+(yi²-y0²)-(di²-d0²)
  const x0 = refs[0].x, y0 = refs[0].y, d0 = refs[0].d;
  const A = [], b = [];
  for (let i = 1; i < refs.length; i++) {
    const { x, y, d } = refs[i];
    A.push([2 * (x - x0), 2 * (y - y0)]);
    b.push((x*x - x0*x0) + (y*y - y0*y0) - (d*d - d0*d0));
  }

  // Normal equations: (AᵀA) v = Aᵀb  → 2×2 solve.
  let a11 = 0, a12 = 0, a22 = 0, bx = 0, by = 0;
  for (let i = 0; i < A.length; i++) {
    a11 += A[i][0] * A[i][0];
    a12 += A[i][0] * A[i][1];
    a22 += A[i][1] * A[i][1];
    bx  += A[i][0] * b[i];
    by  += A[i][1] * b[i];
  }
  const det = a11 * a22 - a12 * a12;

  // Geometry quality: collinear references → near-singular normal matrix.
  const scale = a11 + a22;             // ~ Σ|Δ|² , a magnitude reference
  const cond  = scale > 0 ? Math.abs(det) / (scale * scale) : 0;
  if (cond < 1e-4) {
    warnings.push('References are nearly collinear — pick bodies that are more spread out for an accurate fix.');
  }
  if (Math.abs(det) < 1e-9) {
    return { ok: false, warnings: [...warnings, 'Could not solve — references are degenerate (collinear or coincident).'] };
  }

  const x = ( a22 * bx - a12 * by) / det;
  const y = (-a12 * bx + a11 * by) / det;

  // z² from the first reference; average the implied height over all refs.
  let z2sum = 0, z2n = 0;
  for (const r of refs) {
    const planar = (x - r.x) ** 2 + (y - r.y) ** 2;
    z2sum += (r.d * r.d - planar);
    z2n++;
  }
  const z2 = z2sum / z2n;
  const zAbs = z2 > 0 ? Math.sqrt(z2) : 0;

  // Residual: how well the solved point reproduces the measured distances.
  let sse = 0;
  for (const r of refs) {
    const dHat = Math.sqrt((x - r.x) ** 2 + (y - r.y) ** 2 + Math.max(0, z2));
    sse += (dHat - r.d) ** 2;
  }
  const residualKm = Math.sqrt(sse / refs.length);

  if (z2 < 0) {
    warnings.push('Distances are inconsistent (no common point) — check the values and units.');
  }

  // Quality from residual relative to the mean measured distance.
  const meanD = refs.reduce((s, r) => s + r.d, 0) / refs.length;
  const relErr = meanD > 0 ? residualKm / meanD : 1;
  let quality = 'good';
  if (relErr > 0.10 || cond < 1e-4) quality = 'poor';
  else if (relErr > 0.03)           quality = 'fair';

  return { ok: true, x, y, zAbs, residualKm, quality, warnings };
}
