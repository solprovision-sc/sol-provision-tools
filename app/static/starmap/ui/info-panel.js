// ═══════════════════════════════════════════════════════════════════
//  Info panel — populates and slides in body details on selection.
// ═══════════════════════════════════════════════════════════════════

const FIELDS = [
  ['i-desig',   b => b.designation || '—'],
  ['i-type',    b => b.subtype || b.type || '—'],
  ['i-parent',  (b, parentName) => parentName],
  ['i-dist',    b => b.dist > 0 ? b.dist.toFixed(4) + ' AU' : '—'],
  ['i-period',  b => b.period ? b.period.toFixed(2) + ' days' : '—'],
  ['i-size',    b => b.size > 1 ? Math.round(b.size) + ' km'
                   : b.size > 0 ? (b.size * 100).toFixed(0) + ' (rel)'
                   : '—'],
  ['i-faction', b => b.faction || '—'],
];

export function initInfoClose(onClose = closeInfo) {
  document.getElementById('ipclose').addEventListener('click', onClose);
}

export function showInfo(body, parentName = 'None') {
  document.getElementById('ipt').textContent    = body.name || body.designation || '—';
  document.getElementById('iptype').textContent = (body.subtype || body.type || '—').toUpperCase();
  for (const [id, fn] of FIELDS) {
    document.getElementById(id).textContent = fn(body, parentName);
  }
  document.getElementById('idesc').textContent = body.description || '—';
  document.getElementById('info-panel').classList.add('visible');
}

export function closeInfo() {
  document.getElementById('info-panel').classList.remove('visible');
}
