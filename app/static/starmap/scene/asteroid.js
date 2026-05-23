// Asteroid belt — flat-ish ring of points around the star at the belt's AU.
// P3+ might upgrade this to instanced low-poly rocks; for now points work fine.

import * as THREE from 'three';
import { hexToInt } from '../util/color.js';
import { auToScene } from '../util/scale.js';

export function addAsteroidBelt(world, body, systemConfig) {
  const bd = world.bodyIndex[body.id];
  const radius = auToScene(body.dist, world.refAU);
  const color  = hexToInt(body.color);

  const count = 500, spread = 0.06;
  const pos = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const a = Math.random() * Math.PI * 2;
    const r = radius + (Math.random() - 0.5) * radius * spread;
    pos[i*3]   = Math.cos(a) * r;
    pos[i*3+1] = (Math.random() - 0.5) * 10;
    pos[i*3+2] = Math.sin(a) * r;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));

  const belt = new THREE.Points(geo, new THREE.PointsMaterial({
    color, size: 1.8, transparent: true, opacity: 0.45,
  }));
  belt.userData = { bodyId: body.id, bodyType: 'ASTEROID_BELT' };
  world.scene.add(belt);
  world.sceneObjects.push(belt);
  world.meshes.push(belt);
  bd.mesh = belt;
}
