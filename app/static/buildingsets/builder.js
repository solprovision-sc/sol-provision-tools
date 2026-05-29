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
let armed = null;     // {id, dims} currently selected from palette
let ghost = null;     // preview mesh following the cursor
let selected = null;  // a placed object selected for deletion
const placed = [];
const modelCache = new Map();  // id -> Promise<THREE.Group>
let snapStep = 1;
let ghostRotation = 0;

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let manifest = [];

const viewport = document.getElementById('viewport');
const statusEl = document.getElementById('builder-status');

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

async function loadManifest() {
  manifest = await (await fetch(ASSET_BASE + 'manifest.json')).json();
  const byCat = {};
  for (const p of manifest) (byCat[p.category] ??= []).push(p);
  const pal = document.getElementById('palette');
  pal.innerHTML = '';
  for (const cat of Object.keys(byCat).sort()) {
    const h = document.createElement('div');
    h.className = 'pal-cat';
    h.textContent = cat;
    pal.appendChild(h);
    for (const p of byCat[cat]) {
      const el = document.createElement('button');
      el.className = 'pal-item';
      el.dataset.id = p.id;
      el.innerHTML = `<span class="pal-name">${p.name}</span>
        <span class="pal-dim">${p.size[0]}×${p.size[1]}×${p.size[2]} m</span>`;
      el.onclick = () => armPiece(p, el);
      pal.appendChild(el);
    }
  }
  setStatus(`${manifest.length} pieces loaded. Pick one, then click the grid to place.`);
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

async function armPiece(p, el) {
  document.querySelectorAll('.pal-item.active').forEach(e => e.classList.remove('active'));
  el.classList.add('active');
  armed = p;
  ghostRotation = 0;
  deselect();
  const tmpl = await loadModel(p);
  if (ghost) scene.remove(ghost);
  ghost = tmpl.clone(true);
  ghost.traverse((o) => {
    if (o.isMesh) {
      o.material = o.material.clone();
      o.material.transparent = true;
      o.material.opacity = 0.5;
      o.castShadow = false;
    }
  });
  scene.add(ghost);
  setStatus(`${p.name} armed — click grid to place · R rotate · Esc cancel`);
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
    ghost.position.set(g.x, 0, g.z);
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
  if (e.key === 'r' || e.key === 'R') {
    ghostRotation += Math.PI / 2;
    if (ghost) ghost.rotation.y = ghostRotation;
    if (selected) selected.rotation.y += Math.PI / 2;
  } else if (e.key === 'Escape') {
    disarm(); deselect();
  } else if (e.key === 'Delete' || e.key === 'Backspace') {
    if (selected) { scene.remove(selected); placed.splice(placed.indexOf(selected), 1); selected = null; setStatus('Piece removed.'); }
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
