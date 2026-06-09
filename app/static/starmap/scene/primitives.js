// Reusable scene-graph primitives shared by the body-type builders.

import * as THREE from 'three';

// Glowing orbit ring. Additive blending + a per-vertex brightness sweep gives
// each ring a soft "comet head" gradient — brightest along one arc, fading to a
// faint trace opposite — so the rings read as luminous holographic tracks
// rather than flat wireframe circles. `opacity` sets the peak brightness.
export function makeOrbitRing(radius, color = 0x224466, opacity = 0.3, segments = 128) {
  const base = new THREE.Color(color);
  const pts  = new Float32Array((segments + 1) * 3);
  const cols = new Float32Array((segments + 1) * 3);
  const head = Math.PI * 0.5;   // angle of the brightest arc
  for (let i = 0; i <= segments; i++) {
    const a = (i / segments) * Math.PI * 2;
    pts[i*3]   = Math.cos(a) * radius;
    pts[i*3+1] = 0;
    pts[i*3+2] = Math.sin(a) * radius;
    const sweep = 0.28 + 0.72 * (0.5 + 0.5 * Math.cos(a - head));
    const b = opacity * sweep;
    cols[i*3]   = base.r * b;
    cols[i*3+1] = base.g * b;
    cols[i*3+2] = base.b * b;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pts, 3));
  geo.setAttribute('color',    new THREE.BufferAttribute(cols, 3));
  return new THREE.Line(geo, new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent:  true,
    opacity:      1,
    blending:     THREE.AdditiveBlending,
    depthWrite:   false,
  }));
}

// Thin halo so the star reads as luminous even when the bloom pass is dialed low.
export function makeStarHalo(radius, colorHex, opacity = 0.12) {
  const geo = new THREE.SphereGeometry(radius * 1.3, 24, 24);
  const mat = new THREE.MeshBasicMaterial({
    color: parseInt(colorHex.replace('#', '0x'), 16),
    transparent: true,
    opacity,
    side: THREE.BackSide,
    depthWrite: false,
  });
  return new THREE.Mesh(geo, mat);
}

// Radial-gradient texture for additive glow sprites (coronae, flares). A fresh
// one per sprite: clearScene() disposes each material's .map on system switch,
// so a shared/cached texture would be torn out from under the next build.
function glowTexture() {
  const size = 256, c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(size/2, size/2, 0, size/2, size/2, size/2);
  g.addColorStop(0.0, 'rgba(255,255,255,1.0)');
  g.addColorStop(0.18, 'rgba(255,255,255,0.85)');
  g.addColorStop(0.42, 'rgba(255,255,255,0.30)');
  g.addColorStop(0.72, 'rgba(255,255,255,0.07)');
  g.addColorStop(1.0, 'rgba(255,255,255,0.0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// Billboarded additive glow disc. `diameter` is in world units. depthTest:false
// lets a core glow paint over its own body; true makes a wide halo respect
// foreground geometry so it doesn't wash over closer planets.
export function makeGlowSprite(colorHex, diameter, opacity = 1, depthTest = true) {
  const mat = new THREE.SpriteMaterial({
    map:         glowTexture(),
    color:       parseInt(colorHex.replace('#', '0x'), 16),
    transparent: true,
    opacity,
    blending:    THREE.AdditiveBlending,
    depthWrite:  false,
    depthTest,
  });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(diameter, diameter, 1);
  return sprite;
}
