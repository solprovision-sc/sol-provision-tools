// ═══════════════════════════════════════════════════════════════════
//  Lagrange points — the five L1–L5 equilibrium points of each planet's
//  star-relative orbit. Sourced from the DB POIs (kind 'lagrange'), NOT
//  from systems.js, and positioned by real helio through world.helioToScene
//  (they sit at star-direct scale, on/near the planet's orbit ring).
//
//  Rendered as small billboarded diamond markers + an "L#" label, toggled
//  by the LAGRANGE display filter. The matching rest-stop stations (the
//  parent-less "XXX-L# … Station" rows) are skipped — we show the points.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';
import { makeLabel } from '../util/label.js';

const MARKER_PX     = 64;
const DEFAULT_COLOR = '#9fd4e6';

// Per-marker texture (clearScene disposes each material's .map, so no sharing).
function diamondTexture(hex) {
  const c = document.createElement('canvas');
  c.width = c.height = MARKER_PX;
  const ctx = c.getContext('2d');
  const m = MARKER_PX / 2, R = MARKER_PX * 0.34;
  ctx.strokeStyle = hex; ctx.lineWidth = 3; ctx.globalAlpha = 0.95;
  ctx.beginPath();
  ctx.moveTo(m, m - R); ctx.lineTo(m + R, m); ctx.lineTo(m, m + R); ctx.lineTo(m - R, m); ctx.closePath();
  ctx.stroke();
  ctx.globalAlpha = 0.9; ctx.fillStyle = hex;
  ctx.beginPath(); ctx.arc(m, m, 2.5, 0, Math.PI * 2); ctx.fill();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// A bare L-point: kind 'lagrange', has a parent planet, and isn't a station.
function isLagrangePoint(p) {
  if (p.kind !== 'lagrange' || !p.helio) return false;
  const parent = p.parent_name;
  if (!parent || parent === '—' || parent === '-') return false;
  return !/station/i.test(p.name || '');
}

// "HUR L4" → "HUR-L4" — the parent shorthand already lives in the POI name.
function lpLabel(name) {
  return (name || 'L').trim().replace(/\s+/g, '-');
}

export function addLagrangePoints(world, coords, systemConfig) {
  if (!coords || !Array.isArray(coords.pois) || !world.helioToScene) return;
  const color  = systemConfig.lagrangeColor || DEFAULT_COLOR;
  const colInt = parseInt(color.replace('#', '0x'), 16);

  for (const p of coords.pois) {
    if (!isLagrangePoint(p)) continue;
    const pos = world.helioToScene(p.helio[0], p.helio[1]);

    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: diamondTexture(color), color: colInt, transparent: true, depthWrite: false,
    }));
    sprite.scale.set(14, 14, 1);
    sprite.position.copy(pos);
    sprite.userData = { bodyType: 'LAGRANGE' };
    world.scene.add(sprite);
    world.sceneObjects.push(sprite);

    const label = makeLabel(lpLabel(p.name), color, 14);
    label.scale.set(54, 11, 1);
    label.position.set(pos.x + 11, pos.y + 8, pos.z);
    label.userData = { type: 'label', bodyType: 'LAGRANGE' };
    world.scene.add(label);
    world.sceneObjects.push(label);
    world.labelSprites.push(label);
  }
}
