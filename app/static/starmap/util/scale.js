// Scene-space scale.
// The map is logarithmic-ish (sqrt) so closer-in bodies don't crowd the star.
// Numbers are chosen for visual readability, not physical accuracy.

export function auToScene(au, refAU)     { return Math.sqrt(au / refAU) * 700; }
export function moonAuToScene(au, refAU) { return Math.sqrt(au / refAU) * 160; }
