// ═══════════════════════════════════════════════════════════════════
//  Ecliptic reference grid — the "tactical floor" under the system.
//
//  A polar grid (concentric rings + radial spokes) on the y=0 plane,
//  centred on the star. Drawn with additive blending and per-vertex
//  colour that fades from the accent hue near the star to black at the
//  rim — so the grid dissolves into space instead of ending on a hard
//  circle. Purely decorative; not raycast-eligible, not filtered.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

const RINGS         = 7;    // concentric circles
const SPOKES        = 24;   // radial lines (every 15°)
const RING_SEGMENTS = 120;  // smoothness of each circle
const SPOKE_STEPS   = 24;   // subdivisions per spoke (so it can fade along length)

// brightness falloff from centre (1) to rim (0). Quadratic keeps the inner
// rings legible while the outer ones melt away.
function falloff(t) {
  const k = Math.max(0, 1 - t * t);
  return k * 0.15;   // overall ceiling — the grid is a barely-there trace
}

export function makeEclipticGrid(outerRadius, accentHex = '#3fe0ff') {
  const accent = new THREE.Color(accentHex);
  const positions = [];
  const colors    = [];

  const push = (x, z, b) => {
    positions.push(x, 0, z);
    colors.push(accent.r * b, accent.g * b, accent.b * b);
  };

  // ── Concentric rings ──
  for (let ri = 1; ri <= RINGS; ri++) {
    const r = (ri / RINGS) * outerRadius;
    const b = falloff(ri / RINGS);
    for (let s = 0; s < RING_SEGMENTS; s++) {
      const a0 = (s     / RING_SEGMENTS) * Math.PI * 2;
      const a1 = ((s+1) / RING_SEGMENTS) * Math.PI * 2;
      push(Math.cos(a0) * r, Math.sin(a0) * r, b);
      push(Math.cos(a1) * r, Math.sin(a1) * r, b);
    }
  }

  // ── Radial spokes ──
  for (let si = 0; si < SPOKES; si++) {
    const a = (si / SPOKES) * Math.PI * 2;
    const ca = Math.cos(a), sa = Math.sin(a);
    for (let st = 0; st < SPOKE_STEPS; st++) {
      const r0 = (st     / SPOKE_STEPS) * outerRadius;
      const r1 = ((st+1) / SPOKE_STEPS) * outerRadius;
      push(ca * r0, sa * r0, falloff(st     / SPOKE_STEPS));
      push(ca * r1, sa * r1, falloff((st+1) / SPOKE_STEPS));
    }
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geo.setAttribute('color',    new THREE.Float32BufferAttribute(colors,    3));

  const grid = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent:  true,
    opacity:      0.9,
    blending:     THREE.AdditiveBlending,
    depthWrite:   false,
  }));
  grid.userData = { type: 'ecliptic' };
  grid.renderOrder = -1;   // behind bodies so it never haloes a planet face
  return grid;
}
