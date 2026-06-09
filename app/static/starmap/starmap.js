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
import { makeEclipticGrid }    from './scene/grid.js';
import { makeNebula }          from './scene/nebula.js';
import { addStar }             from './scene/star.js';
import { addPlanet, addCapturedPlanet } from './scene/planet.js';
import { addMoon }             from './scene/moon.js';
import { addJumpPoint }        from './scene/jumppoint.js';
import { addAsteroidBelt }     from './scene/asteroid.js';
import { rotateClouds }        from './scene/clouds.js';
import { setMarker, clearMarker, updateMarker } from './scene/marker.js';

import { SYSTEMS }                       from './data/systems.js';
import { loadManifest }                  from './data/textures.js';
import { loadSystemCoords }              from './data/coords.js';
import { auToScene, moonAuToScene }      from './util/scale.js';

// 1 AU in km — used to seed the systems.js→km fallback factor for bodies the
// DB doesn't position (asteroid belts, unmodelled jump points).
const AU_KM = 149597870.7;
const SCENE_BASE_R = 700;   // scene radius of the outermost star-direct body

import { initHud, setActiveSystem, setOrbitToggle, flashTransition } from './ui/hud.js';
import { initInfoClose, showInfo, closeInfo }                        from './ui/info-panel.js';
import { initPicker }                                                from './ui/tooltip.js';
import { initPosition }                                             from './ui/position.js';
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
  // Real-coordinate transform (set per system in computeInitialPositions).
  refKm:          1,
  helioToScene:   null,   // (xKm, yKm) → THREE.Vector3 scene position on the plane
  coords:         null,   // raw /api/starmap payload for the active system
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

// Walk the parent chain so children inherit parent positions, placing each body
// at its precomputed orbitRadiusScene / orbitAngle relative to its parent.
function placeAll(systemConfig) {
  function place(id) {
    const b = world.bodyIndex[id];
    if (!b) return new THREE.Vector3();
    if (b.type === 'STAR') { b.position.set(0, 0, 0); return b.position; }
    const parentBd = b.parent ? world.bodyIndex[b.parent] : null;
    const pp       = parentBd ? place(b.parent) : new THREE.Vector3();
    const r        = b.orbitRadiusScene || 0;
    const lat      = (b.lat || 0) * Math.PI / 180;
    b.position.set(
      pp.x + r * Math.cos(lat) * Math.cos(b.orbitAngle),
      pp.y + r * Math.sin(lat),
      pp.z + r * Math.cos(lat) * Math.sin(b.orbitAngle),
    );
    return b.position;
  }
  for (const b of systemConfig.bodies) place(b.id);
}

// systems.js-only placement (no DB coords reachable). Mirrors the original v2
// behaviour: star-direct bodies via auToScene, moons via the exaggerated
// moonAuToScene, angles from the curated lon. Keeps the map working offline.
function computeLegacyPositions(systemConfig) {
  const starId     = systemConfig.bodies.find(b => b.type === 'STAR').id;
  const directKids = systemConfig.bodies.filter(b => b.parent === starId && b.dist > 0);
  world.refAU      = Math.max(...directKids.map(b => b.dist), 1);

  let moonRef = 0;
  for (const p of systemConfig.bodies.filter(b => b.type === 'PLANET'))
    for (const m of systemConfig.bodies.filter(b => b.parent === p.id))
      if (m.dist > moonRef) moonRef = m.dist;
  world.moonRefAU = moonRef < 0.001 ? 0.001 : moonRef;

  world.refKm        = world.refAU * AU_KM;
  world.helioToScene = (x, y) => {
    const r = Math.hypot(x, y), a = Math.atan2(y, x);
    const R = auToScene(r / AU_KM, world.refAU);
    return new THREE.Vector3(R * Math.cos(a), 0, R * Math.sin(a));
  };

  for (const b of systemConfig.bodies) {
    const parentIsPlanet = b.parent && systemConfig.bodies
      .some(x => x.id === b.parent && x.type === 'PLANET');
    const r = parentIsPlanet ? moonAuToScene(b.dist, world.moonRefAU)
                             : auToScene(b.dist, world.refAU);
    world.bodyIndex[b.id] = {
      ...b, position: new THREE.Vector3(), helio: null, dbUuid: null,
      orbitAngle: b.lon * Math.PI / 180, orbitRadiusScene: r,
    };
  }
  placeAll(systemConfig);
}

// DB-coordinate placement: star/planets/jump points from real helio; moons +
// captured planets keep the exaggerated parent-relative offset (so they stay
// legible) but carry their real .helio for triangulation; asteroid belts and
// any unmatched jump points fall back to a per-system km factor. A single
// world.helioToScene maps any (x,y) km → scene, shared by bodies + the marker.
function computeInitialPositions(systemConfig, coords) {
  const hasDb = coords && Array.isArray(coords.bodies) && coords.bodies.some(n => n.helio);
  if (!hasDb) { computeLegacyPositions(systemConfig); return; }

  const starId  = systemConfig.bodies.find(b => b.type === 'STAR').id;
  const bodies  = systemConfig.bodies;

  // DB lookups: bodies by name, jump points by their target-system token.
  const nodeByName   = new Map();
  const jumpByTarget = new Map();
  for (const n of coords.bodies) {
    if (n.helio && n.name) nodeByName.set(n.name.toLowerCase(), n);
    if (n.kind === 'jumppoint' && n.helio && n.key) {
      const toks = n.key.split('_');           // JumpPoint_<own>_<target>
      if (toks.length >= 3) jumpByTarget.set(toks[2].toLowerCase(), n);
    }
  }

  // Index every body; attach real helio where we can match it.
  for (const b of bodies) {
    const bd = { ...b, position: new THREE.Vector3(), helio: null, dbUuid: null,
                 orbitAngle: b.lon * Math.PI / 180, orbitRadiusScene: 0 };
    const n = nodeByName.get((b.name || '').toLowerCase());
    if (n) { bd.helio = n.helio; bd.dbUuid = n.uuid; }
    else if (b.type === 'JUMPPOINT') {
      const jn = jumpByTarget.get((b.name || '').split(/\s+/)[0].toLowerCase());
      if (jn) { bd.helio = jn.helio; bd.dbUuid = jn.uuid; }
    }
    world.bodyIndex[b.id] = bd;
  }

  // Radial reference + transform from star-direct bodies that have real coords.
  const starDirect = bodies.filter(b => b.parent === starId && b.type !== 'STAR');
  let refKm = 0;
  for (const b of starDirect) {
    const bd = world.bodyIndex[b.id];
    if (bd.helio) refKm = Math.max(refKm, Math.hypot(bd.helio[0], bd.helio[1]));
  }
  world.refKm = refKm > 0 ? refKm : AU_KM;
  const scaleR = km => Math.sqrt(Math.max(km, 0) / world.refKm) * SCENE_BASE_R;
  world.helioToScene = (x, y) => {
    const r = Math.hypot(x, y), a = Math.atan2(y, x), R = scaleR(r);
    return new THREE.Vector3(R * Math.cos(a), 0, R * Math.sin(a));
  };

  // Median km-per-AU factor from matched star-direct bodies, for the gaps.
  const factors = starDirect
    .map(b => world.bodyIndex[b.id])
    .filter(bd => bd.helio && bd.dist > 0)
    .map(bd => Math.hypot(bd.helio[0], bd.helio[1]) / bd.dist)
    .sort((a, b) => a - b);
  const kmPerAU = factors.length ? factors[Math.floor(factors.length / 2)] : AU_KM / 10;

  // Moon exaggeration reference (systems.js), kept for legibility.
  let moonRef = 0;
  for (const p of bodies.filter(b => b.type === 'PLANET'))
    for (const m of bodies.filter(b => b.parent === p.id))
      if (m.dist > moonRef) moonRef = m.dist;
  world.moonRefAU = moonRef < 0.001 ? 0.001 : moonRef;
  world.refAU     = Math.max(...starDirect.map(b => b.dist), 1);

  // Star-direct bodies: real radius+angle if matched, else km-factor fallback.
  for (const b of starDirect) {
    const bd = world.bodyIndex[b.id];
    if (bd.helio) {
      bd.orbitRadiusScene = scaleR(Math.hypot(bd.helio[0], bd.helio[1]));
      bd.orbitAngle       = Math.atan2(bd.helio[1], bd.helio[0]);
    } else {
      bd.orbitRadiusScene = scaleR(b.dist * kmPerAU);
      bd.orbitAngle       = b.lon * Math.PI / 180;
    }
  }

  // Moons + captured planets: exaggerated offset from parent for legibility,
  // but use the real DB bearing when both ends are positioned.
  for (const b of bodies) {
    const bd = world.bodyIndex[b.id];
    const pbd = b.parent ? world.bodyIndex[b.parent] : null;
    if (!pbd || pbd.type !== 'PLANET') continue;
    bd.orbitRadiusScene = moonAuToScene(b.dist, world.moonRefAU);
    if (bd.helio && pbd.helio) {
      bd.orbitAngle = Math.atan2(bd.helio[1] - pbd.helio[1], bd.helio[0] - pbd.helio[0]);
    }
  }

  placeAll(systemConfig);
}

async function buildSystem(systemKey) {
  clearScene();
  const sys = SYSTEMS[systemKey];
  world.activeSystem = systemKey;
  world.systemConfig = sys;
  const coords = await loadSystemCoords(systemKey);
  world.coords = coords;
  computeInitialPositions(sys, coords);

  const starId = sys.bodies.find(b => b.type === 'STAR').id;

  // Backdrop nebula + ecliptic reference grid go in first so bodies draw over them.
  const nebula = makeNebula(sys.gridColor || '#3fe0ff');
  scene.add(nebula);
  world.sceneObjects.push(nebula);

  const gridOuter = SCENE_BASE_R * 1.12;
  const grid = makeEclipticGrid(gridOuter, sys.gridColor || '#3fe0ff');
  scene.add(grid);
  world.sceneObjects.push(grid);

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
    const r        = bd.orbitRadiusScene || 0;
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

// Labels are authored for the overview camera distance. Scale each inversely
// with its screen-space distance (relative to the default view) so they hold a
// roughly constant on-screen size — no more giant text when fly-to zooms in.
const LABEL_REF_DIST = 900;
function billboardLabels() {
  for (const s of world.labelSprites) {
    s.quaternion.copy(camera.quaternion);
    if (!s.userData.baseScale) s.userData.baseScale = { x: s.scale.x, y: s.scale.y };
    const d = camera.position.distanceTo(s.position);
    const f = Math.max(0.4, Math.min(2.4, d / LABEL_REF_DIST));
    s.scale.set(s.userData.baseScale.x * f, s.userData.baseScale.y * f, 1);
  }
}

// ─── Loop subscribers (order matters — camera first, render last) ─
loop.add(updateCamera);
loop.add(updateOrbits);
loop.add(dt => rotateClouds(world, dt));
loop.add(billboardLabels);
loop.add(dt => updateMarker(world, dt));
loop.add(() => tuneBloom(bloomPass, camera.position.length()));
loop.add(() => composer.render());

// ─── Position marker ──────────────────────────────────────────────
let positionUI = null;

// Real helio (x,y) km → scene position. Planets are at true scale so the
// global transform is correct there; but a point inside a planet's moon-system
// is mapped proportionally into that planet's *exaggerated* local frame, so a
// marker e.g. 10% of the way from a moon to its planet lands 10% along the
// rendered moon→planet line instead of collapsing onto the planet.
function realToScene(x, y) {
  const sys = world.systemConfig;
  let nearest = null, nd = Infinity;
  for (const b of sys.bodies) {
    if (b.type !== 'PLANET') continue;
    const bd = world.bodyIndex[b.id];
    if (!bd || !bd.helio) continue;
    const d = Math.hypot(x - bd.helio[0], y - bd.helio[1]);
    if (d < nd) { nd = d; nearest = bd; }
  }
  if (nearest) {
    let maxReal = 0, kSum = 0, kN = 0;
    for (const mb of sys.bodies) {
      if (mb.parent !== nearest.id) continue;
      const mbd = world.bodyIndex[mb.id];
      if (!mbd || !mbd.helio || !mbd.orbitRadiusScene) continue;
      const realMoon = Math.hypot(mbd.helio[0] - nearest.helio[0], mbd.helio[1] - nearest.helio[1]);
      if (realMoon > maxReal) maxReal = realMoon;
      if (realMoon > 0) { kSum += mbd.orbitRadiusScene / realMoon; kN++; }
    }
    if (kN && nd < maxReal * 1.6) {
      const K = kSum / kN;   // average local exaggeration of this moon-system
      return new THREE.Vector3(
        nearest.position.x + (x - nearest.helio[0]) * K,
        0,
        nearest.position.z + (y - nearest.helio[1]) * K,
      );
    }
  }
  return world.helioToScene(x, y);
}

async function getLocations(systemKey) {
  const data = await loadSystemCoords(systemKey);
  if (!data) return [];
  const list = [];
  for (const b of (data.bodies || [])) {
    if ((b.kind === 'planet' || b.kind === 'moon') && b.helio)
      list.push({ name: b.name, kind: b.kind, helio: b.helio, parentName: b.parent_name || '—', key: b.key });
  }
  for (const p of (data.pois || [])) {
    if (p.helio) list.push({ name: p.name, kind: p.kind, helio: p.helio, parentName: p.parent_name || '—', key: p.key });
  }
  return list;
}

function placePosition(x, y, label) {
  if (!world.helioToScene) return;
  setMarker(world, realToScene(x, y), label);
}

// ─── Focus + URL state ───────────────────────────────────────────
// fly-to distance: clamp(80, 600, renderRadius * 5) — keeps moons close,
// gas giants comfortably framed, stars not buried inside the camera.
function focusDistanceFor(body) {
  const r = body.renderRadius || 12;
  return Math.max(80, Math.min(600, r * 5));
}

async function switchSystem(key, { updateUrl = true } = {}) {
  if (key === world.activeSystem) return;
  flashTransition();
  clearMarker(world);            // position is system-specific
  await buildSystem(key);
  setActiveSystem(key, SYSTEMS[key]);
  world.orbitAnimating = false;
  setOrbitToggle(false);
  resetCamera();  // overview shot of the new system (focusBody, if next, will override)
  if (positionUI) await positionUI.setSystem(key);
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

async function applyUrlState({ system, bodySlug }) {
  const target = (system && SYSTEMS[system]) ? system : 'stanton';
  if (target !== world.activeSystem) await switchSystem(target, { updateUrl: false });
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

positionUI = initPosition({
  getLocations,
  onSwitchSystem: key => switchSystem(key),
  onPlace:        placePosition,
  onClear:        () => clearMarker(world),
});

// ─── Init ─────────────────────────────────────────────────────────
// Kick off the manifest fetch (fire-and-forget — body texture loads await it).
loadManifest();

const initial = parseUrl();
const initialSystem = (initial.system && SYSTEMS[initial.system]) ? initial.system : 'stanton';

loop.start();

// Build the initial system (awaits the DB coords), then apply any deep-linked
// body focus once the bodies exist.
await buildSystem(initialSystem);
setActiveSystem(initialSystem, SYSTEMS[initialSystem]);
await positionUI.setSystem(initialSystem);
// Canonicalize the URL (so /starmap → /starmap/stanton on load).
replaceState(initialSystem, initial.bodySlug);

if (initial.bodySlug) {
  const body = findBodyBySlug(SYSTEMS[initialSystem], initial.bodySlug);
  if (body) focusBody(body.id, { updateUrl: false });
}
