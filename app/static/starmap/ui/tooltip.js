// ═══════════════════════════════════════════════════════════════════
//  Picker — raycast against world.meshes, show tooltip on hover,
//  call onSelect(bodyId) on click. Owns no DOM state outside #tooltip.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

const raycaster = new THREE.Raycaster();
// Slightly enlarged Points threshold so asteroid-belt picking works.
raycaster.params.Points = { threshold: 10 };
const mouse = new THREE.Vector2();

export function initPicker(container, camera, world, onSelect) {
  const tip = document.getElementById('tooltip');

  container.addEventListener('mousemove', e => {
    mouse.x =  (e.clientX / window.innerWidth)  * 2 - 1;
    mouse.y = -(e.clientY / window.innerHeight) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(world.meshes, false);
    if (hits.length > 0 && hits[0].object.userData.bodyId) {
      const b = world.bodyIndex[hits[0].object.userData.bodyId];
      if (b && b.name) {
        tip.textContent  = b.name.toUpperCase();
        tip.style.left   = (e.clientX + 14) + 'px';
        tip.style.top    = (e.clientY - 8)  + 'px';
        tip.classList.add('show');
        document.body.style.cursor = 'pointer';
        return;
      }
    }
    tip.classList.remove('show');
    document.body.style.cursor = 'crosshair';
  });

  container.addEventListener('click', e => {
    mouse.x =  (e.clientX / window.innerWidth)  * 2 - 1;
    mouse.y = -(e.clientY / window.innerHeight) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(world.meshes, false);
    if (hits.length > 0 && hits[0].object.userData.bodyId) {
      onSelect(hits[0].object.userData.bodyId);
    }
  });
}
