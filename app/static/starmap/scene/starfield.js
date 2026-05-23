// Background point cloud — 5000 stars on a 10,000-25,000 unit shell around origin.
// Lives forever; not regenerated per system switch.

import * as THREE from 'three';

export function makeStarfield(count = 5000) {
  const pos = new Float32Array(count * 3);
  const col = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const r  = 10000 + Math.random() * 15000;
    const th = Math.random() * Math.PI * 2;
    const ph = Math.acos(2 * Math.random() - 1);
    pos[i*3]   = r * Math.sin(ph) * Math.cos(th);
    pos[i*3+1] = r * Math.sin(ph) * Math.sin(th);
    pos[i*3+2] = r * Math.cos(ph);
    const t = Math.random();
    col[i*3]   = 0.7 + t * 0.3;
    col[i*3+1] = 0.8 + t * 0.2;
    col[i*3+2] = 0.9 + t * 0.1;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('color',    new THREE.BufferAttribute(col, 3));
  return new THREE.Points(geo, new THREE.PointsMaterial({
    size: 1.3, vertexColors: true, transparent: true, opacity: 0.65,
  }));
}
