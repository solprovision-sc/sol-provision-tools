// ═══════════════════════════════════════════════════════════════════
//  HUD wiring — system selector, filter toggles, orbit-motion toggle,
//  themed colour swap, header text updates. Pure DOM; no Three.js.
// ═══════════════════════════════════════════════════════════════════

export function initHud({ onSwitchSystem, onToggleFilter, onToggleOrbits }) {
  document.querySelectorAll('.sys-btn[data-system]').forEach(btn => {
    btn.addEventListener('click', () => onSwitchSystem(btn.dataset.system));
  });

  document.querySelectorAll('.filter-btn[data-type]').forEach(btn => {
    btn.addEventListener('click', () => {
      const active = !btn.classList.contains('active');
      btn.classList.toggle('active', active);
      onToggleFilter(btn.dataset.type, active);
    });
  });

  document.getElementById('orbit-toggle').addEventListener('click', onToggleOrbits);
}

const SYSTEM_THEME_CLASS = { stanton: '', pyro: 'pyro-theme', nyx: 'nyx-theme' };

export function setActiveSystem(systemKey, systemConfig) {
  document.body.classList.remove('pyro-theme', 'nyx-theme');
  const cls = SYSTEM_THEME_CLASS[systemKey];
  if (cls) document.body.classList.add(cls);

  document.getElementById('system-id').textContent  = `SYS: ${systemConfig.name} // ${systemConfig.subtitle}`;
  document.getElementById('body-count').textContent = systemConfig.planetCount;
  document.querySelectorAll('.sys-btn[data-system]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.system === systemKey);
  });
}

export function setOrbitToggle(active) {
  document.getElementById('orbit-toggle').classList.toggle('active', active);
}

// Brief overlay flash to telegraph the system switch.
export function flashTransition() {
  const flash = document.getElementById('flash');
  flash.style.opacity = '0.15';
  setTimeout(() => { flash.style.opacity = '0'; }, 200);
}
