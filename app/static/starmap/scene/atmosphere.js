// ═══════════════════════════════════════════════════════════════════
//  Atmosphere rim glow — the signature "Arkmap" planet limb.
//
//  A slightly larger back-side shell whose opacity follows a Fresnel
//  term (bright at the silhouette edge, transparent face-on). Additive
//  blending makes it read as scattered light bleeding past the limb.
//
//  The shell is parented to the body mesh, so it inherits position and
//  follows the body during orbit animation with no per-frame bookkeeping.
//  It is still pushed into world.sceneObjects so clearScene() disposes it.
// ═══════════════════════════════════════════════════════════════════

import * as THREE from 'three';

const VERT = /* glsl */`
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vView  = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;

const FRAG = /* glsl */`
  uniform vec3  uColor;
  uniform float uIntensity;
  uniform float uPower;
  varying vec3  vNormal;
  varying vec3  vView;
  void main() {
    // Back-side shell: normals face the camera, so a low dot = limb.
    float fres = pow(1.0 - abs(dot(vNormal, vView)), uPower);
    gl_FragColor = vec4(uColor, fres * uIntensity);
  }
`;

// Per-subtype atmosphere tint + thickness. Gas/ice giants get a fat, vivid
// halo; rocky/airless bodies get a thin, faint one so they still pop off the
// black without pretending to have air.
const ATMO = {
  'Gas Giant':         { mult: 1.22, intensity: 0.48, power: 2.6, tint: null  },
  'Ice Giant':         { mult: 1.20, intensity: 0.42, power: 2.8, tint: '#9fd8ff' },
  'Super-Earth':       { mult: 1.16, intensity: 0.40, power: 3.0, tint: '#6fb4ff' },
  'Smog Planet':       { mult: 1.18, intensity: 0.42, power: 2.6, tint: '#d8c27a' },
  'Terrestrial Rocky': { mult: 1.12, intensity: 0.24, power: 3.4, tint: '#bcdcff' },
  'Protoplanet':       { mult: 1.10, intensity: 0.16, power: 3.6, tint: null  },
  'Moon':              { mult: 1.09, intensity: 0.15, power: 3.8, tint: null  },
  default:             { mult: 1.14, intensity: 0.28, power: 3.0, tint: '#9fd8ff' },
};

// blend two hex/Color-ish values: mix the body colour with the atmo tint so
// the halo keeps a hint of the planet but trends toward a believable sky hue.
function atmoColor(bodyColorInt, tintHex) {
  const base = new THREE.Color(bodyColorInt);
  if (!tintHex) return base.lerp(new THREE.Color(0xffffff), 0.25);
  return base.lerp(new THREE.Color(tintHex), 0.6);
}

/**
 * @param world          shared scene state (for sceneObjects disposal tracking)
 * @param bodyMesh       the planet/moon mesh to wrap
 * @param radius         body render radius
 * @param bodyColorInt   0xRRGGBB of the body (procedural colour)
 * @param subtype        body subtype key into ATMO (or 'Moon')
 */
export function addAtmosphere(world, bodyMesh, radius, bodyColorInt, subtype) {
  const cfg = ATMO[subtype] || ATMO.default;

  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uColor:     { value: atmoColor(bodyColorInt, cfg.tint) },
      uIntensity: { value: cfg.intensity },
      uPower:     { value: cfg.power },
    },
    vertexShader:   VERT,
    fragmentShader: FRAG,
    side:        THREE.BackSide,
    blending:    THREE.AdditiveBlending,
    transparent: true,
    depthWrite:  false,
  });

  const shell = new THREE.Mesh(new THREE.SphereGeometry(radius * cfg.mult, 48, 48), mat);
  shell.userData = { type: 'atmosphere' };
  bodyMesh.add(shell);              // inherits the body's world position
  world.sceneObjects.push(shell);  // so clearScene() disposes geo + material
  return shell;
}
