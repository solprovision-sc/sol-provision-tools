// Moon builder. Smaller than planets, orbit ring is dimmer.

import * as THREE from 'three';
import { hexToInt } from '../util/color.js';
import { makeLabel } from '../util/label.js';
import { makeOrbitRing } from './primitives.js';
import { moonAuToScene } from '../util/scale.js';
import { loadBodyTextures, applyTexturesToMesh } from '../data/textures.js';
import { addCloudLayer } from './clouds.js';

const MOON_R = 8;

export function addMoon(world, body, systemConfig) {
  const bd       = world.bodyIndex[body.id];
  const parentBd = world.bodyIndex[body.parent];
  const col      = hexToInt(body.color);
  const orbitR   = moonAuToScene(body.dist, world.moonRefAU);

  const ring = makeOrbitRing(orbitR, systemConfig.moonOrbitColor, 0.18, 64);
  ring.position.copy(parentBd.position);
  ring.userData = { type: 'orbit', bodyType: 'orbits', parentId: body.parent };
  world.scene.add(ring);
  world.sceneObjects.push(ring);
  world.orbitObjects.push(ring);
  bd.orbitRing = ring;

  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(MOON_R, 32, 32),
    new THREE.MeshStandardMaterial({ color: col, roughness: 0.9 }),
  );
  mesh.position.copy(bd.position);
  mesh.userData = { bodyId: body.id };
  world.scene.add(mesh);
  world.sceneObjects.push(mesh);
  world.meshes.push(mesh);
  bd.mesh         = mesh;
  bd.renderRadius = MOON_R;

  const label = makeLabel(body.name, systemConfig.moonLabelColor, 15);
  label.position.set(bd.position.x + 7, bd.position.y + 7, bd.position.z);
  label.scale.set(55, 10, 1);
  label.userData = { type: 'label', bodyType: 'SATELLITE', followId: body.id, offX: 7, offY: 7 };
  world.scene.add(label);
  world.sceneObjects.push(label);
  world.labelSprites.push(label);

  loadBodyTextures(body.id).then(tex => {
    applyTexturesToMesh(mesh, tex);
    if (tex.clouds) addCloudLayer(world, body, mesh, MOON_R, tex.clouds);
  });
}
