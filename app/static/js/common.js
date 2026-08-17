/* ── Sol Provision — Shared JS Utilities ─────────────────────────────────── */

// ── API helpers ──────────────────────────────────────────────────────────────
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ── Formatters ───────────────────────────────────────────────────────────────
function cleanName(s) {
  if (!s) return '—';
  return s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function cleanCareer(s) {
  if (!s) return '';
  return s.replace('@vehicle_focus_', '').replace('@vehicle_class_', '')
          .replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function cleanRole(s) {
  if (!s) return '';
  return s.replace('@vehicle_class_', '').replace('@vehicle_role_', '')
          .replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function getMfr(entityName) {
  const map = {
    aegs:'Aegis', anvl:'Anvil', argo:'Argo', banu:'Banu', cnou:'C.O.',
    crus:'Crusader', drak:'Drake', espr:'Esperia', gama:'Gatac',
    grin:'Greycat', krig:'Kruger', misc:'MISC', mrai:'Mirai',
    orig:'Origin', rsi:'RSI', tmbl:'Tumbril', vncl:'Vanduul', xian:"Xi'an",
    behr:'Behring', klwe:'Klaus & Werner', mnsr:'Joker', apar:'Apocalypse',
    taln:'Talon', recl:'MISC', csin:'Preacher', vkrn:'Vanduul',
    acas:'Castra', acom:'Achilles', amrs:'Armistice', basl:'Basilisk',
    cool:'Joker', clda:'CLDA', cdas:'CDS',
  };
  const prefix = (entityName || '').split('_')[0].toLowerCase();
  return map[prefix] || prefix.toUpperCase();
}

function fmt(val, decimals = 0, unit = '') {
  if (val == null || val === '') return '—';
  const n = parseFloat(val);
  if (isNaN(n)) return String(val);
  return n.toFixed(decimals) + (unit ? ` ${unit}` : '');
}

function fmtStat(val, decimals = 0, unit = '') {
  if (val == null || val === '') return '<span class="stat null">—</span>';
  const n = parseFloat(val);
  if (isNaN(n)) return `<span class="stat">${val}</span>`;
  return `<span class="stat">${n.toFixed(decimals)}<span class="stat-unit">${unit}</span></span>`;
}

function careerPillClass(career) {
  const c = (career || '').toLowerCase();
  if (c.includes('transport') || c.includes('freight')) return 'pill-transporter';
  if (c.includes('support') || c.includes('medical'))  return 'pill-support';
  if (c.includes('combat') || c.includes('intercept') || c.includes('bomber')) return 'pill-combat';
  if (c.includes('explor') || c.includes('recon'))     return 'pill-exploration';
  if (c.includes('mining'))   return 'pill-mining';
  if (c.includes('salvage'))  return 'pill-salvage';
  return 'pill-default';
}

function sizeBadge(size, label) {
  const cls = size ? `s${size}` : '';
  return `<span class="size-badge ${cls}">${label || (size ? 'S'+size : '?')}</span>`;
}

// ── Logo HTML ─────────────────────────────────────────────────────────────────
function logoHTML() {
  return `
    <a href="/" class="logo">
      <div class="logo-mark">
        <img src="/brand/img/Circular_Badge_Logo_White_1080x1080.png" alt="Sol Provision" />
      </div>
      <div>
        <div class="logo-text">Sol Provision</div>
        <div class="logo-sub">Data Intelligence</div>
      </div>
    </a>`;
}

// ── Nav HTML ──────────────────────────────────────────────────────────────────
// userInfo is optional. When provided and the user is rank >= 5, we append the
// officer-only "HQ" link. Anyone else gets the normal nav.
function navHTML(active, userInfo) {
	const links = [
	{ href: '/',              label: 'Dashboard' },
	//{ href: '/ships',         label: 'Ships' },
	// { href: '/components',    label: 'Components' },
	// { href: '/weapons/ship',  label: 'Ship Weapons' },
	// { href: '/weapons/fps',   label: 'FPS Weapons' },
	// { href: '/armor',         label: 'Armor' },
	// { href: '/shops',         label: 'Shops' },
	//{ href: '/crafting', 	  label: 'Crafting'},
	//{ href: '/mission-rep',   label: 'Mission Rep' },
	//{ href: '/mining-signatures', label: 'Mining Sigs' },
	//{ href: '/cargo-planner', label: 'Cargo Planner' },
	// { href: '/starmap',       label: 'Star Map' },       // ← ADD THIS
	//{ href: '/ledger', label: 'Ledger' },
	//{ href: '/item_collection',    label: 'Item Collection' },
	//{ href: '/base-builder',  label: 'Base Builder' },
	];
  // Rank may be int or string depending on the snapshot row; coerce defensively
  // so a stringy "5" still grants access. The server enforces the same gate.
  const rankRaw = userInfo && userInfo.rank;
  const rankN = rankRaw == null ? 0 : (parseInt(rankRaw, 10) || 0);
  if (rankN >= 5) {
    links.push({ href: '/officers', label: 'HQ' });
  }
  return `<nav class="section-nav">` +
    links.map(l =>
      `<a href="${l.href}" class="nav-link ${l.href === active ? 'active' : ''}">${l.label}</a>`
    ).join('') + `</nav>`;
}

// ── Shared header render ──────────────────────────────────────────────────────
async function renderHeader(activePage) {
  try {
    const meta = await api('/api/meta');
    
    // Check if user is logged in
    let userInfo = null;
    try {
      userInfo = await api('/api/auth/me');
    } catch (e) {
      // Not logged in
    }
    
    document.getElementById('header-logo').innerHTML = logoHTML();
    document.getElementById('header-nav').innerHTML  = navHTML(activePage, userInfo);

    // Right side: show user info or patch info
    if (userInfo) {
      let rankDisplay = '';
      
      // Check for CEO (Sulyce)
      if (userInfo.callsign === 'Sulyce') {
        rankDisplay = 'CEO';
      } 
      // Check for Officer (Rank 5)
      else if (userInfo.rank === 5) {
        rankDisplay = `${userInfo.division || 'Unassigned'} · Officer`;
      }
      // Check for Wing Commander (Rank 4)
      else if (userInfo.rank === 4) {
        rankDisplay = `${userInfo.division || 'Unassigned'} · Wing Commander`;
      }
      // Default: show division and rank number
      else {
        rankDisplay = `${userInfo.division || 'Unassigned'} · Rank ${userInfo.rank}`;
      }
      
      document.getElementById('header-meta').innerHTML = `
        <div class="user-info">
          <div class="user-callsign">${userInfo.callsign}</div>
          <div class="user-rank">${rankDisplay}</div>
        </div>`;
    } else {
      document.getElementById('header-meta').innerHTML = `
        <div>Patch <span>${meta.patch_version}</span></div>
        <div>${(meta.total_ships||0).toLocaleString()} ships · ${(meta.total_entities||0).toLocaleString()} entities</div>`;
    }
    
  } catch(e) {
    document.getElementById('header-logo').innerHTML = logoHTML();
    document.getElementById('header-nav').innerHTML  = navHTML(activePage);
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(btn, tabId) {
  btn.closest('.tab-area').querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.closest('.tab-area').querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(tabId).classList.add('active');
}

// ── Compare chip list ─────────────────────────────────────────────────────────
class CompareManager {
  constructor(max = 4, onUpdate) {
    this.max = max;
    this.items = [];
    this.onUpdate = onUpdate;
  }
  toggle(item) {
    const idx = this.items.findIndex(i => i.entity_name === item.entity_name);
    if (idx >= 0) this.items.splice(idx, 1);
    else if (this.items.length < this.max) this.items.push(item);
    this.onUpdate(this.items);
  }
  has(entityName) { return this.items.some(i => i.entity_name === entityName); }
  remove(entityName) {
    this.items = this.items.filter(i => i.entity_name !== entityName);
    this.onUpdate(this.items);
  }
  clear() { this.items = []; this.onUpdate(this.items); }
  renderChips(containerId, btnId) {
    const list = document.getElementById(containerId);
    const btn  = document.getElementById(btnId);
    if (!list) return;
    list.innerHTML = this.items.map(i => `
      <div class="compare-chip">
        <span>${cleanName(i.entity_name)}</span>
        <button onclick="compareMgr.remove('${i.entity_name}')">✕</button>
      </div>`).join('');
    const count = document.getElementById('compare-count');
    if (count) count.textContent = `(${this.items.length}/${this.max})`;
    if (btn) btn.style.display = this.items.length >= 2 ? 'block' : 'none';
  }
}
