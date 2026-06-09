// Star body. Lightweight: a bright unlit sphere + a soft back-side halo + the
// system's point light + a faint ambient so unlit faces aren't pitch black.
//
// Real bloom (post-FX) does the heavy lifting of "this looks luminous."

import * as THREE from 'three';
import { makeLabel } from '../util/label.js';
import { makeStarHalo } from './primitives.js';

// Small, intense disc — a star reads as a hot point at this scale, not a big
// sphere. Bloom turns the over-bright white core into a tight crisp flare.
const STAR_RENDER_RADIUS = 34;

export function addStar(world, starData, systemConfig) {
  const r = STAR_RENDER_RADIUS;

  // White-hot core: a real photosphere is near-white; the system tint lives in
  // the halo, not the disc. Pushing the core toward white keeps its luminance
  // above the bloom threshold for EVERY system (incl. ember Pyro), so the star
  // always blooms into a small intense glint instead of a flat coloured coin.
  const core = new THREE.Color(systemConfig.starColor).lerp(new THREE.Color(0xffffff), 0.8);

  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(r, 48, 48),
    new THREE.MeshBasicMaterial({ color: core }),
  );
  mesh.userData = { bodyId: starData.id };
  world.scene.add(mesh);
  world.sceneObjects.push(mesh);
  world.meshes.push(mesh);
  world.bodyIndex[starData.id].mesh         = mesh;
  world.bodyIndex[starData.id].renderRadius = r;

  // A thin back-side halo carries the system colour as a soft edge; bloom on the
  // white core does the luminous work. No corona sprite — a big additive glow
  // makes the star read as an oversized cartoon sun, not the tight Arkmap point.
  const halo = makeStarHalo(r, systemConfig.starColor, 0.18);
  world.scene.add(halo);
  world.sceneObjects.push(halo);

  const light = new THREE.PointLight(systemConfig.starGlow, 1.6, 6000);
  world.scene.add(light);
  world.sceneObjects.push(light);

  // Fill so the night side of texture-less procedural bodies (e.g. Pyro V, the
  // Nyx planets, which have no game texture) reads as their colour instead of
  // black. Pre-lit (unlit) bodies ignore lights, so this only lifts the bodies
  // that need it. Star-tinted sky, near-black ground for a hint of direction.
  const fill = new THREE.HemisphereLight(systemConfig.starGlow, 0x080b12, 0.55);
  world.scene.add(fill);
  world.sceneObjects.push(fill);

  const ambient = new THREE.AmbientLight(0x20262e, 0.7);
  world.scene.add(ambient);
  world.sceneObjects.push(ambient);

  const label = makeLabel(starData.name, systemConfig.starColor, 22);
  label.position.set(r + 12, r + 10, 0);
  label.userData = { type: 'label', bodyType: 'STAR' };
  world.scene.add(label);
  world.sceneObjects.push(label);
  world.labelSprites.push(label);
}
