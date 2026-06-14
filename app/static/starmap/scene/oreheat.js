// ═══════════════════════════════════════════════════════════════════
//  ORE CONCENTRATION HEAT MAP — flat heat disks dropped at the POIs
//  where each ore has been recorded, tinted by concentration.
//
//  Data: data/ore_heatmap.js (generated from the org mining spreadsheet).
//  Each location carries an `anchor` we resolve against the live scene:
//    body / belt -> the body's current scene position
//    lagrange    -> the L-point's real helio through world.helioToScene
//    none        -> can't be placed yet (skipped)
//
//  Colour is a CONSTANT red→green scale keyed to the dataset's global max
//  percentage, so a disk's colour means the same thing regardless of which
//  ores are toggled on. When several selected ores share a location their
//  disks stack concentrically (highest concentration = largest disk) so each
//  stays readable.
//
//  Overlay is rebuilt on toggle / selection change / system switch — it is a
//  static snapshot, so it does not follow bodies during orbital motion.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';
import { ORE_HEATMAP } from '../data/ore_heatmap.js';

const BASE_RADIUS  = 58;    // outermost disk radius (scene units) — visible but not swamping
const RADIUS_STEP  = 0.30;  // each stacked (lower-%) ore disk shrinks by this fraction
const MIN_FACTOR   = 0.34;  // floor so deep stacks don't vanish
const CENTER_ALPHA = 0.72;  // opacity at the disk centre (fades to 0 at the rim)
const TEX_PX       = 128;

// Quick lookups built once from the static dataset.
const MAX_PCT   = ORE_HEATMAP.meta.maxPct || 100;
export const ORE_LIST = ORE_HEATMAP.meta.ores;

// pct → THREE.Color on the fixed red(0) → yellow → green(max) ramp.
export function pctColor(pct) {
  const t = Math.max(0, Math.min(1, pct / MAX_PCT));
  const c = new THREE.Color();
  c.setHSL(t * (120 / 360), 0.85, 0.5);   // hue 0°(red) → 120°(green)
  return c;
}

// Soft radial-falloff alpha sprite, tinted per-disk via material.color. One
// texture is shared by every disk in a build and disposed in clearOreHeat.
function makeFalloffTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = TEX_PX;
  const ctx = c.getContext('2d');
  const m = TEX_PX / 2;
  const g = ctx.createRadialGradient(m, m, 0, m, m, m);
  g.addColorStop(0.0, `rgba(255,255,255,${CENTER_ALPHA})`);
  g.addColorStop(0.55, `rgba(255,255,255,${CENTER_ALPHA * 0.5})`);
  g.addColorStop(1.0, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, TEX_PX, TEX_PX);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// Resolve a location's render anchor to a scene position (or null if it can't
// be placed in the current system).
function resolveAnchor(world, anchor) {
  if (!anchor) return null;

  if (anchor.kind === 'body' || anchor.kind === 'belt') {
    const want = (anchor.name || '').toLowerCase();
    for (const id in world.bodyIndex) {
      const b = world.bodyIndex[id];
      if ((b.name || '').toLowerCase() === want) return b.position.clone();
    }
    return null;
  }

  if (anchor.kind === 'lagrange') {
    const want = (anchor.code || '').toUpperCase();
    const pois = (world.coords && world.coords.pois) || [];
    for (const p of pois) {
      if (p.kind !== 'lagrange' || !p.helio) continue;
      const code = (p.name || '').trim().replace(/\s+/g, '-').toUpperCase();
      if (code === want && world.helioToScene) {
        return world.helioToScene(p.helio[0], p.helio[1]);
      }
    }
    return null;
  }

  return null;   // kind 'none'
}

// One flat disk lying in the ecliptic (XZ) plane at `pos`, tinted for `pct`.
function makeDisk(pos, radius, pct, tex, order) {
  const geo = new THREE.CircleGeometry(radius, 48);
  const mat = new THREE.MeshBasicMaterial({
    map: tex,
    color: pctColor(pct),
    transparent: true,
    depthWrite: false,
    blending: THREE.NormalBlending,
    side: THREE.DoubleSide,
  });
  const disk = new THREE.Mesh(geo, mat);
  disk.rotation.x = -Math.PI / 2;          // lay flat on the orbital plane
  disk.position.set(pos.x, pos.y + 0.4 * order, pos.z);   // tiny lift per stack level
  disk.renderOrder = 10 + order;
  disk.userData = { oreHeat: true };
  return disk;
}

// (Re)build the overlay for the active system from the set of selected ores.
export function buildOreHeat(world, selectedOres) {
  clearOreHeat(world);
  if (!selectedOres || selectedOres.size === 0) return;

  const group = new THREE.Group();
  const tex = makeFalloffTexture();
  let placed = 0;

  for (const loc of ORE_HEATMAP.locations) {
    if (loc.system !== world.activeSystem) continue;

    // Selected ores present at this location, strongest first.
    const hits = [];
    for (const ore of selectedOres) {
      const pct = loc.ores[ore];
      if (pct !== undefined) hits.push({ ore, pct });
    }
    if (!hits.length) continue;

    const pos = resolveAnchor(world, loc.anchor);
    if (!pos) continue;

    hits.sort((a, b) => b.pct - a.pct);
    hits.forEach((h, i) => {
      const r = BASE_RADIUS * Math.max(MIN_FACTOR, 1 - i * RADIUS_STEP);
      group.add(makeDisk(pos, r, h.pct, tex, i));
    });
    placed += hits.length;
  }

  world.scene.add(group);
  world.oreHeat = { group, tex, placed };
  if (typeof window !== 'undefined') window.__oreHeatPlaced = placed;   // smoke-test hook
}

export function clearOreHeat(world) {
  const oh = world.oreHeat;
  if (!oh) return;
  world.scene.remove(oh.group);
  oh.group.traverse(o => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) o.material.dispose();
  });
  if (oh.tex) oh.tex.dispose();
  world.oreHeat = null;
}
