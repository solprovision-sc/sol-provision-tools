// Canvas-sprite text labels. Billboarded against the camera by the loop.
// 256x48 is enough for the readable sizes we use; raise the canvas if labels start to look soft.

import * as THREE from 'three';

export function makeLabel(text, color = '#00ff88', fontSize = 22) {
  const canvas = document.createElement('canvas');
  canvas.width = 256;
  canvas.height = 48;
  const ctx = canvas.getContext('2d');
  ctx.font = `${fontSize}px "Share Tech Mono", monospace`;
  ctx.fillStyle = color;
  ctx.textAlign = 'left';
  ctx.fillText(text.toUpperCase(), 4, fontSize + 4);

  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(80, 15, 1);
  return sprite;
}
