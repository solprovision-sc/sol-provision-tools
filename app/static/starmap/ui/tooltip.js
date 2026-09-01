// ═══════════════════════════════════════════════════════════════════
//  Picker — raycast against world.meshes, show tooltip on hover,
//  call onSelect(bodyId) on click. Owns no DOM state outside #tooltip.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

const raycaster = new THREE.Raycaster();
// Slightly enlarged Points threshold so asteroid-belt picking works.
raycaster.params.Points = { threshold: 10 };
const mouse = new THREE.Vector2();

// NDC must be measured against the canvas container's rect, not the window —
// the map is a block inset from the page edges, so window-relative coordinates
// would offset every pick by the header + sidebar dimensions.
function setMouseFrom(e, container) {
  const r = container.getBoundingClientRect();
  mouse.x =  ((e.clientX - r.left) / r.width)  * 2 - 1;
  mouse.y = -((e.clientY - r.top)  / r.height) * 2 + 1;
}

export function initPicker(container, camera, world, onSelect) {
  const tip = document.getElementById('tooltip');

  container.addEventListener('mousemove', e => {
    setMouseFrom(e, container);
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(world.meshes, false);
    if (hits.length > 0 && hits[0].object.userData.bodyId) {
      const b = world.bodyIndex[hits[0].object.userData.bodyId];
      if (b && b.name) {
        tip.textContent  = b.name.toUpperCase();
        tip.style.left   = (e.clientX + 14) + 'px';
        tip.style.top    = (e.clientY - 8)  + 'px';
        tip.classList.add('show');
        // Scope the cursor to the map: the page around it is normal chrome now.
        container.style.cursor = 'pointer';
        return;
      }
    }
    tip.classList.remove('show');
    container.style.cursor = 'default';
  });

  container.addEventListener('mouseleave', () => {
    tip.classList.remove('show');
    container.style.cursor = 'default';
  });

  container.addEventListener('click', e => {
    setMouseFrom(e, container);
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(world.meshes, false);
    if (hits.length > 0 && hits[0].object.userData.bodyId) {
      onSelect(hits[0].object.userData.bodyId);
    }
  });
}
