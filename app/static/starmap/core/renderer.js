// ═══════════════════════════════════════════════════════════════════
//  Renderer + post-FX pipeline (bloom, ACES tonemap, sRGB output).
//  Returns the WebGLRenderer, an EffectComposer wrapping it, and the
//  bloom pass so callers can tweak strength/threshold at runtime.
//
//  Bloom is distance-modulated by tuneBloom() — at default zoom the star
//  glows; close-in zoom dials strength down so the star doesn't wash
//  out the rest of the scene.
// ═══════════════════════════════════════════════════════════════════

import * as THREE                from 'three';
import { EffectComposer }        from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass }            from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass }       from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass }            from 'three/addons/postprocessing/OutputPass.js';

export function createRenderer(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 1);
  renderer.outputColorSpace    = THREE.SRGBColorSpace;
  renderer.toneMapping         = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  container.appendChild(renderer.domElement);

  return { renderer };
}

export function createComposer(renderer, scene, camera) {
  const composer  = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloomPass = new UnrealBloomPass(
    new THREE.Vector2(window.innerWidth, window.innerHeight),
    BLOOM_MAX_STRENGTH,
    0.4,    // radius — tighter, so the star flares to a crisp point not a smear
    0.9,    // threshold — the white-hot star core + jump-marker emissives bloom
  );
  composer.addPass(bloomPass);
  composer.addPass(new OutputPass());
  return { composer, bloomPass };
}

// Distance-modulated bloom. Spherical r ranges 60..12000; we ramp strength
// linearly from BLOOM_MIN at NEAR_R to BLOOM_MAX at FAR_R so close zoom
// doesn't smear the planets in front of the star.
const BLOOM_MIN_STRENGTH = 0.06;
const BLOOM_MAX_STRENGTH = 0.34;
const NEAR_R = 120;
const FAR_R  = 1000;

export function tuneBloom(bloomPass, cameraDistance) {
  const t = Math.min(1, Math.max(0, (cameraDistance - NEAR_R) / (FAR_R - NEAR_R)));
  bloomPass.strength = BLOOM_MIN_STRENGTH + (BLOOM_MAX_STRENGTH - BLOOM_MIN_STRENGTH) * t;
}

export function attachResize(renderer, composer, camera) {
  window.addEventListener('resize', () => {
    const w = window.innerWidth, h = window.innerHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    composer.setSize(w, h);
  });
}
