// Reusable scene-graph primitives shared by the body-type builders.

import * as THREE from 'three';

export function makeOrbitRing(radius, color = 0x004422, opacity = 0.3, segments = 128) {
  const pts = new Float32Array((segments + 1) * 3);
  for (let i = 0; i <= segments; i++) {
    const a = (i / segments) * Math.PI * 2;
    pts[i*3]   = Math.cos(a) * radius;
    pts[i*3+1] = 0;
    pts[i*3+2] = Math.sin(a) * radius;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pts, 3));
  return new THREE.Line(geo, new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false }));
}

// Thin halo so the star reads as luminous even when the bloom pass is dialed low.
export function makeStarHalo(radius, colorHex, opacity = 0.12) {
  const geo = new THREE.SphereGeometry(radius * 1.6, 24, 24);
  const mat = new THREE.MeshBasicMaterial({
    color: parseInt(colorHex.replace('#', '0x'), 16),
    transparent: true,
    opacity,
    side: THREE.BackSide,
    depthWrite: false,
  });
  return new THREE.Mesh(geo, mat);
}
