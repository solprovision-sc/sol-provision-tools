// ═══════════════════════════════════════════════════════════════════
//  "Set your position" panel.
//
//  Two modes:
//   • At a location — pick a system + a known location (planet/moon/
//     station/L-point/OM) → marker dropped at its real coordinates.
//   • Triangulate — pick 3 references + distances → 2D trilateration
//     solves the real position; marker dropped there with a fix-quality
//     readout.
//
//  Owns its DOM; talks to the map only through the callbacks passed to
//  initPosition(). The orchestrator converts the real (x,y) km we hand it
//  into a scene position and manages the marker.
// ═══════════════════════════════════════════════════════════════════

import { trilaterate2D } from '../util/trilaterate.js';

const UNIT_KM = { Gm: 1e6, Mm: 1e3, km: 1 };
const TRI = '__triangulate__';

let cb = null;
let locations = [];          // current system's selectable locations

const $ = id => document.getElementById(id);

// Build <optgroup>s grouped by parent body. `placeholder` is the first option.
function fillLocationSelect(sel, placeholder, withTriangulate) {
  sel.innerHTML = '';
  const ph = new Option(placeholder, '');
  ph.disabled = false; sel.add(ph);
  if (withTriangulate) sel.add(new Option('⊕ Triangulate my position…', TRI));

  const groups = new Map();
  locations.forEach((loc, i) => {
    const g = loc.parentName || '—';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(i);
  });
  for (const [g, idxs] of groups) {
    const og = document.createElement('optgroup');
    og.label = g;
    for (const i of idxs) og.appendChild(new Option(locations[i].name, String(i)));
    sel.appendChild(og);
  }
}

function refRows() {
  return [1, 2, 3].map(n => ({
    body: $(`pos-ref-body-${n}`),
    dist: $(`pos-ref-dist-${n}`),
    unit: $(`pos-ref-unit-${n}`),
  }));
}

function fmtDist(km) {
  if (km >= 1e6) return (km / 1e6).toFixed(3) + ' Gm';
  if (km >= 1e3) return (km / 1e3).toFixed(1) + ' Mm';
  return Math.round(km) + ' km';
}

function setStatus(msg, cls = '') {
  const el = $('pos-status');
  el.textContent = msg || '';
  el.className = cls;
  $('pos-clear').hidden = !msg;
}

function showTriangulate(on) {
  $('pos-triangulate').hidden = !on;
  if (on) { $('pos-result').textContent = ''; $('pos-result').className = ''; }
}

function onLocationChange() {
  const v = $('pos-location').value;
  if (v === TRI) { showTriangulate(true); return; }
  showTriangulate(false);
  if (v === '') { return; }
  const loc = locations[Number(v)];
  if (!loc || !loc.helio) return;
  cb.onPlace(loc.helio[0], loc.helio[1], loc.name);
  setStatus(`Position set: ${loc.name}`, 'ok');
}

function solve() {
  const rows = refRows();
  const refs = [];
  const used = new Set();
  for (const r of rows) {
    const idx = r.body.value;
    if (idx === '') { setStatus('Pick all three reference bodies.', 'warn'); return; }
    if (used.has(idx)) { setStatus('Reference bodies must be different.', 'warn'); return; }
    used.add(idx);
    const loc = locations[Number(idx)];
    const val = parseFloat(r.dist.value);
    if (!(val > 0)) { setStatus('Enter a positive distance for each reference.', 'warn'); return; }
    const km = val * (UNIT_KM[r.unit.value] || 1);
    refs.push({ x: loc.helio[0], y: loc.helio[1], d: km });
  }

  const res = trilaterate2D(refs);
  const out = $('pos-result');
  if (!res.ok) {
    out.className = 'warn';
    out.textContent = res.warnings.join(' ');
    return;
  }
  cb.onPlace(res.x, res.y, 'Triangulated position');

  const qcls = res.quality === 'good' ? 'ok' : res.quality === 'fair' ? 'warn' : 'bad';
  const lines = [
    `Fix: ${res.quality.toUpperCase()} · residual ±${fmtDist(res.residualKm)}`,
  ];
  if (res.zAbs > res.residualKm) lines.push(`Height off plane ≈ ${fmtDist(res.zAbs)} (above/below unknown)`);
  for (const w of res.warnings) lines.push('⚠ ' + w);
  out.className = qcls;
  out.innerHTML = lines.map(l => `<div>${l}</div>`).join('');
  setStatus('Position set: triangulated', 'ok');
}

// Repopulate every location-bearing select for the active system.
function populate() {
  fillLocationSelect($('pos-location'), '— Select location —', true);
  for (const r of refRows()) fillLocationSelect(r.body, '— Reference —', false);
  showTriangulate(false);
}

export function initPosition(callbacks) {
  cb = callbacks;

  $('pos-system').addEventListener('change', async e => {
    setStatus('');
    await cb.onSwitchSystem(e.target.value);   // map switch + reload happens via setSystem()
  });
  $('pos-location').addEventListener('change', onLocationChange);
  $('pos-solve').addEventListener('click', solve);
  $('pos-clear').addEventListener('click', () => {
    cb.onClear();
    $('pos-location').value = '';
    showTriangulate(false);
    setStatus('');
  });

  return {
    // Called by the orchestrator whenever the active system changes (top
    // selector or panel) so the panel + its dropdowns stay in sync.
    async setSystem(systemKey) {
      $('pos-system').value = systemKey;
      locations = (await cb.getLocations(systemKey)) || [];
      populate();
      setStatus('');
    },
  };
}
