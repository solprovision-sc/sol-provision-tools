// ═══════════════════════════════════════════════════════════════════
//  Spherical camera controller.
//  Left-drag: rotate    Right-drag: pan    Wheel: zoom    F: reset
//
//  Movement is critically-damped via lerp, not snapped — so dragging
//  produces inertia without us needing a separate velocity model.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

const DEFAULT_VIEW = { theta: 0.3, phi: 1.1, r: 1050 };
const R_MIN = 60;
const R_MAX = 12000;

export function createCameraController(container) {
  const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.1, 80000);
  camera.position.set(0, 400, 900);

  let spherical       = { ...DEFAULT_VIEW };
  let targetSpherical = { ...DEFAULT_VIEW };
  const pan       = new THREE.Vector3();
  const targetPan = new THREE.Vector3();
  let isDragging = false, isRightDrag = false;
  let prevX = 0, prevY = 0;

  container.addEventListener('mousedown', e => {
    isDragging = true; isRightDrag = e.button === 2;
    prevX = e.clientX; prevY = e.clientY;
  });
  container.addEventListener('contextmenu', e => e.preventDefault());
  window.addEventListener('mouseup', () => { isDragging = false; });
  window.addEventListener('mousemove', e => {
    if (!isDragging) return;
    const dx = e.clientX - prevX, dy = e.clientY - prevY;
    if (isRightDrag) {
      const right = new THREE.Vector3();
      const up    = new THREE.Vector3(0, 1, 0);
      right.crossVectors(camera.position.clone().sub(pan).normalize(), up).normalize();
      const sp = spherical.r * 0.0005;
      targetPan.addScaledVector(right, dx * sp);
      targetPan.addScaledVector(up,    dy * sp);
    } else {
      targetSpherical.theta += dx * 0.005;
      targetSpherical.phi   -= dy * 0.005;
    }
    prevX = e.clientX; prevY = e.clientY;
  });
  container.addEventListener('wheel', e => {
    targetSpherical.r *= 1 + e.deltaY * 0.001;
  }, { passive: true });
  window.addEventListener('keydown', e => {
    if (e.key === 'f' || e.key === 'F') reset();
  });

  function reset() {
    targetSpherical = { ...DEFAULT_VIEW };
    targetPan.set(0, 0, 0);
  }

  // Smoothly fly toward a target point at a given distance. The animation is
  // implicit: we just set the lerp targets and the existing update() converges.
  // Optional theta/phi override the angle; otherwise we keep the current view.
  function flyTo({ position, distance, theta, phi }) {
    targetPan.copy(position);
    if (distance != null) targetSpherical.r     = distance;
    if (theta    != null) targetSpherical.theta = theta;
    if (phi      != null) targetSpherical.phi   = phi;
  }

  function update() {
    spherical.theta += (targetSpherical.theta - spherical.theta) * 0.08;
    spherical.phi   += (targetSpherical.phi   - spherical.phi)   * 0.08;
    spherical.r     += (targetSpherical.r     - spherical.r)     * 0.08;
    pan.lerp(targetPan, 0.08);

    spherical.phi = Math.max(0.05, Math.min(Math.PI - 0.05, spherical.phi));
    spherical.r   = Math.max(R_MIN, Math.min(R_MAX, spherical.r));

    camera.position.set(
      pan.x + spherical.r * Math.sin(spherical.phi) * Math.cos(spherical.theta),
      pan.y + spherical.r * Math.cos(spherical.phi),
      pan.z + spherical.r * Math.sin(spherical.phi) * Math.sin(spherical.theta),
    );
    camera.lookAt(pan);
  }

  return { camera, update, reset, flyTo };
}
