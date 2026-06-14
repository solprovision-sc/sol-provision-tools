// ═══════════════════════════════════════════════════════════════════
//  Parse the in-game `/showlocation` chat output into helio km.
//
//  The game prints system coordinates in METRES, star at origin, with
//  the ecliptic on the X/Y plane and Z the height above/below it:
//
//      Coordinates: x:-19023432445.837769 y:-2613543032.846788 z:-2189.750035
//
//  The DB helio frame (helio_x_km / helio_y_km) is the same axes in km,
//  so we divide by 1000. We're tolerant about the surrounding text: the
//  "Coordinates:" prefix is optional, separators may be ':' or '=', and a
//  bare "x y z" triple of numbers is accepted as a fallback.
// ═══════════════════════════════════════════════════════════════════

const M_PER_KM = 1000;

// Pull labelled axes (x:.. y:.. z:..) first; fall back to the first 2–3
// signed/scientific numbers in the string. Returns km {x,y,z} or null.
export function parseGameCoords(text) {
  if (!text) return null;
  const NUM = /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/;

  const labelled = {};
  const re = new RegExp(`([xyz])\\s*[:=]\\s*(${NUM.source})`, 'gi');
  let m;
  while ((m = re.exec(text)) !== null) labelled[m[1].toLowerCase()] = parseFloat(m[2]);

  let xm, ym, zm;
  if (Number.isFinite(labelled.x) && Number.isFinite(labelled.y)) {
    xm = labelled.x; ym = labelled.y; zm = Number.isFinite(labelled.z) ? labelled.z : 0;
  } else {
    const nums = (text.match(new RegExp(NUM.source, 'g')) || []).map(Number);
    if (nums.length < 2) return null;
    xm = nums[0]; ym = nums[1]; zm = nums.length >= 3 ? nums[2] : 0;
  }
  if (!Number.isFinite(xm) || !Number.isFinite(ym)) return null;

  return { x: xm / M_PER_KM, y: ym / M_PER_KM, z: zm / M_PER_KM };
}

// Compact human-readable distance for the parse echo.
export function fmtGm(km) {
  const gm = km / 1e6;
  if (Math.abs(gm) >= 1) return gm.toFixed(2) + ' Gm';
  const mm = km / 1e3;
  if (Math.abs(mm) >= 1) return mm.toFixed(1) + ' Mm';
  return Math.round(km) + ' km';
}
