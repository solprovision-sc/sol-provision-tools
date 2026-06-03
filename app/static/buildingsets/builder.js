// Sol Provision — Base Builder prototype
// three.js r0.170 (importmap in base_builder.html). Loads meshopt-compressed GLBs,
// builds a palette from manifest.json, places pieces on a snapped grid.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';

const ASSET_BASE = '/static/buildingsets/';

// ── Procedural colours by material-slot name (Option A: no textures) ───────────
const MAT_RULES = [
  [/glass|window/,            { color: 0x6fa8c0, opacity: 0.35, transparent: true, metalness: 0.0, roughness: 0.05 }],
  [/light|illum|lamp|emiss/,  { color: 0xffe6b0, emissive: 0xffcf8a, emissiveIntensity: 0.9 }],
  [/metal|steel|alu|beam|pipe|mechanical/, { color: 0x8b919b, metalness: 0.85, roughness: 0.45 }],
  [/brass|gold/,              { color: 0xc9a96e, metalness: 0.9, roughness: 0.35 }],
  [/wood|oak/,                { color: 0x8a6a43, roughness: 0.8 }],
  [/concrete|stone|plaster/,  { color: 0x9a978c, roughness: 0.95 }],
  [/plastic|white/,           { color: 0xd6d8da, roughness: 0.6 }],
  [/paint_green|green/,       { color: 0x5a8f5a }],
  [/paint_yellow|yellow/,     { color: 0xc9b24a }],
  [/decal|marking|sign|graphic/, { color: 0xb8babd, roughness: 0.7 }],
  [/panel|wall|hull|trim/,    { color: 0xa7adb4, metalness: 0.4, roughness: 0.6 }],
];
const DEFAULT_MAT = { color: 0xb0b4b8, metalness: 0.3, roughness: 0.7 };

function matParams(name) {
  const n = (name || '').toLowerCase();
  for (const [re, p] of MAT_RULES) if (re.test(n)) return p;
  return DEFAULT_MAT;
}

// ── three.js setup ─────────────────────────────────────────────────────────────
let scene, camera, renderer, controls, loader;
let groundPlane, gridHelper;
let armed = null;      // piece currently selected from palette
let ghost = null;      // preview mesh following the cursor
let selected = null;   // a placed object selected for deletion
const placed = [];
const modelCache = new Map();   // id -> Promise<THREE.Group>
let snapStep = 1;
let ghostRotation = 0;
let ghostElevation = 0;   // persists between placements so you can lay a whole upper floor
let ghostMirror = 1;      // 1 = normal, -1 = mirrored on local X

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

// ── Palette state (full 2,614-piece catalog) ──────────────────────────────────
let allPieces = [];
const pieceById = new Map();
const filterState = { q: '', kit: '', cats: new Set(), modular: true };
const MAX_RENDER = 500;   // cap rendered list to keep DOM responsive

const viewport       = document.getElementById('viewport');
const statusEl       = document.getElementById('builder-status');
const palEl          = document.getElementById('palette');
const piecesCountEl  = document.getElementById('bb-piece-count');
const searchEl       = document.getElementById('bb-search');
const kitSelEl       = document.getElementById('bb-kit');
const modularChkEl   = document.getElementById('bb-modular');
const catChipsEl     = document.getElementById('bb-cats');

init();

async function init() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0c0e);
  scene.fog = new THREE.Fog(0x0a0c0e, 80, 220);

  camera = new THREE.PerspectiveCamera(55, viewport.clientWidth / viewport.clientHeight, 0.1, 2000);
  camera.position.set(24, 22, 28);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(viewport.clientWidth, viewport.clientHeight);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  viewport.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI / 2 - 0.02;
  controls.target.set(0, 2, 0);

  // lighting
  scene.add(new THREE.HemisphereLight(0xbfd6ff, 0x2a1f1a, 0.9));
  const key = new THREE.DirectionalLight(0xffe9d0, 1.6);
  key.position.set(40, 60, 25);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  const d = 60;
  key.shadow.camera.left = -d; key.shadow.camera.right = d;
  key.shadow.camera.top = d; key.shadow.camera.bottom = -d;
  scene.add(key);
  scene.add(new THREE.DirectionalLight(0x88aacc, 0.4).translateZ(-40));

  // ground + grid
  const groundMat = new THREE.MeshStandardMaterial({ color: 0x12100e, roughness: 1 });
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), groundMat);
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);
  groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

  gridHelper = new THREE.GridHelper(200, 200, 0xc0392b, 0x2a1f1a);
  gridHelper.position.y = 0.01;
  scene.add(gridHelper);
  const major = new THREE.GridHelper(200, 25, 0x9a2d20, 0x3a2820);
  major.position.y = 0.02;
  scene.add(major);

  loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);

  window.addEventListener('resize', onResize);
  renderer.domElement.addEventListener('pointerdown', onPointerDown);
  renderer.domElement.addEventListener('pointerup', onPointerUp);
  renderer.domElement.addEventListener('pointermove', onPointerMove);
  window.addEventListener('keydown', onKey);

  await loadManifest();
  bindToolbar();
  animate();
}

// ── Manifest + palette ─────────────────────────────────────────────────────────
async function loadManifest() {
  const t0 = performance.now();
  allPieces = await (await fetch(ASSET_BASE + 'manifest.json')).json();
  allPieces.forEach(p => pieceById.set(p.id, p));
  initKitFilter();
  renderCatChips();
  bindFilters();
  renderPalette();
  const ms = (performance.now() - t0).toFixed(0);
  setStatus(`${allPieces.length.toLocaleString()} pieces loaded in ${ms} ms. Pick one to begin.`);
}

function initKitFilter() {
  const counts = new Map();
  allPieces.forEach(p => counts.set(p.kit, (counts.get(p.kit) || 0) + 1));
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  kitSelEl.innerHTML = `<option value="">All kits (${allPieces.length.toLocaleString()})</option>` +
    sorted.map(([k, n]) => `<option value="${escapeAttr(k)}">${escapeHtml(k)} (${n})</option>`).join('');
}

function renderCatChips() {
  // Chip counts honour the modular toggle so the numbers match what you'd see.
  const base = filterState.modular ? allPieces.filter(p => p.modular) : allPieces;
  const counts = new Map();
  base.forEach(p => counts.set(p.category, (counts.get(p.category) || 0) + 1));
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  catChipsEl.innerHTML = sorted.map(([c, n]) =>
    `<button class="bb-chip ${filterState.cats.has(c) ? 'active' : ''}" data-cat="${escapeAttr(c)}">${escapeHtml(c)}<span class="chip-n">${n}</span></button>`
  ).join('');
}

function bindFilters() {
  searchEl.addEventListener('input', () => {
    filterState.q = searchEl.value;
    renderPalette();
  });
  kitSelEl.addEventListener('change', () => {
    filterState.kit = kitSelEl.value;
    renderPalette();
  });
  modularChkEl.addEventListener('change', () => {
    filterState.modular = modularChkEl.checked;
    renderCatChips();
    renderPalette();
  });
  catChipsEl.addEventListener('click', (e) => {
    const chip = e.target.closest('.bb-chip');
    if (!chip) return;
    const cat = chip.dataset.cat;
    if (filterState.cats.has(cat)) filterState.cats.delete(cat);
    else filterState.cats.add(cat);
    chip.classList.toggle('active');
    renderPalette();
  });
  palEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.pal-item');
    if (!btn) return;
    const p = pieceById.get(btn.dataset.id);
    if (p) armPiece(p);
  });
}

function applyFilters() {
  const q = filterState.q.trim().toLowerCase();
  return allPieces.filter(p => {
    if (filterState.modular && !p.modular) return false;
    if (filterState.kit && p.kit !== filterState.kit) return false;
    if (filterState.cats.size > 0 && !filterState.cats.has(p.category)) return false;
    if (q) {
      const hay = `${p.name} ${p.kit} ${p.id}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderPalette() {
  const r = applyFilters();
  piecesCountEl.textContent = `${r.length.toLocaleString()} of ${allPieces.length.toLocaleString()} pieces`;
  if (r.length === 0) {
    palEl.innerHTML = `<div class="pal-truncate">No pieces match these filters.</div>`;
    return;
  }
  const groups = new Map();
  for (const p of r) {
    if (!groups.has(p.kit)) groups.set(p.kit, []);
    groups.get(p.kit).push(p);
  }
  const kits = [...groups.keys()].sort();
  const parts = [];
  let rendered = 0;
  for (const k of kits) {
    if (rendered >= MAX_RENDER) break;
    const items = groups.get(k);
    parts.push(`<div class="pal-cat"><span>${escapeHtml(k)}</span><span class="pal-cat-n">${items.length}</span></div>`);
    for (const p of items) {
      if (rendered >= MAX_RENDER) break;
      const sz = p.size.map(x => x < 10 ? x.toFixed(1) : Math.round(x).toString()).join('×');
      parts.push(
        `<button class="pal-item" data-id="${escapeAttr(p.id)}">` +
        `<span class="pal-name">${escapeHtml(p.name)}</span>` +
        `<span class="pal-dim">${sz} m · ${escapeHtml(p.category)}</span>` +
        `</button>`
      );
      rendered++;
    }
  }
  if (rendered < r.length) {
    parts.push(`<div class="pal-truncate">Showing ${rendered.toLocaleString()} of ${r.length.toLocaleString()}.<br>Refine the search or pick a kit.</div>`);
  }
  palEl.innerHTML = parts.join('');
}

// ── GLB loading + recolour ──────────────────────────────────────────────────────
function loadModel(p) {
  if (!modelCache.has(p.id)) {
    modelCache.set(p.id, new Promise((resolve, reject) => {
      loader.load(ASSET_BASE + p.file, (gltf) => {
        const root = gltf.scene;
        root.traverse((o) => {
          if (!o.isMesh) return;
          o.castShadow = true; o.receiveShadow = true;
          const srcName = o.material ? o.material.name : '';
          o.material = new THREE.MeshStandardMaterial(matParams(srcName));
          o.material.name = srcName;   // keep slot name so downstream matParams() still resolves
        });
        // recentre on ground: x/z centred, y resting on 0
        const box = new THREE.Box3().setFromObject(root);
        const c = box.getCenter(new THREE.Vector3());
        root.position.x -= c.x;
        root.position.z -= c.z;
        root.position.y -= box.min.y;
        const wrap = new THREE.Group();
        wrap.add(root);
        resolve(wrap);
      }, undefined, reject);
    }));
  }
  return modelCache.get(p.id);
}

async function armPiece(p) {
  document.querySelectorAll('.pal-item.active').forEach(e => e.classList.remove('active'));
  const el = document.querySelector(`.pal-item[data-id="${CSS.escape(p.id)}"]`);
  if (el) el.classList.add('active');
  armed = p;
  ghostRotation = 0;
  ghostMirror = 1;
  deselect();
  const tmpl = await loadModel(p);
  if (ghost) scene.remove(ghost);
  ghost = tmpl.clone(true);
  ghost.scale.x = ghostMirror;
  ghost.traverse((o) => {
    if (o.isMesh) {
      o.material = o.material.clone();
      o.material.transparent = true;
      o.material.opacity = 0.5;
      o.castShadow = false;
    }
  });
  scene.add(ghost);
  setStatus(`${p.name} armed — click grid to place · R rotate · Q/E elevate · F mirror · Esc cancel`);
}

// ── pointer / placement ──────────────────────────────────────────────────────────
let downPos = null;
function onPointerDown(e) { downPos = { x: e.clientX, y: e.clientY }; }

function onPointerUp(e) {
  if (!downPos) return;
  const moved = Math.hypot(e.clientX - downPos.x, e.clientY - downPos.y);
  downPos = null;
  if (moved > 5) return;   // was an orbit drag, not a click
  if (armed && ghost) { placeGhost(); return; }
  pickPlaced(e);           // selection when nothing armed
}

function updatePointer(e) {
  const r = renderer.domElement.getBoundingClientRect();
  pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  pointer.y = -((e.clientY - r.top) / r.height) * 2 + 1;
}

function groundPoint(e) {
  updatePointer(e);
  raycaster.setFromCamera(pointer, camera);
  const hit = new THREE.Vector3();
  if (raycaster.ray.intersectPlane(groundPlane, hit)) {
    hit.x = Math.round(hit.x / snapStep) * snapStep;
    hit.z = Math.round(hit.z / snapStep) * snapStep;
    return hit;
  }
  return null;
}

function onPointerMove(e) {
  if (!armed || !ghost) return;
  const g = groundPoint(e);
  if (g) {
    ghost.position.set(g.x, ghostElevation, g.z);
    ghost.rotation.y = ghostRotation;
  }
}

function placeGhost() {
  const obj = ghost.clone(true);
  obj.traverse((o) => {
    if (o.isMesh) {
      o.material = o.material.clone();
      o.material.transparent = matParams(o.material.name).transparent || false;
      o.material.opacity = o.material.transparent ? 0.35 : 1;
      o.castShadow = true;
    }
  });
  obj.userData.pieceId = armed.id;
  scene.add(obj);
  placed.push(obj);
  setStatus(`Placed ${armed.name}. (${placed.length} on grid)`);
}

function pickPlaced(e) {
  updatePointer(e);
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(placed, true);
  if (hits.length) {
    let root = hits[0].object;
    while (root.parent && !placed.includes(root)) root = root.parent;
    select(root);
  } else {
    deselect();
  }
}

function select(obj) {
  deselect();
  selected = obj;
  obj.traverse((o) => { if (o.isMesh) o.material.emissive = new THREE.Color(0xc0392b).multiplyScalar(0.6); });
  setStatus('Piece selected — Delete to remove · Esc to deselect');
}
function deselect() {
  if (selected) selected.traverse((o) => { if (o.isMesh && o.material.emissive) o.material.emissive.setHex(0x000000); });
  selected = null;
}

function onKey(e) {
  // Don't hijack typing in the search input.
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT')) return;
  if (e.key === 'r' || e.key === 'R') {
    ghostRotation += Math.PI / 2;
    if (ghost) ghost.rotation.y = ghostRotation;
    if (selected) selected.rotation.y += Math.PI / 2;
  } else if (e.key === 'e' || e.key === 'E') {
    adjustElevation(snapStep);
  } else if (e.key === 'q' || e.key === 'Q') {
    adjustElevation(-snapStep);
  } else if (e.key === 'f' || e.key === 'F') {
    toggleMirror();
  } else if (e.key === 'Escape') {
    disarm(); deselect();
  } else if (e.key === 'Delete' || e.key === 'Backspace') {
    if (selected) { scene.remove(selected); placed.splice(placed.indexOf(selected), 1); selected = null; setStatus('Piece removed.'); }
  }
}

function adjustElevation(delta) {
  if (armed && ghost) {
    ghostElevation += delta;
    ghost.position.y = ghostElevation;
    setStatus(`${armed.name} · elevation ${ghostElevation.toFixed(2)} m — Q/E adjust · click to place`);
  } else if (selected) {
    selected.position.y += delta;
    setStatus(`Elevation ${selected.position.y.toFixed(2)} m`);
  }
}

// Mirror a piece by negating local-X scale; negative scale flips winding so we
// switch its materials to DoubleSide to keep lighting correct.
function applyMirror(obj, sign) {
  obj.scale.x = Math.abs(obj.scale.x) * sign;
  obj.traverse((o) => { if (o.isMesh && o.material) o.material.side = THREE.DoubleSide; });
}

function toggleMirror() {
  if (armed && ghost) {
    ghostMirror *= -1;
    applyMirror(ghost, ghostMirror);
    setStatus(`${armed.name} · ${ghostMirror < 0 ? 'mirrored' : 'normal'} — F flip · click to place`);
  } else if (selected) {
    applyMirror(selected, selected.scale.x < 0 ? 1 : -1);
    setStatus(`Piece ${selected.scale.x < 0 ? 'mirrored' : 'normal'}`);
  }
}

function disarm() {
  armed = null;
  if (ghost) { scene.remove(ghost); ghost = null; }
  document.querySelectorAll('.pal-item.active').forEach(e => e.classList.remove('active'));
  setStatus('Idle. Pick a piece to place.');
}

function bindToolbar() {
  document.getElementById('btn-clear').onclick = () => {
    placed.forEach(o => scene.remove(o)); placed.length = 0; deselect();
    setStatus('Cleared.');
  };
  const snapSel = document.getElementById('snap-step');
  snapSel.onchange = () => { snapStep = parseFloat(snapSel.value); setStatus(`Snap = ${snapStep} m`); };
}

function setStatus(t) { if (statusEl) statusEl.textContent = t; }

function onResize() {
  camera.aspect = viewport.clientWidth / viewport.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(viewport.clientWidth, viewport.clientHeight);
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

// ── Small DOM helpers ──────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }
