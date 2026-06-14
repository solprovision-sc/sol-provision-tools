// ═══════════════════════════════════════════════════════════════════
//  "Set your position" panel.
//
//  Two modes:
//   • At a location — pick a system + a known location (planet/moon/
//     station/L-point/OM) → marker dropped at its real coordinates.
//   • From in-game coords — paste the `/showlocation` chat output
//     (Coordinates: x:… y:… z:…) → marker dropped at those real coords.
//
//  Owns its DOM; talks to the map only through the callbacks passed to
//  initPosition(). The orchestrator converts the real (x,y) km we hand it
//  into a scene position and manages the marker.
// ═══════════════════════════════════════════════════════════════════

import { parseGameCoords, fmtGm } from '../util/gamecoords.js';

const COORDS = '__coords__';

let cb = null;
let locations = [];          // current system's selectable locations

const $ = id => document.getElementById(id);

// Build <optgroup>s grouped by parent body. `placeholder` is the first option.
function fillLocationSelect(sel, placeholder) {
  sel.innerHTML = '';
  const ph = new Option(placeholder, '');
  ph.disabled = false; sel.add(ph);
  sel.add(new Option('⊕ Set from in-game coords…', COORDS));

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

function setStatus(msg, cls = '') {
  const el = $('pos-status');
  el.textContent = msg || '';
  el.className = cls;
  $('pos-clear').hidden = !msg;
}

function setResult(html, cls = '') {
  const el = $('pos-result');
  el.innerHTML = html || '';
  el.className = cls;
}

function showCoords(on) {
  $('pos-coords').hidden = !on;
  if (on) setResult('');
}

function onLocationChange() {
  const v = $('pos-location').value;
  if (v === COORDS) { showCoords(true); return; }
  showCoords(false);
  if (v === '') { return; }
  const loc = locations[Number(v)];
  if (!loc || !loc.helio) return;
  cb.onPlace(loc.helio[0], loc.helio[1], loc.name);
  setStatus(`Position set: ${loc.name}`, 'ok');
}

function setFromCoords() {
  const raw = $('pos-coords-input').value;
  const c = parseGameCoords(raw);
  if (!c) {
    setResult('Couldn’t read coordinates. Paste the full <b>/showlocation</b> line.', 'warn');
    return;
  }
  cb.onPlace(c.x, c.y, 'Your position');
  const zNote = Math.abs(c.z) >= 1
    ? `<div>height ${c.z >= 0 ? '+' : '−'}${fmtGm(Math.abs(c.z))} off plane</div>` : '';
  setResult(`<div>x ${fmtGm(c.x)} · y ${fmtGm(c.y)}</div>${zNote}`, 'ok');
  setStatus('Position set: in-game coords', 'ok');
}

// Repopulate the location select for the active system.
function populate() {
  fillLocationSelect($('pos-location'), '— Select location —');
  showCoords(false);
}

export function initPosition(callbacks) {
  cb = callbacks;

  $('pos-system').addEventListener('change', async e => {
    setStatus('');
    await cb.onSwitchSystem(e.target.value);   // map switch + reload happens via setSystem()
  });
  $('pos-location').addEventListener('change', onLocationChange);
  $('pos-coords-set').addEventListener('click', setFromCoords);
  $('pos-clear').addEventListener('click', () => {
    cb.onClear();
    $('pos-location').value = '';
    showCoords(false);
    setStatus('');
  });

  return {
    // Called by the orchestrator whenever the active system changes (top
    // selector or panel) so the panel + its dropdown stay in sync.
    async setSystem(systemKey) {
      $('pos-system').value = systemKey;
      locations = (await cb.getLocations(systemKey)) || [];
      populate();
      setStatus('');
    },
  };
}
