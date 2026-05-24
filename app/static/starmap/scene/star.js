// Star body. Lightweight: a bright unlit sphere + a soft back-side halo + the
// system's point light + a faint ambient so unlit faces aren't pitch black.
//
// Real bloom (post-FX) does the heavy lifting of "this looks luminous."

import * as THREE from 'three';
import { hexToInt } from '../util/color.js';
import { makeLabel } from '../util/label.js';
import { makeStarHalo } from './primitives.js';

const STAR_RENDER_RADIUS = 60;

export function addStar(world, starData, systemConfig) {
  const r = STAR_RENDER_RADIUS;
  const col = hexToInt(systemConfig.starColor);

  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(r, 32, 32),
    new THREE.MeshBasicMaterial({ color: col }),
  );
  mesh.userData = { bodyId: starData.id };
  world.scene.add(mesh);
  world.sceneObjects.push(mesh);
  world.meshes.push(mesh);
  world.bodyIndex[starData.id].mesh         = mesh;
  world.bodyIndex[starData.id].renderRadius = r;

  const halo = makeStarHalo(r, systemConfig.starColor);
  world.scene.add(halo);
  world.sceneObjects.push(halo);

  const light = new THREE.PointLight(systemConfig.starGlow, 1.5, 6000);
  world.scene.add(light);
  world.sceneObjects.push(light);

  const ambient = new THREE.AmbientLight(0x111111, 0.9);
  world.scene.add(ambient);
  world.sceneObjects.push(ambient);

  const label = makeLabel(starData.name, systemConfig.starColor, 22);
  label.position.set(r + 12, r + 10, 0);
  label.userData = { type: 'label', bodyType: 'STAR' };
  world.scene.add(label);
  world.sceneObjects.push(label);
  world.labelSprites.push(label);
}
