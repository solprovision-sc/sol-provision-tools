// Background star shell around origin. Lives forever; not regenerated per
// system switch. Two layers — a dense field of faint pinpoints plus a sparse
// scatter of brighter, larger stars — give the sky depth instead of reading as
// flat noise.

import * as THREE from 'three';

function shell(count, rMin, rMax, brightMin, size, opacity) {
  const pos = new Float32Array(count * 3);
  const col = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const r  = rMin + Math.random() * (rMax - rMin);
    const th = Math.random() * Math.PI * 2;
    const ph = Math.acos(2 * Math.random() - 1);
    pos[i*3]   = r * Math.sin(ph) * Math.cos(th);
    pos[i*3+1] = r * Math.sin(ph) * Math.sin(th);
    pos[i*3+2] = r * Math.cos(ph);
    // Slight blue→warm-white spread, scaled by a per-star brightness.
    const t = Math.random();
    const b = brightMin + (1 - brightMin) * Math.random();
    col[i*3]   = (0.72 + t * 0.28) * b;
    col[i*3+1] = (0.80 + t * 0.20) * b;
    col[i*3+2] = (0.90 + t * 0.10) * b;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('color',    new THREE.BufferAttribute(col, 3));
  return new THREE.Points(geo, new THREE.PointsMaterial({
    size, vertexColors: true, transparent: true, opacity,
    sizeAttenuation: false,
  }));
}

export function makeStarfield() {
  const group = new THREE.Group();
  // Dense faint field.
  group.add(shell(5200, 11000, 26000, 0.35, 1.3, 0.6));
  // Sparse bright field — larger, whiter, the "named" stars.
  group.add(shell(420,  12000, 24000, 0.7,  2.6, 0.9));
  return group;
}
