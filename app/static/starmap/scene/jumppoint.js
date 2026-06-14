// Jump point — wireframe octahedron marker.
// P4 will swap the octahedron for CIG's actual starmap jump-gate CGFm model.

import * as THREE from 'three';
import { hexToInt } from '../util/color.js';
import { makeLabel } from '../util/label.js';

const MARKER_R = 6;

export function addJumpPoint(world, body, systemConfig) {
  const bd  = world.bodyIndex[body.id];
  const col = hexToInt(systemConfig.jumpColor);

  const marker = new THREE.Mesh(
    new THREE.OctahedronGeometry(MARKER_R, 0),
    new THREE.MeshBasicMaterial({ color: col, wireframe: true, transparent: true, opacity: 0.85 }),
  );
  marker.position.copy(bd.position);
  marker.userData = { bodyId: body.id };
  world.scene.add(marker);
  world.sceneObjects.push(marker);
  world.meshes.push(marker);
  bd.mesh         = marker;
  bd.renderRadius = MARKER_R;

  const label = makeLabel(body.name, systemConfig.jumpColor, 15);
  label.position.set(bd.position.x + 10, bd.position.y + 10, bd.position.z);
  label.scale.set(70, 11, 1);
  label.userData = { type: 'label', bodyType: 'JUMPPOINT', followId: body.id, offX: 10, offY: 10 };
  world.scene.add(label);
  world.sceneObjects.push(label);
  world.labelSprites.push(label);
}
