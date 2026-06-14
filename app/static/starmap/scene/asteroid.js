// Asteroid belt — flat-ish ring of points around the star at the belt's AU.
// P3+ might upgrade this to instanced low-poly rocks; for now points work fine.

import * as THREE from 'three';
import { hexToInt } from '../util/color.js';

export function addAsteroidBelt(world, body, systemConfig) {
  const bd = world.bodyIndex[body.id];
  const radius = bd.orbitRadiusScene;
  const color  = hexToInt(body.color);

  // Tight, dense band: a narrow radial width and a near-flat vertical extent,
  // with a high particle count so the belt reads as a pronounced ring.
  const count = 1000, spread = 0.03;
  const pos = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const a = Math.random() * Math.PI * 2;
    const r = radius + (Math.random() - 0.5) * radius * spread;
    pos[i*3]   = Math.cos(a) * r;
    pos[i*3+1] = (Math.random() - 0.5) * 6;
    pos[i*3+2] = Math.sin(a) * r;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));

  const belt = new THREE.Points(geo, new THREE.PointsMaterial({
    color, size: 1.8, transparent: true, opacity: 0.6,
  }));
  belt.userData = { bodyId: body.id, bodyType: 'ASTEROID_BELT' };
  world.scene.add(belt);
  world.sceneObjects.push(belt);
  world.meshes.push(belt);
  bd.mesh = belt;
}
