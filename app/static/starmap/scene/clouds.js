// ═══════════════════════════════════════════════════════════════════
//  Cloud overlay sphere.
//
//  Built slightly larger than the body so clouds float above the surface.
//  The cloud texture from the extractor is a single-channel "cloud opacity
//  mask" (white = dense, black = clear), used here as alphaMap on a white
//  sphere — no extra colour texture needed.
//
//  Drives a slow rotation per frame via world.cloudMeshes.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

// Multipliers vs. planet radius. Two layers separated subtly is the standard
// "from space" trick; one layer is enough to read as clouds, two adds depth
// without volumetric cost.
const CLOUD_RADIUS_MULT = 1.025;
const ROT_SPEED         = 0.00002; // radians per ms — barely visible over a few seconds

export function addCloudLayer(world, body, planetMesh, planetRadius, cloudTexture) {
  const geo = new THREE.SphereGeometry(planetRadius * CLOUD_RADIUS_MULT, 64, 64);
  const mat = new THREE.MeshLambertMaterial({
    color:        0xffffff,
    alphaMap:     cloudTexture,
    transparent:  true,
    depthWrite:   false,
    side:         THREE.FrontSide,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.position.copy(planetMesh.position);
  mesh.userData = { type: 'clouds', followId: body.id };
  world.scene.add(mesh);
  world.sceneObjects.push(mesh);
  if (!world.cloudMeshes) world.cloudMeshes = [];
  world.cloudMeshes.push(mesh);
}

// Called from the orchestrator loop. Cheap — just a rotation increment.
export function rotateClouds(world, dt) {
  if (!world.cloudMeshes) return;
  for (const c of world.cloudMeshes) {
    c.rotation.y += ROT_SPEED * dt;
    // If the body is orbit-animating, keep clouds following the planet.
    if (c.userData.followId != null) {
      const bd = world.bodyIndex[c.userData.followId];
      if (bd && bd.mesh) c.position.copy(bd.mesh.position);
    }
  }
}
