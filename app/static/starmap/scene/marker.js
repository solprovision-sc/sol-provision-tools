// ═══════════════════════════════════════════════════════════════════
//  "You are here" marker — a billboarded reticle + label dropped at the
//  player's set/triangulated position. Managed outside world.sceneObjects
//  (it isn't a body and must survive per-frame rebuilds of nothing, but is
//  cleared explicitly on system switch / clear). One marker at a time.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';
import { makeLabel } from '../util/label.js';

const MARKER_COLOR = '#ffffff';
const RETICLE_PX   = 128;

function reticleTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = RETICLE_PX;
  const ctx = c.getContext('2d');
  const m = RETICLE_PX / 2, R = RETICLE_PX * 0.34;
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = 3;
  ctx.globalAlpha = 0.95;
  // ring
  ctx.beginPath(); ctx.arc(m, m, R, 0, Math.PI * 2); ctx.stroke();
  // crosshair ticks (gap in the middle)
  const g = R * 0.45, t = R * 1.25;
  ctx.beginPath();
  ctx.moveTo(m, m - g); ctx.lineTo(m, m - t);
  ctx.moveTo(m, m + g); ctx.lineTo(m, m + t);
  ctx.moveTo(m - g, m); ctx.lineTo(m - t, m);
  ctx.moveTo(m + g, m); ctx.lineTo(m + t, m);
  ctx.stroke();
  // center dot
  ctx.fillStyle = '#ffffff';
  ctx.beginPath(); ctx.arc(m, m, 3.5, 0, Math.PI * 2); ctx.fill();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

export function setMarker(world, scenePos, label) {
  clearMarker(world);

  const group = new THREE.Group();
  group.position.copy(scenePos);

  const tex = reticleTexture();
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: tex, color: MARKER_COLOR, transparent: true,
    depthTest: false, depthWrite: false,
  }));
  const base = 30;
  sprite.scale.set(base, base, 1);
  group.add(sprite);

  const lab = makeLabel(label || 'YOU ARE HERE', MARKER_COLOR, 16);
  lab.position.set(0, -base * 0.9, 0);
  lab.renderOrder = 999;
  group.add(lab);

  world.scene.add(group);
  world.marker = { group, sprite, lab, base, t: 0 };
}

export function clearMarker(world) {
  const m = world.marker;
  if (!m) return;
  world.scene.remove(m.group);
  m.group.traverse(o => {
    if (o.material) {
      if (o.material.map) o.material.map.dispose();
      o.material.dispose();
    }
    if (o.geometry) o.geometry.dispose();
  });
  world.marker = null;
}

// Gentle pulse + constant on-screen size, driven from the loop.
export function updateMarker(world, dt) {
  const m = world.marker;
  if (!m) return;
  m.t += dt;
  const pulse = 1 + 0.12 * Math.sin(m.t * 0.004);
  const d = world.camera.position.distanceTo(m.group.position);
  const s = Math.max(0.5, Math.min(3.0, d / 900)) * pulse;
  m.sprite.scale.set(m.base * s, m.base * s, 1);
  m.group.children.forEach(ch => ch.quaternion.copy(world.camera.quaternion));
}
