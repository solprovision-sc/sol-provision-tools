// Planet builder. Handles both:
//   • PLANET orbiting a STAR (large orbit ring on the star plane)
//   • PLANET orbiting a PLANET (captured — e.g. Pyro IV around Pyro V)
//
// Material starts procedural (flat colour) and gets upgraded in place once
// loadBodyTextures() resolves. If the body has no texture entry in the
// manifest, the procedural material stays.

import * as THREE from 'three';
import { hexToInt } from '../util/color.js';
import { makeLabel } from '../util/label.js';
import { makeOrbitRing } from './primitives.js';
import { loadBodyTextures, applyTexturesToMesh } from '../data/textures.js';
import { addCloudLayer } from './clouds.js';
import { addAtmosphere } from './atmosphere.js';
import { PLANET_R, CAPTURED_PLANET_R } from './sizes.js';

export function addPlanet(world, body, systemConfig) {
  const bd  = world.bodyIndex[body.id];
  const pr  = PLANET_R[body.subtype] || PLANET_R.default;
  const col = hexToInt(body.color);

  const ring = makeOrbitRing(bd.orbitRadiusScene, systemConfig.orbitColor, 0.22);
  ring.userData = { type: 'orbit', bodyType: 'orbits' };
  world.scene.add(ring);
  world.sceneObjects.push(ring);
  world.orbitObjects.push(ring);

  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(pr, 64, 64),
    new THREE.MeshStandardMaterial({ color: col, roughness: 0.82, metalness: 0.08 }),
  );
  mesh.position.copy(bd.position);
  mesh.userData = { bodyId: body.id };
  world.scene.add(mesh);
  world.sceneObjects.push(mesh);
  world.meshes.push(mesh);
  bd.mesh         = mesh;
  bd.renderRadius = pr;

  addAtmosphere(world, mesh, pr, col, body.subtype);

  const label = makeLabel(body.name, systemConfig.labelColor, 19);
  label.position.set(bd.position.x + pr + 10, bd.position.y + pr + 8, bd.position.z);
  label.userData = { type: 'label', bodyType: 'PLANET', followId: body.id, offX: pr + 10, offY: pr + 8 };
  world.scene.add(label);
  world.sceneObjects.push(label);
  world.labelSprites.push(label);

  loadBodyTextures(body.id).then(tex => {
    applyTexturesToMesh(mesh, tex);
    if (tex.clouds) addCloudLayer(world, body, mesh, pr, tex.clouds);
  });
}

export function addCapturedPlanet(world, body, systemConfig) {
  const bd       = world.bodyIndex[body.id];
  const parentBd = world.bodyIndex[body.parent];
  const pr       = CAPTURED_PLANET_R;
  const col      = hexToInt(body.color);

  const ring = makeOrbitRing(bd.orbitRadiusScene, systemConfig.moonOrbitColor, 0.3, 64);
  ring.position.copy(parentBd.position);
  ring.userData = { type: 'orbit', bodyType: 'orbits', parentId: body.parent };
  world.scene.add(ring);
  world.sceneObjects.push(ring);
  world.orbitObjects.push(ring);
  bd.orbitRing = ring;

  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(pr, 48, 48),
    new THREE.MeshStandardMaterial({ color: col, roughness: 0.85 }),
  );
  mesh.position.copy(bd.position);
  mesh.userData = { bodyId: body.id };
  world.scene.add(mesh);
  world.sceneObjects.push(mesh);
  world.meshes.push(mesh);
  bd.mesh         = mesh;
  bd.renderRadius = pr;

  addAtmosphere(world, mesh, pr, col, body.subtype);

  const label = makeLabel(body.name, systemConfig.labelColor, 17);
  label.position.set(bd.position.x + pr + 8, bd.position.y + pr + 6, bd.position.z);
  label.scale.set(65, 12, 1);
  label.userData = { type: 'label', bodyType: 'PLANET', followId: body.id, offX: pr + 8, offY: pr + 6 };
  world.scene.add(label);
  world.sceneObjects.push(label);
  world.labelSprites.push(label);

  loadBodyTextures(body.id).then(tex => {
    applyTexturesToMesh(mesh, tex);
    if (tex.clouds) addCloudLayer(world, body, mesh, pr, tex.clouds);
  });
}
