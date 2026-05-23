// ═══════════════════════════════════════════════════════════════════
//  URL state — /starmap, /starmap/<system>, /starmap/<system>/<body>.
//
//  Body slugs are derived from the body name: lowercase, spaces → dashes,
//  non-alphanumerics dropped. The reverse lookup matches body.name → slug
//  against the URL segment.
//
//  System switches and body selections push history entries so browser
//  back/forward acts as a navigation history.
// ═══════════════════════════════════════════════════════════════════

const BASE = '/starmap';

export function bodyToSlug(body) {
  if (!body || !body.name) return null;
  return body.name.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '');
}

export function findBodyBySlug(systemConfig, slug) {
  if (!systemConfig || !slug) return null;
  for (const b of systemConfig.bodies) {
    if (bodyToSlug(b) === slug) return b;
  }
  return null;
}

// Parse the current URL into { system, bodySlug }. Both may be null.
export function parseUrl() {
  const parts = window.location.pathname.split('/').filter(Boolean);
  // ["starmap", "<system>", "<body>"]
  const system   = parts[1] ? parts[1].toLowerCase() : null;
  const bodySlug = parts[2] ? parts[2].toLowerCase() : null;
  return { system, bodySlug };
}

function buildUrl(system, bodySlug) {
  if (!system) return BASE;
  return bodySlug ? `${BASE}/${system}/${bodySlug}` : `${BASE}/${system}`;
}

// pushState when the change should be in browser history (user-initiated navigations).
export function pushState(system, bodySlug) {
  const url = buildUrl(system, bodySlug);
  if (window.location.pathname !== url) {
    window.history.pushState({ system, bodySlug }, '', url);
  }
}

// replaceState for transient updates that shouldn't grow history.
export function replaceState(system, bodySlug) {
  window.history.replaceState({ system, bodySlug }, '', buildUrl(system, bodySlug));
}

export function onPopState(handler) {
  window.addEventListener('popstate', () => handler(parseUrl()));
}
