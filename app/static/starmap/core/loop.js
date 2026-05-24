// ═══════════════════════════════════════════════════════════════════
//  Animation loop. Subscribers receive (dt) in milliseconds since the
//  previous frame. The loop calls them in registration order, so the
//  order of `add()` calls matters: camera update before render, etc.
// ═══════════════════════════════════════════════════════════════════

export function createLoop() {
  const subs = [];
  let lastTime = 0;

  function tick(now) {
    requestAnimationFrame(tick);
    const dt = lastTime ? now - lastTime : 16;
    lastTime = now;
    for (const fn of subs) fn(dt);
  }

  return {
    add(fn) { subs.push(fn); },
    start() { requestAnimationFrame(tick); },
  };
}
