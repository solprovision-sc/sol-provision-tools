// ═══════════════════════════════════════════════════════════════════
//  SOL PROVISION — STAR MAP  (v2 phase 2 — modular)
//  app/static/starmap/starmap.js
//
//  Orchestrator. Imports all modules, owns the `world` state, defines
//  the per-system rebuild flow, and wires UI events to scene actions.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

import { createRenderer, createComposer, attachResize, tuneBloom } from './core/renderer.js';
import { createCameraController }                        from './core/camera.js';
import { createLoop }                                    from './core/loop.js';

import { makeStarfield }       from './scene/starfield.js';
import { addStar }             from './scene/star.js';
import { addPlanet, addCapturedPlanet } from './scene/planet.js';
import { addMoon }             from './scene/moon.js';
import { addJumpPoint }        from './scene/jumppoint.js';
import { addAsteroidBelt }     from './scene/asteroid.js';
import { rotateClouds }        from './scene/clouds.js';

import { SYSTEMS }                       from './data/systems.js';
import { loadManifest }                  from './data/textures.js';
import { auToScene, moonAuToScene }      from './util/scale.js';

import { initHud, setActiveSystem, setOrbitToggle, flashTransition } from './ui/hud.js';
import { initInfoClose, showInfo, closeInfo }                        from './ui/info-panel.js';
import { initPicker }                                                from './ui/tooltip.js';
import { bodyToSlug, findBodyBySlug, parseUrl, pushState, replaceState, onPopState } from './util/url.js';

// ─── World ────────────────────────────────────────────────────────
const container = document.getElementById('canvas-container');
const scene = new THREE.Scene();
scene.add(makeStarfield());

const { renderer }                              = createRenderer(container);
const { camera, update: updateCamera, flyTo, reset: resetCamera } = createCameraController(container);
const { composer, bloomPass }                   = createComposer(renderer, scene, camera);
attachResize(renderer, composer, camera);

const loop = createLoop();

/** Shared mutable scene state. Scene-builder modules push into the array slots;
 *  per-frame systems read from them. Reset on every buildSystem(). */
const world = {
  scene,
  camera,
  sceneObjects:   [],   // everything addToScene'd for current system (for clearScene)
  bodyIndex:      {},   // id → enriched body data with .position/.mesh/.orbitRing
  meshes:         [],   // raycast-eligible
  labelSprites:   [],   // billboarded every frame
  orbitObjects:   [],   // for orbit-ring filter
  cloudMeshes:    [],   // rotated each frame + follows body in orbit motion
  filterState:    { PLANET:true, SATELLITE:true, orbits:true, labels:true, JUMPPOINT:true, ASTEROID_BELT:true },
  activeSystem:   'stanton',
  orbitAnimating: false,
  refAU:          1,
  moonRefAU:      0.001,
  systemConfig:   null,
};

// ─── Per-system build / clear ─────────────────────────────────────
function clearScene() {
  for (const obj of world.sceneObjects) {
    scene.remove(obj);
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
      for (const m of mats) {
        if (m.map) m.map.dispose();
        m.dispose();
      }
    }
  }
  world.sceneObjects.length = 0;
  world.bodyIndex    = {};
  world.meshes.length        = 0;
  world.labelSprites.length  = 0;
  world.orbitObjects.length  = 0;
  world.cloudMeshes.length   = 0;
  closeInfo();
}

function computeInitialPositions(systemConfig) {
  const starId      = systemConfig.bodies.find(b => b.type === 'STAR').id;
  const directKids  = systemConfig.bodies.filter(b => b.parent === starId && b.dist > 0);
  world.refAU       = Math.max(...directKids.map(b => b.dist));

  let moonRef = 0;
  for (const p of systemConfig.bodies.filter(b => b.type === 'PLANET')) {
    for (const m of systemConfig.bodies.filter(b => b.parent === p.id)) {
      if (m.dist > moonRef) moonRef = m.dist;
    }
  }
  world.moonRefAU = moonRef < 0.001 ? 0.001 : moonRef;

  // Initial index entries with default position + orbit angle.
  for (const b of systemConfig.bodies) {
    world.bodyIndex[b.id] = { ...b, position: new THREE.Vector3(), orbitAngle: b.lon * Math.PI / 180 };
  }

  // Recursive position walk so children inherit parent positions.
  function place(id) {
    const b = world.bodyIndex[id];
    if (!b) return new THREE.Vector3();
    if (b.type === 'STAR') { b.position.set(0, 0, 0); return b.position; }
    const parentBd = b.parent ? world.bodyIndex[b.parent] : null;
    const pp       = parentBd ? place(b.parent) : new THREE.Vector3();
    const useMoon  = parentBd && parentBd.type === 'PLANET';
    const r        = useMoon ? moonAuToScene(b.dist, world.moonRefAU)
                             : auToScene(b.dist, world.refAU);
    const lat      = b.lat * Math.PI / 180;
    b.position.set(
      pp.x + r * Math.cos(lat) * Math.cos(b.orbitAngle),
      pp.y + r * Math.sin(lat),
      pp.z + r * Math.cos(lat) * Math.sin(b.orbitAngle),
    );
    return b.position;
  }
  for (const b of systemConfig.bodies) place(b.id);
}

function buildSystem(systemKey) {
  clearScene();
  const sys = SYSTEMS[systemKey];
  world.activeSystem = systemKey;
  world.systemConfig = sys;
  computeInitialPositions(sys);

  const starId = sys.bodies.find(b => b.type === 'STAR').id;

  addStar(world, world.bodyIndex[starId], sys);

  for (const b of sys.bodies) {
    if      (b.type === 'PLANET'    && b.parent === starId) addPlanet(world, b, sys);
    else if (b.type === 'PLANET'    && b.parent !== starId) addCapturedPlanet(world, b, sys);
    else if (b.type === 'SATELLITE')                        addMoon(world, b, sys);
    else if (b.type === 'JUMPPOINT')                        addJumpPoint(world, b, sys);
    else if (b.type === 'ASTEROID_BELT' || b.type === 'ASTEROID_FIELD') addAsteroidBelt(world, b, sys);
  }

  applyFilters();
}

// ─── Per-frame systems ────────────────────────────────────────────
function applyFilters() {
  for (const m of world.meshes) {
    const id = m.userData.bodyId; if (!id) continue;
    const b = world.bodyIndex[id]; if (!b) continue;
    const t = b.type === 'ASTEROID_FIELD' ? 'ASTEROID_BELT' : b.type;
    if (world.filterState[t] !== undefined) m.visible = world.filterState[t];
  }
  for (const o of world.orbitObjects) {
    if (o.userData.type === 'orbit') o.visible = world.filterState['orbits'];
  }
  for (const s of world.labelSprites) {
    if (!world.filterState['labels']) { s.visible = false; continue; }
    s.visible = world.filterState[s.userData.bodyType] !== false;
  }
  // Jump-point lines aren't tracked in meshes/orbitObjects; flag them via userData.
  for (const o of world.sceneObjects) {
    if (o.userData.bodyType === 'JUMPPOINT' && !o.userData.bodyId) {
      o.visible = world.filterState['JUMPPOINT'];
    }
  }
}

function orbitSpeedFor(b) {
  if (b.period && b.period > 0) return 0.12 / b.period;
  return b.type === 'SATELLITE' ? 0.0018 : 0.0002;
}

function updateOrbits(dt) {
  if (!world.orbitAnimating || !world.systemConfig) return;

  for (const b of world.systemConfig.bodies) {
    if (b.type !== 'PLANET' && b.type !== 'SATELLITE') continue;
    const bd = world.bodyIndex[b.id]; if (!bd || !bd.mesh) continue;
    bd.orbitAngle += orbitSpeedFor(b) * dt;

    const parentBd = b.parent ? world.bodyIndex[b.parent] : null;
    const pp       = parentBd ? parentBd.position : new THREE.Vector3();
    const useMoon  = parentBd && parentBd.type === 'PLANET';
    const r        = useMoon ? moonAuToScene(b.dist, world.moonRefAU)
                             : auToScene(b.dist, world.refAU);
    const lat      = b.lat * Math.PI / 180;
    bd.position.set(
      pp.x + r * Math.cos(lat) * Math.cos(bd.orbitAngle),
      pp.y + r * Math.sin(lat),
      pp.z + r * Math.cos(lat) * Math.sin(bd.orbitAngle),
    );
    bd.mesh.position.copy(bd.position);
    if (bd.orbitRing) bd.orbitRing.position.copy(pp);
  }

  for (const s of world.labelSprites) {
    const fid = s.userData.followId; if (!fid) continue;
    const bd = world.bodyIndex[fid]; if (!bd) continue;
    s.position.set(
      bd.position.x + (s.userData.offX || 0),
      bd.position.y + (s.userData.offY || 0),
      bd.position.z,
    );
  }
}

function billboardLabels() {
  for (const s of world.labelSprites) s.quaternion.copy(camera.quaternion);
}

// ─── Loop subscribers (order matters — camera first, render last) ─
loop.add(updateCamera);
loop.add(updateOrbits);
loop.add(dt => rotateClouds(world, dt));
loop.add(billboardLabels);
loop.add(() => tuneBloom(bloomPass, camera.position.length()));
loop.add(() => composer.render());

// ─── Focus + URL state ───────────────────────────────────────────
// fly-to distance: clamp(80, 600, renderRadius * 5) — keeps moons close,
// gas giants comfortably framed, stars not buried inside the camera.
function focusDistanceFor(body) {
  const r = body.renderRadius || 12;
  return Math.max(80, Math.min(600, r * 5));
}

function switchSystem(key, { updateUrl = true } = {}) {
  if (key === world.activeSystem) return;
  flashTransition();
  buildSystem(key);
  setActiveSystem(key, SYSTEMS[key]);
  world.orbitAnimating = false;
  setOrbitToggle(false);
  resetCamera();  // overview shot of the new system (focusBody, if next, will override)
  if (updateUrl) pushState(key, null);
}

function focusBody(bodyId, { updateUrl = true } = {}) {
  const b = world.bodyIndex[bodyId]; if (!b) return;
  const parentName = b.parent && world.bodyIndex[b.parent]
    ? (world.bodyIndex[b.parent].name || '—')
    : 'None';
  showInfo(b, parentName);
  flyTo({ position: b.position, distance: focusDistanceFor(b) });
  if (updateUrl) {
    const slug = bodyToSlug(b);
    if (slug) pushState(world.activeSystem, slug);
  }
}

function handleClose() {
  closeInfo();
  pushState(world.activeSystem, null);
}

function applyUrlState({ system, bodySlug }) {
  const target = (system && SYSTEMS[system]) ? system : 'stanton';
  if (target !== world.activeSystem) switchSystem(target, { updateUrl: false });
  if (bodySlug) {
    const body = findBodyBySlug(SYSTEMS[target], bodySlug);
    if (body) focusBody(body.id, { updateUrl: false });
    else closeInfo();
  } else {
    closeInfo();
  }
}

initHud({
  onSwitchSystem: switchSystem,
  onToggleFilter: (type, active) => { world.filterState[type] = active; applyFilters(); },
  onToggleOrbits: () => {
    world.orbitAnimating = !world.orbitAnimating;
    setOrbitToggle(world.orbitAnimating);
  },
});
initInfoClose(handleClose);
initPicker(container, camera, world, focusBody);
onPopState(applyUrlState);

// ─── Init ─────────────────────────────────────────────────────────
// Kick off the manifest fetch (fire-and-forget — body texture loads await it).
loadManifest();

const initial = parseUrl();
const initialSystem = (initial.system && SYSTEMS[initial.system]) ? initial.system : 'stanton';
buildSystem(initialSystem);
setActiveSystem(initialSystem, SYSTEMS[initialSystem]);
// Canonicalize the URL (so /starmap → /starmap/stanton on load).
replaceState(initialSystem, initial.bodySlug);

if (initial.bodySlug) {
  const body = findBodyBySlug(SYSTEMS[initialSystem], initial.bodySlug);
  if (body) focusBody(body.id, { updateUrl: false });
}

loop.start();
