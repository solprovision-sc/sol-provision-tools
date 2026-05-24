// Tiny helpers for the #RRGGBB → 0xRRGGBB conversions sprinkled across scene modules.

export function hexToInt(hex) {
  return parseInt(hex.replace('#', '0x'), 16);
}
