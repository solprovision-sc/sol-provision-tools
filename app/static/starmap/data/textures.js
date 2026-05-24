// ═══════════════════════════════════════════════════════════════════
//  TEXTURES — manifest-driven body texture loader.
//
//  Source of truth is C:\Projects\sol-provision-tools\app\static\starmap\textures\manifest.json,
//  produced by C:\Projects\star-citizen-tools\extractor\extract_starmap_textures.py.
//
//  Texture conventions (per body):
//    *_starmap_diff or *_global_starmap_diff  → pre-lit orbital composite (use MeshBasicMaterial)
//    *_clim + optional *_ddn                  → raw albedo + normal (use MeshStandardMaterial)
//
//  If a body has neither, the procedural-color sphere built by the scene module stays.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

const TEXTURE_BASE = '/static/starmap/textures';
const MANIFEST_URL = `${TEXTURE_BASE}/manifest.json`;

// Suffixes considered "pre-lit composites" — these get an unlit material.
const PRELIT_SUFFIXES = new Set(['starmap_diff', 'global_starmap_diff']);
// Suffixes that contain a global cloud opacity mask (extractor collapses CIG's
// inconsistent naming into whatever is shipped per body).
const CLOUD_SUFFIXES  = new Set(['clouds_global', 'cloud_global', 'cloud_global_property']);

let manifestPromise = null;
const texCache = new Map();
const loader = new THREE.TextureLoader();

export async function loadManifest() {
  if (!manifestPromise) {
    manifestPromise = fetch(MANIFEST_URL)
      .then(r => r.ok ? r.json() : null)
      .catch(err => { console.warn('starmap: manifest fetch failed', err); return null; });
  }
  return manifestPromise;
}

function loadTex(path, isColor) {
  if (!path) return Promise.resolve(null);
  if (texCache.has(path)) return Promise.resolve(texCache.get(path));
  return new Promise(resolve => {
    loader.load(
      path,
      tex => {
        tex.colorSpace = isColor ? THREE.SRGBColorSpace : THREE.NoColorSpace;
        tex.anisotropy = 8;
        tex.flipY      = false;
        tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
        texCache.set(path, tex);
        resolve(tex);
      },
      undefined,
      () => { console.warn(`Texture missing: ${path}`); resolve(null); }
    );
  });
}

function texPath(system, slug, filename) {
  return `${TEXTURE_BASE}/${system}/${slug}/${filename}`;
}

// Returns { prelit, clim, ddn, clouds } — any of which may be null.
export async function loadBodyTextures(bodyId) {
  const manifest = await loadManifest();
  if (!manifest) return { prelit:null, clim:null, ddn:null, clouds:null };

  const entry = manifest.bodies[String(bodyId)];
  if (!entry) return { prelit:null, clim:null, ddn:null, clouds:null };

  const { system, slug, textures } = entry;

  // Pick pre-lit composite if available; otherwise clim+ddn.
  let prelitFile = null;
  for (const s of PRELIT_SUFFIXES) {
    if (textures[s]) { prelitFile = textures[s].filename; break; }
  }
  // Pick whichever cloud-suffix the extractor wrote for this body.
  let cloudsFile = null;
  for (const s of CLOUD_SUFFIXES) {
    if (textures[s]) { cloudsFile = textures[s].filename; break; }
  }
  const climFile = textures.clim?.filename;
  const ddnFile  = textures.ddn?.filename;

  const [prelit, clim, ddn, clouds] = await Promise.all([
    prelitFile ? loadTex(texPath(system, slug, prelitFile), true)  : Promise.resolve(null),
    climFile   ? loadTex(texPath(system, slug, climFile),   true)  : Promise.resolve(null),
    ddnFile    ? loadTex(texPath(system, slug, ddnFile),    false) : Promise.resolve(null),
    cloudsFile ? loadTex(texPath(system, slug, cloudsFile), false) : Promise.resolve(null),
  ]);
  return { prelit, clim, ddn, clouds };
}

// Replace a mesh's procedural material with a textured one, picking the right
// material type based on whether we have a pre-lit composite or raw albedo+normal.
export function applyTexturesToMesh(mesh, { prelit, clim, ddn }) {
  if (prelit) {
    mesh.material.dispose();
    mesh.material = new THREE.MeshBasicMaterial({ map: prelit });
  } else if (clim) {
    mesh.material.dispose();
    const params = { map: clim, roughness: 0.88, metalness: 0.02 };
    if (ddn) {
      params.normalMap   = ddn;
      params.normalScale = new THREE.Vector2(1, -1); // DX → GL Y flip
    }
    mesh.material = new THREE.MeshStandardMaterial(params);
  }
  mesh.material.needsUpdate = true;
}
