// ═══════════════════════════════════════════════════════════════════
//  Ore Concentrations panel — master on/off toggle + per-ore checkboxes
//  (all unchecked by default). Owns the selection state; tells the map to
//  rebuild the heat-disk overlay via the onChange callback. Pure DOM.
//
//  The panel is fixed-positioned directly under the Set Position panel; its
//  top is recomputed from that panel's live height (it grows/shrinks as the
//  coords entry opens), so the two stay stacked without overlapping.
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

function setEnabled(on) {
  enabled = on;
  $('ore-toggle').classList.toggle('active', on);
  $('ore-panel').classList.toggle('on', on);
  emit();
}

// Keep the panel docked beneath the Set Position panel, and cap the ore list so
// it scrolls within the remaining space above the bottom of the viewport.
function relayout() {
  const above = $('position-panel');
  const me = $('ore-panel');
  if (!above || !me) return;
  me.style.top = `${Math.round(above.getBoundingClientRect().bottom + 12)}px`;

  const list = $('ore-list');
  const avail = Math.max(100, window.innerHeight - 26 - list.getBoundingClientRect().top - 16);
  list.style.maxHeight = `${Math.round(avail)}px`;
}

export function initOreHeat(callbacks) {
  cb = callbacks;
  buildList();

  $('ore-toggle').addEventListener('click', () => setEnabled(!enabled));

  relayout();
  window.addEventListener('resize', relayout);
  // The Set Position panel changes height (coords entry, status/clear rows);
  // watch it so we re-dock when it does.
  if (window.ResizeObserver) {
    new ResizeObserver(relayout).observe($('position-panel'));
  }

  return {
    relayout,
    // Re-emit the current selection so the map can rebuild for a new system.
    refresh: emit,
  };
}
