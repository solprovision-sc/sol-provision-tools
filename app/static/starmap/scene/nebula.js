// ═══════════════════════════════════════════════════════════════════
//  Backdrop nebula — a huge inward-facing sphere with a procedural fbm
//  cloud field tinted by the system accent. Replaces the flat black void
//  with faint depth and colour, the way the Arkmap fades space into a
//  soft coloured haze. Sits outside the starfield shell so points read in
//  front of it. Rebuilt per system so the tint follows the active theme.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

const RADIUS = 42000;   // > starfield shell (10k–25k), < camera far (80k)

const VERT = /* glsl */`
  varying vec3 vDir;
  void main() {
    vDir = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

// 3D value noise + fbm, lifted into a domain-warped two-tone cloud field —
// warping is what gives procedural nebulae their filamentary, photographed feel
// instead of the blobby look of plain fbm.
const FRAG = /* glsl */`
  precision highp float;
  uniform vec3 uAccent;
  uniform vec3 uAccent2;
  varying vec3 vDir;

  float hash(vec3 p) {
    p = fract(p * 0.3183099 + 0.1);
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float vnoise(vec3 x) {
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash(i + vec3(0,0,0)), hash(i + vec3(1,0,0)), f.x),
          mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
      mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x),
          mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
  }
  float fbm(vec3 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 5; i++) { v += a * vnoise(p); p *= 2.03; a *= 0.5; }
    return v;
  }

  void main() {
    vec3 d = normalize(vDir);
    vec3 p = d * 2.2;

    // Domain warp: displace the sample point by a low-freq noise vector, then
    // sample again — folds the clouds into stretched filaments and voids.
    vec3 q = vec3(fbm(p + vec3(0.0)),
                  fbm(p + vec3(3.1, 1.7, 8.2)),
                  fbm(p + vec3(6.3, 9.1, 2.4)));
    float base = fbm(p + 3.2 * q);
    float fine = fbm(p * 3.1 + 7.4);
    float dens = pow(clamp(base * 0.9 + fine * 0.35, 0.0, 1.0), 2.3);

    // Mix two tints across the field so it isn't a flat monochrome wash.
    float band     = smoothstep(0.25, 0.85, fbm(p * 0.7 + 21.0));
    vec3  cloudCol = mix(uAccent, uAccent2, band);

    // Very dark base; faint clouds; a few brighter cores; subtle horizon lift.
    vec3  baseDark = vec3(0.003, 0.006, 0.012);
    float horizon  = smoothstep(0.6, 0.0, abs(d.y)) * 0.03;

    vec3 col = baseDark
             + cloudCol * dens * 0.16
             + cloudCol * pow(dens, 4.0) * 0.10
             + uAccent  * horizon;

    gl_FragColor = vec4(col, 1.0);
  }
`;

// secondary tint: the accent rotated toward violet, so the field reads as a
// two-colour nebula (cyan↔blue-violet for Stanton, etc.) rather than one hue.
function secondaryTint(accentHex) {
  return new THREE.Color(accentHex).lerp(new THREE.Color('#7a4dff'), 0.55);
}

export function makeNebula(accentHex = '#3fe0ff') {
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uAccent:  { value: new THREE.Color(accentHex) },
      uAccent2: { value: secondaryTint(accentHex) },
    },
    vertexShader:   VERT,
    fragmentShader: FRAG,
    side:           THREE.BackSide,
    depthWrite:     false,
  });
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(RADIUS, 32, 24), mat);
  mesh.userData = { type: 'nebula' };
  mesh.renderOrder = -2;   // draw before everything
  return mesh;
}
