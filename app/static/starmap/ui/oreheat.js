// ═══════════════════════════════════════════════════════════════════
//  Ore Concentrations panel — master on/off toggle + per-ore checkboxes
//  (all unchecked by default). Owns the selection state; tells the map to
//  rebuild the heat-disk overlay via the onChange callback. Pure DOM.
//
//  The panel lives in the page sidebar in normal document flow, so it needs no
//  manual positioning — the old relayout() that docked it beneath the Set
//  Position panel was removed when the map moved out of fullscreen.
// ═══════════════════════════════════════════════════════════════════

import { ORE_LIST } from '../scene/oreheat.js';

const $ = id => document.getElementById(id);

let cb = null;
let enabled = false;
const selected = new Set();

// Effective selection handed to the map: nothing while the master toggle is off.
function activeSelection() {
  return enabled ? selected : new Set();
}

function emit() {
  if (cb && cb.onChange) cb.onChange(activeSelection());
}

function buildList() {
  const list = $('ore-list');
  list.innerHTML = '';
  for (const ore of ORE_LIST) {
    const row = document.createElement('div');
    row.className = 'ore-row';
    row.innerHTML = `<span class="ore-box"></span><span>${ore}</span>`;
    row.addEventListener('click', () => {
      const on = !selected.has(ore);
      if (on) selected.add(ore); else selected.delete(ore);
      row.classList.toggle('checked', on);
      emit();
    });
    list.appendChild(row);
  }
}

// The master switch doubles as the disclosure control for the legend + ore list:
// #ore-panel.on is what the stylesheet keys the expansion off, so the list is
// hidden until the overlay is switched on rather than shown in a dimmed,
// non-interactive state.
function setEnabled(on) {
  enabled = on;
  const toggle = $('ore-toggle');
  toggle.classList.toggle('active', on);
  toggle.setAttribute('aria-checked', String(on));
  toggle.setAttribute('aria-expanded', String(on));
  $('ore-panel').classList.toggle('on', on);
  emit();
}

export function initOreHeat(callbacks) {
  cb = callbacks;
  buildList();

  const toggle = $('ore-toggle');
  toggle.addEventListener('click', () => setEnabled(!enabled));
  toggle.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault();          // stop Space from scrolling the sidebar
      setEnabled(!enabled);
    }
  });

  return {
    // Re-emit the current selection so the map can rebuild for a new system.
    refresh: emit,
  };
}
