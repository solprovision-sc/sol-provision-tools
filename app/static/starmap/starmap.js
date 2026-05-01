// ═══════════════════════════════════════════════════════════════════
//  SOL PROVISION — STAR MAP
//  static/starmap/starmap.js
//
//  Textures served from: /static/starmap/textures/{system}/{file}.webp
//  Data source: ARK Starmap API
// ═══════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════
//  TEXTURE MAP  —  body id → texture filenames
//  clim  = albedo/color  → THREE map
//  ddn   = normal map    → THREE normalMap
//  displ = displacement  → THREE displacementMap
//  Falls back to procedural color sphere if null
// ═══════════════════════════════════════════
const TEXTURE_BASE = '/static/starmap/textures';

const TEXTURES = {
  // ── STANTON ──────────────────────────────
  // Using _starmap_diff as albedo — CIG's pre-baked composite of how each body
  // looks from space. The _global_clim files are biome blend masks, not colour maps.
  // _starmap_diff textures are pre-lit, so no normal map applied on top.
  1693: { sys:'stanton', clim:'stanton1_global_starmap_diff',  ddn:null, displ:null }, // Hurston
  1695: { sys:'stanton', clim:null,                            ddn:null, displ:null }, // Crusader — gas giant, procedural
  1694: { sys:'stanton', clim:'stanton3_starmap_diff',         ddn:null, displ:null }, // ArcCorp
  1692: { sys:'stanton', clim:'stanton4_starmap_diff',         ddn:null, displ:null }, // microTech
  2746: { sys:'stanton', clim:'stanton1a_global_starmap_diff', ddn:null, displ:null }, // Arial
  2747: { sys:'stanton', clim:'stanton1b_global_starmap_diff', ddn:null, displ:null }, // Aberdeen
  2748: { sys:'stanton', clim:'stanton1c_global_starmap_diff', ddn:null, displ:null }, // Magda
  2749: { sys:'stanton', clim:'stanton1d_global_starmap_diff', ddn:null, displ:null }, // Ita
  2737: { sys:'stanton', clim:'stanton2a_global_starmap_diff', ddn:null, displ:null }, // Cellin
  2738: { sys:'stanton', clim:'stanton2b_global_starmap_diff', ddn:null, displ:null }, // Daymar
  2739: { sys:'stanton', clim:'stanton2c_global_starmap_diff', ddn:null, displ:null }, // Yela
  2750: { sys:'stanton', clim:'stanton3a_global_starmap_diff', ddn:null, displ:null }, // Lyria
  2751: { sys:'stanton', clim:'stanton3b_global_starmap_diff', ddn:null, displ:null }, // Wala
  2752: { sys:'stanton', clim:'stanton4a_global_starmap_diff', ddn:null, displ:null }, // Calliope
  2753: { sys:'stanton', clim:'stanton4b_global_starmap_diff', ddn:null, displ:null }, // Clio
  2754: { sys:'stanton', clim:'stanton4c_global_starmap_diff', ddn:null, displ:null }, // Euterpe

  // ── PYRO ─────────────────────────────────
  // Pyro bodies don't have _starmap_diff files — fall back to _clim which for
  // rocky bodies (not gas giants) are actual surface colour maps in Pyro's pipeline
  1745: { sys:'pyro',    clim:'pyro1_clim',   ddn:null,        displ:null }, // Pyro I
  1746: { sys:'pyro',    clim:'pyro2_clim',   ddn:'pyro2_ddn', displ:null }, // Monox
  1747: { sys:'pyro',    clim:'pyro3_clim',   ddn:'pyro3_ddn', displ:null }, // Bloom
  1748: { sys:'pyro',    clim:null,           ddn:null,        displ:null }, // Pyro V — gas giant, procedural
  1749: { sys:'pyro',    clim:'pyro4_clim',   ddn:null,        displ:null }, // Pyro IV
  1750: { sys:'pyro',    clim:'pyro6_clim',   ddn:'pyro6_ddn', displ:null }, // Terminus
  2783: { sys:'pyro',    clim:'pyro5a_clim',  ddn:'pyro5a_ddn',displ:null }, // Ignis
  2784: { sys:'pyro',    clim:'pyro5b_clim',  ddn:'pyro5b_ddn',displ:null }, // Vatra
  2785: { sys:'pyro',    clim:'pyro5c_clim',  ddn:'pyro5c_ddn',displ:null }, // Adir
  2786: { sys:'pyro',    clim:'pyro5d_clim',  ddn:'pyro5d_ddn',displ:null }, // Fairo
  2787: { sys:'pyro',    clim:'pyro5e_clim',  ddn:'pyro5e_ddn',displ:null }, // Fuego
  2788: { sys:'pyro',    clim:'pyro5f_clim',  ddn:'pyro5f_ddn',displ:null }, // Vuur

  // ── NYX ──────────────────────────────────
  2626: { sys:'nyx',     clim:'delamar_global_starmap_diff', ddn:null, displ:null }, // Delamar
  // Nyx I, II, III — no textures, procedural fallback
};

// ═══════════════════════════════════════════
//  TEXTURE LOADER — cached, async, graceful
// ═══════════════════════════════════════════
const loader   = new THREE.TextureLoader();
const texCache = {};

function loadTex(path) {
  if (!path) return Promise.resolve(null);
  if (texCache[path]) return Promise.resolve(texCache[path]);
  return new Promise(resolve => {
    loader.load(
      path,
      tex => { texCache[path] = tex; resolve(tex); },
      undefined,
      ()  => { console.warn(`Texture not found: ${path}`); resolve(null); }
    );
  });
}

function texPath(sys, name) {
  return `${TEXTURE_BASE}/${sys}/${name}.webp`;
}

async function loadBodyTextures(bodyId) {
  const t = TEXTURES[bodyId];
  if (!t) return { clim:null, ddn:null, displ:null };
  const [clim, ddn, displ] = await Promise.all([
    t.clim  ? loadTex(texPath(t.sys, t.clim))  : Promise.resolve(null),
    t.ddn   ? loadTex(texPath(t.sys, t.ddn))   : Promise.resolve(null),
    t.displ ? loadTex(texPath(t.sys, t.displ)) : Promise.resolve(null),
  ]);
  return { clim, ddn, displ };
}

function applyTexturesToMesh(mesh, textures, fallbackColor) {
  const { clim, ddn } = textures;
  if (!clim) return; // no albedo = keep procedural material

  // ── Albedo — sRGB colour space ──
  clim.encoding = THREE.sRGBEncoding;
  clim.flipY    = false; // SC textures are already Y-correct for WebGL
  clim.wrapS    = clim.wrapT = THREE.RepeatWrapping;

  // Detect whether this is a pre-lit starmap_diff composite or a raw clim.
  // Pre-lit textures use MeshBasicMaterial (unlit) to avoid double-lighting.
  // Raw clim textures (Pyro) use MeshStandardMaterial with optional normal map.
  const isPrelit = !ddn; // starmap_diff bodies have no ddn assigned

  if (isPrelit) {
    mesh.material.dispose();
    mesh.material = new THREE.MeshBasicMaterial({ map: clim });
  } else {
    // ── Normal map — linear, DirectX→OpenGL Y-channel inversion ──
    ddn.encoding = THREE.LinearEncoding;
    ddn.flipY    = false;
    ddn.wrapS    = ddn.wrapT = THREE.RepeatWrapping;

    mesh.material.dispose();
    mesh.material = new THREE.MeshStandardMaterial({
      map:         clim,
      normalMap:   ddn,
      normalScale: new THREE.Vector2(1, -1), // flip Y: DX → GL convention
      roughness:   0.88,
      metalness:   0.02,
    });
  }
  mesh.material.needsUpdate = true;
}
// ═══════════════════════════════════════════
//  SYSTEM DATA  —  ARK Starmap API
// ═══════════════════════════════════════════
const SYSTEMS = {

  stanton: {
    name: 'STANTON',
    subtitle: 'UEE TERRITORY // CORP-OWNED',
    starColor: '#FFF4B0',
    starGlow: 0xFFF4B0,
    orbitColor: 0x00aa44,
    moonOrbitColor: 0x006633,
    jumpColor: '#FF8844',
    jumpLine: 0xFF6622,
    planetCount: '4 PLANETS // 12 MOONS',
    labelColor: '#00ff88',
    moonLabelColor: '#00aa55',
    bodies: [
      { id:1691, parent:null, type:'STAR', name:'Stanton', designation:'', dist:0, lat:0, lon:0, size:1.2, color:'#FFF4B0',
        description:'A G-type main sequence star. The only fully corporatized system in the UEE.', period:null, faction:'UEE' },
      { id:1693, parent:1691, type:'PLANET', name:'Hurston', designation:'Stanton I', dist:0.859, lat:0, lon:-30, size:11853, color:'#7A5C30', subtype:'Super-Earth',
        description:'A wealth of ore and other resources are mined on Hurston to manufacture Hurston Dynamics\' line of munitions and weapons. Heavy industry has resulted in severe pollution across the planet.', period:291.02, faction:'Hurston Dynamics' },
      { id:1695, parent:1691, type:'PLANET', name:'Crusader', designation:'Stanton II', dist:1.28, lat:0, lon:-160, size:74500, color:'#C87A40', subtype:'Gas Giant',
        description:'A low mass gas giant that features a breathable atmosphere at high altitudes. Home to Crusader Industries\' massive floating platform shipyards above the cloud layer.', period:534.63, faction:'Crusader Industries' },
      { id:1694, parent:1691, type:'PLANET', name:'ArcCorp', designation:'Stanton III', dist:1.933, lat:0, lon:65, size:8363, color:'#8899BB', subtype:'Super-Earth',
        description:'Buildings cover a majority of the planet\'s surface. ArcCorp\'s factories and business headquarters occupy the equatorial zones while residential areas are relegated to the poles.', period:982.18, faction:'ArcCorp' },
      { id:1692, parent:1691, type:'PLANET', name:'microTech', designation:'Stanton IV', dist:2.904, lat:0, lon:-90, size:10328, color:'#AACCDD', subtype:'Super-Earth',
        description:'A terraforming error left microTech with unnaturally dense cloud cover and a colder than average climate — ideal for heat-sensitive computing and manufacturing infrastructure.', period:1804.39, faction:'microTech' },
      { id:2746, parent:1693, type:'SATELLITE', name:'Arial',    designation:'Stanton 1a', dist:0.000579,  lat:7,  lon:167, size:0.7, color:'#AAA090', description:'Named after Arial Hurston, 3rd CEO of Hurston Dynamics, known for the controversial Life/Labor-style employee contract.', period:null, faction:null },
      { id:2747, parent:1693, type:'SATELLITE', name:'Aberdeen', designation:'Stanton 1b', dist:0.000648,  lat:-2, lon:13,  size:0.4, color:'#9A8878', description:'Named after scientist Aberdeen Hurston, credited with designing the company\'s first antimatter warhead.', period:null, faction:null },
      { id:2748, parent:1693, type:'SATELLITE', name:'Magda',    designation:'Stanton 1c', dist:0.0007889, lat:5,  lon:49,  size:0.2, color:'#887060', description:'Named after CEO Magda Stanton who made the decision to purchase Stanton I from the UEE.', period:null, faction:null },
      { id:2749, parent:1693, type:'SATELLITE', name:'Ita',      designation:'Stanton 1d', dist:0.000823,  lat:3,  lon:256, size:0.3, color:'#907868', description:'Named after Ita Hurston, who died during the First Tevarin War — a reminder of why Hurston\'s products are "so important."', period:null, faction:null },
      { id:2737, parent:1695, type:'SATELLITE', name:'Cellin',   designation:'Stanton 2a', dist:0.0006,  lat:-10, lon:100, size:0.67, color:'#B89870', description:'Features over a hundred dormant volcanoes said to represent Cellin\'s simmering anger, from the children\'s tale "A Gift for Baba."', period:null, faction:null },
      { id:2738, parent:1695, type:'SATELLITE', name:'Daymar',   designation:'Stanton 2b', dist:0.0007,  lat:5,   lon:200, size:0.4,  color:'#C8A068', description:'The largest of Crusader\'s moons. Its slightly eccentric orbit represents the middle brother\'s ease at getting lost in the story.', period:null, faction:null },
      { id:2739, parent:1695, type:'SATELLITE', name:'Yela',     designation:'Stanton 2c', dist:0.0011,  lat:-3,  lon:300, size:0.7,  color:'#C8D8E8', description:'A water-ice crust moon representing Yela\'s cool and calculating nature. Home to the infamous Grim HEX pirate outpost.', period:null, faction:null },
      { id:2750, parent:1694, type:'SATELLITE', name:'Lyria',    designation:'Stanton 3a', dist:0.000347, lat:6,  lon:156, size:0.5, color:'#D8E8F0', description:'An icy moon featuring active cryogeysers and cryovolcanoes. Rich in subsurface minerals.', period:null, faction:null },
      { id:2751, parent:1694, type:'SATELLITE', name:'Wala',     designation:'Stanton 3b', dist:0.000571, lat:-5, lon:15,  size:0.3, color:'#B8C8D0', description:'Low density makes Wala particularly susceptible to tidal forces, resulting in a noticeably prolate shape.', period:null, faction:null },
      { id:2752, parent:1692, type:'SATELLITE', name:'Calliope', designation:'Stanton 4a', dist:0.0002589, lat:4,  lon:78,  size:0.6, color:'#C0C8D4', description:'Named after the Greek muse of eloquence — to remind those on microTech to strive for pure expression of thought.', period:null, faction:null },
      { id:2753, parent:1692, type:'SATELLITE', name:'Clio',     designation:'Stanton 4b', dist:0.0003678, lat:-8, lon:34,  size:0.3, color:'#B0B8C4', description:'Named after the Greek muse of history — to remind microTech engineers that their work stands on those who came before.', period:null, faction:null },
      { id:2754, parent:1692, type:'SATELLITE', name:'Euterpe',  designation:'Stanton 4c', dist:0.0006248, lat:0,  lon:-90, size:0.5, color:'#A8B4C0', description:'Named after the Greek muse of music — to remind designers to be guided by the natural rhythm of the universe.', period:null, faction:null },
      { id:1689, parent:1691, type:'JUMPPOINT', name:'Pyro Jump',   designation:'', dist:1.8,  lat:-5,  lon:130,  size:0, color:'#FF8844', description:'Jump point connecting Stanton to the Pyro system.', period:null, faction:null },
      { id:1690, parent:1691, type:'JUMPPOINT', name:'Magnus Jump', designation:'', dist:4.85, lat:15,  lon:45,   size:0, color:'#FF8844', description:'Jump point connecting Stanton to the Magnus system.', period:null, faction:null },
      { id:1699, parent:1691, type:'JUMPPOINT', name:'Terra Jump',  designation:'', dist:4.1,  lat:-20, lon:-135, size:0, color:'#FF8844', description:'Jump point connecting Stanton to the Terra system.', period:null, faction:null },
      { id:2843, parent:1691, type:'JUMPPOINT', name:'Nyx Jump',    designation:'', dist:3.3,  lat:0,   lon:-78,  size:0, color:'#FF8844', description:'Jump point connecting Stanton to the Nyx system. Temporarily redirected from Nyx-Castra as of CitizenCon 2025.', period:null, faction:null },
      { id:1698, parent:1691, type:'ASTEROID_BELT', name:'Aaron Halo', designation:'Stanton Belt Alpha', dist:1.563, lat:0, lon:0, size:0, color:'#888866',
        description:'Though Crusader Industries argues the belt was part of their purchase of Stanton II, the legal confusion created a massive rush of independent miners staking claims.', period:null, faction:null },
    ]
  },

  pyro: {
    name: 'PYRO',
    subtitle: 'UNCLAIMED // HIGH DANGER',
    starColor: '#FF9944',
    starGlow: 0xFF8822,
    orbitColor: 0xaa4400,
    moonOrbitColor: 0x882200,
    jumpColor: '#FFCC44',
    jumpLine: 0xDD9900,
    planetCount: '6 PLANETS // 6 MOONS',
    labelColor: '#ff8833',
    moonLabelColor: '#cc5522',
    bodies: [
      { id:1744, parent:null, type:'STAR', name:'Pyro', designation:'', dist:0, lat:0, lon:0, size:571000, color:'#FF9944',
        description:'A volatile K-type main sequence star. More unstable than Stanton\'s sun, Pyro bathes its system in dangerous radiation and unpredictable flare activity.', period:null, faction:'Unclaimed' },
      { id:1745, parent:1744, type:'PLANET', name:'Pyro I',   designation:'Pyro I',   dist:0.553, lat:0.67, lon:53,    size:4117,  color:'#CC5500', subtype:'Terrestrial Rocky',
        description:'Orbiting very close to Pyro\'s volatile star, Pyro I has high atmospheric pressure and extreme temperatures. Largely uninhabitable.', period:167.85, faction:'Unclaimed' },
      { id:1746, parent:1744, type:'PLANET', name:'Monox',    designation:'Pyro II',  dist:0.71,  lat:1.94, lon:80,    size:5295,  color:'#886644', subtype:'Terrestrial Rocky',
        description:'The coreless Monox bears the scars of old mining operations. Its part-oxygen atmosphere is laced with toxic carbon monoxide, earning it the nickname.', period:244.20, faction:'Unclaimed' },
      { id:1747, parent:1744, type:'PLANET', name:'Bloom',    designation:'Pyro III', dist:1.19,  lat:9.311,lon:42,    size:4139,  color:'#7799BB', subtype:'Terrestrial Rocky',
        description:'An icy terrestrial world with a breathable nitrogen-oxygen atmosphere. Despite its relative habitability, Bloom has been overrun by outlaws and is extremely dangerous.', period:529.77, faction:'Unclaimed' },
      { id:1748, parent:1744, type:'PLANET', name:'Pyro V',   designation:'Pyro V',   dist:2.87,  lat:9.155,lon:116,   size:78123, color:'#668833', subtype:'Gas Giant',
        description:'The largest planet in the Pyro system, with a striking atmosphere swirled with shades of green and yellow. Pyro IV is gravitationally captured in its orbit.', period:4393.65, faction:'Unclaimed' },
      { id:1749, parent:1748, type:'PLANET', name:'Pyro IV',  designation:'Pyro IV',  dist:0.025, lat:8.142,lon:55.18, size:3214,  color:'#AA7744', subtype:'Terrestrial Rocky',
        description:'Astronomers theorize Pyro IV collided with a planet-sized mass in the distant past, warping its landscape and knocking it into a captured orbit around Pyro V.', period:14.53, faction:'Unclaimed' },
      { id:1750, parent:1744, type:'PLANET', name:'Terminus', designation:'Pyro VI',  dist:4.57,  lat:4.44, lon:152,   size:2850,  color:'#8899BB', subtype:'Terrestrial Rocky',
        description:'A frigid, barely-habitable planet with a methane-laced atmosphere. Terminus sits at the cold outer edge of the Pyro system. Home to the abandoned Ruin Station.', period:15146.03, faction:'Unclaimed' },
      { id:2783, parent:1748, type:'SATELLITE', name:'Ignis',  designation:'Pyro 5a', dist:0.0037,  lat:1.933, lon:0,    size:0.5,  color:'#CC6633', description:'Innermost moon of Pyro V, covered in deep canyons and dried riverbeds. No signs of water remain on its desiccated surface.', period:2.121, faction:null },
      { id:2784, parent:1748, type:'SATELLITE', name:'Vatra',  designation:'Pyro 5b', dist:0.00496, lat:1.486, lon:180,  size:0.6,  color:'#886644', description:'A thick high-pressure nitrogen-methane atmosphere hides a dark and eerie landscape beneath the clouds.', period:2.645, faction:null },
      { id:2785, parent:1748, type:'SATELLITE', name:'Adir',   designation:'Pyro 5c', dist:0.006756,lat:0.16,  lon:36,   size:0.42, color:'#999988', description:'The crater-ridden surface of Adir is interspersed with rocky hills and jagged mountains from ancient impacts.', period:4.047, faction:null },
      { id:2786, parent:1748, type:'SATELLITE', name:'Fairo',  designation:'Pyro 5d', dist:0.00841, lat:1.244, lon:-60,  size:0.38, color:'#BB5522', description:'Frequent earthquakes rock Fairo, causing spectacular waves in its brackish seas. A geologically violent world.', period:6.117, faction:null },
      { id:2787, parent:1748, type:'SATELLITE', name:'Fuego',  designation:'Pyro 5e', dist:0.00995, lat:0.151, lon:-110, size:0.81, color:'#DDCC33', description:'High amounts of iron-sulfide in Fuego\'s soil give it a livid yellow-black coloration visible from orbit.', period:9.177, faction:null },
      { id:2788, parent:1748, type:'SATELLITE', name:'Vuur',   designation:'Pyro 5f', dist:0.01177, lat:0.867, lon:90,   size:0.93, color:'#448833', description:'The carbon-rich soil of Vuur supports unique crystalline-carbon plant life found nowhere else in charted space.', period:14.578, faction:null },
      { id:1739, parent:1744, type:'JUMPPOINT', name:'Nyx Jump',     designation:'', dist:13,  lat:21,  lon:-134, size:0, color:'#FFCC44', description:'Jump point connecting Pyro to the Nyx system.', period:null, faction:null },
      { id:1740, parent:1744, type:'JUMPPOINT', name:'Stanton Jump', designation:'', dist:9,   lat:-1,  lon:-5,   size:0, color:'#FFCC44', description:'Jump point connecting Pyro to the Stanton system.', period:null, faction:null },
      { id:1741, parent:1744, type:'JUMPPOINT', name:'Hadrian Jump', designation:'', dist:12,  lat:15,  lon:61,   size:0, color:'#FFCC44', description:'Jump point connecting Pyro to the Hadrian system.', period:null, faction:null },
      { id:1742, parent:1744, type:'JUMPPOINT', name:'Oso Jump',     designation:'', dist:13,  lat:-8,  lon:-69,  size:0, color:'#FFCC44', description:'Jump point connecting Pyro to the Oso system.', period:null, faction:null },
      { id:1743, parent:1744, type:'JUMPPOINT', name:'Castra Jump',  designation:'', dist:11,  lat:-9,  lon:153,  size:0, color:'#FFCC44', description:'Jump point connecting Pyro to the Castra system.', period:null, faction:null },
      { id:1754, parent:1744, type:'JUMPPOINT', name:'Terra Jump',   designation:'', dist:3,   lat:14,  lon:72,   size:0, color:'#FFCC44', description:'Jump point connecting Pyro to the Terra system.', period:null, faction:null },
      { id:1753, parent:1744, type:'ASTEROID_BELT', name:'Akiro Cluster', designation:'Pyro Cluster Alpha', dist:0.553, lat:0, lon:233, size:0, color:'#886644',
        description:'A charred cluster of blackened asteroids. While mostly worthless, some rare materials can be found here. Shares orbital distance with Pyro I.', period:null, faction:null },
    ]
  },

  nyx: {
    name: 'NYX',
    subtitle: 'UNCLAIMED // OUTLAW TERRITORY',
    starColor: '#E8EEFF',
    starGlow: 0xCCDDFF,
    orbitColor: 0x334488,
    moonOrbitColor: 0x223366,
    jumpColor: '#AABBFF',
    jumpLine: 0x8899DD,
    planetCount: '3 PLANETS // 0 MOONS',
    labelColor: '#aabbff',
    moonLabelColor: '#6677cc',
    bodies: [
      { id:1703, parent:null, type:'STAR', name:'Nyx', designation:'', dist:0, lat:0, lon:0, size:965325, color:'#E8EEFF',
        description:'A main sequence F-type dwarf star — hotter and brighter than Earth\'s sun, with a blue-white cast. The Nyx system remains unclaimed and ungoverned, making it a haven for outlaws and political radicals.', period:null, faction:'Unclaimed' },
      { id:1704, parent:1703, type:'PLANET', name:'Nyx I',   designation:'Nyx I',   dist:0.901, lat:0,  lon:-35, size:5786,  color:'#887755', subtype:'Terrestrial Rocky',
        description:'Nyx I is currently being terraformed using experimental technology. The process is ongoing and the planet remains largely inhospitable.', period:274.13, faction:'Unclaimed' },
      { id:1705, parent:1703, type:'PLANET', name:'Nyx II',  designation:'Nyx II',  dist:1.501, lat:1,  lon:80,  size:5955,  color:'#997744', subtype:'Smog Planet',
        description:'A high-pressure atmosphere and thick clouds of acid and carbon dioxide make Nyx II an extremely dangerous travel destination. The toxic smog is impenetrable from orbit.', period:589.15, faction:'Unclaimed' },
      { id:1706, parent:1703, type:'PLANET', name:'Nyx III', designation:'Nyx III', dist:7.7,   lat:0,  lon:160, size:30028, color:'#5577AA', subtype:'Ice Giant',
        description:'A vast ice giant that lacks a breathable atmosphere. Nyx III dominates the outer system and serves as a navigation reference point for ships transiting the system.', period:6848.17, faction:'Unclaimed' },
      { id:2626, parent:1703, type:'PLANET', name:'Delamar', designation:'Delamar', dist:2.1,   lat:0,  lon:260, size:165.4, color:'#667788', subtype:'Protoplanet',
        description:'A large asteroid hidden deep within the Glaciem Ring. Anti-UEE activists, political radicals, and criminals occupy a deserted mining facility built inside this rock. Visitors are forbidden from entering its residential areas.', period:975.0, faction:'Outlaw' },
      { id:1701, parent:1703, type:'JUMPPOINT', name:'Castra Jump', designation:'', dist:3.1, lat:-8,  lon:-133, size:0, color:'#AABBFF', description:'Jump point connecting Nyx to the Castra system.', period:null, faction:null },
      { id:1702, parent:1703, type:'JUMPPOINT', name:'Bremen Jump', designation:'', dist:3.4, lat:21,  lon:-157, size:0, color:'#AABBFF', description:'Jump point connecting Nyx to the Bremen system.', period:null, faction:null },
      { id:1710, parent:1703, type:'JUMPPOINT', name:'Pyro Jump',   designation:'', dist:2.7, lat:1,   lon:102,  size:0, color:'#AABBFF', description:'Jump point connecting Nyx to the Pyro system.', period:null, faction:null },
      { id:1711, parent:1703, type:'JUMPPOINT', name:'Virgil Jump', designation:'', dist:3.9, lat:31,  lon:122,  size:0, color:'#AABBFF', description:'Jump point connecting Nyx to the Virgil system.', period:null, faction:null },
      { id:1712, parent:1703, type:'JUMPPOINT', name:'Tohil Jump',  designation:'', dist:2.4, lat:16,  lon:-42,  size:0, color:'#AABBFF', description:'Jump point connecting Nyx to the Tohil system.', period:null, faction:null },
      { id:1713, parent:1703, type:'JUMPPOINT', name:'Odin Jump',   designation:'', dist:3.9, lat:-31, lon:122,  size:0, color:'#AABBFF', description:'Jump point connecting Nyx to the Odin system.', period:null, faction:null },
      { id:2842, parent:1703, type:'JUMPPOINT', name:'Stanton Jump',designation:'', dist:2.6, lat:0,   lon:260,  size:0, color:'#AABBFF', description:'Jump point connecting Nyx to the Stanton system.', period:null, faction:null },
      { id:1707, parent:1703, type:'ASTEROID_BELT', name:'Glaciem Ring', designation:'Nyx Belt Alpha', dist:2.1, lat:0, lon:0, size:0, color:'#8899AA',
        description:'A dense asteroid belt full of places to hide. Delamar, a particularly large asteroid, houses both criminals and a large anti-UEE population deep within the ring.', period:975.0, faction:null },
      { id:2627, parent:1703, type:'ASTEROID_BELT', name:'Keeger Belt', designation:'Nyx Belt Beta', dist:11.2, lat:0, lon:0, size:0, color:'#667788',
        description:'A sparse belt located on the outer edge of the Nyx system. Low mineral density makes it largely unprofitable to mine.', period:12018.0, faction:null },
    ]
  }
};

// ═══════════════════════════════════════════
//  SCALE FUNCTIONS
// ═══════════════════════════════════════════
function auToScene(au, refAU)     { return Math.sqrt(au / refAU) * 700; }
function moonAuToScene(au, refAU) { return Math.sqrt(au / refAU) * 160; }

// ═══════════════════════════════════════════
//  THREE.JS SETUP
// ═══════════════════════════════════════════
const container = document.getElementById('canvas-container');
const renderer  = new THREE.WebGLRenderer({ antialias:true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x000000, 1);
container.appendChild(renderer.domElement);

const scene  = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(50, window.innerWidth/window.innerHeight, 0.1, 80000);
camera.position.set(0, 400, 900);

// ── CAMERA CONTROLS ──
let spherical       = { theta:0.3, phi:1.1, r:1050 };
let targetSpherical = { ...spherical };
let pan             = new THREE.Vector3();
let targetPan       = new THREE.Vector3();
let isDragging=false, isRightDrag=false, prevMouse={x:0,y:0};

function updateCamera() {
  spherical.theta += (targetSpherical.theta - spherical.theta) * 0.08;
  spherical.phi   += (targetSpherical.phi   - spherical.phi)   * 0.08;
  spherical.r     += (targetSpherical.r     - spherical.r)     * 0.08;
  pan.lerp(targetPan, 0.08);
  spherical.phi = Math.max(0.05, Math.min(Math.PI-0.05, spherical.phi));
  spherical.r   = Math.max(60,   Math.min(12000, spherical.r));
  camera.position.set(
    pan.x + spherical.r * Math.sin(spherical.phi) * Math.cos(spherical.theta),
    pan.y + spherical.r * Math.cos(spherical.phi),
    pan.z + spherical.r * Math.sin(spherical.phi) * Math.sin(spherical.theta)
  );
  camera.lookAt(pan);
}

container.addEventListener('mousedown', e => { isDragging=true; isRightDrag=e.button===2; prevMouse={x:e.clientX,y:e.clientY}; });
container.addEventListener('contextmenu', e => e.preventDefault());
window.addEventListener('mouseup', () => isDragging=false);
window.addEventListener('mousemove', e => {
  if (!isDragging) return;
  const dx=e.clientX-prevMouse.x, dy=e.clientY-prevMouse.y;
  if (isRightDrag) {
    const right=new THREE.Vector3(), up=new THREE.Vector3(0,1,0);
    right.crossVectors(camera.position.clone().sub(pan).normalize(), up).normalize();
    const sp=spherical.r*0.0005;
    targetPan.addScaledVector(right,dx*sp);
    targetPan.addScaledVector(up, dy*sp);
  } else {
    targetSpherical.theta += dx*0.005;
    targetSpherical.phi   -= dy*0.005;
  }
  prevMouse={x:e.clientX,y:e.clientY};
});
container.addEventListener('wheel', e => { targetSpherical.r *= 1+e.deltaY*0.001; }, {passive:true});
window.addEventListener('keydown', e => {
  if (e.key==='f'||e.key==='F') { targetSpherical={theta:0.3,phi:1.1,r:1050}; targetPan=new THREE.Vector3(); }
});

// ── STARFIELD ──
function makeStarfield() {
  const geo=new THREE.BufferGeometry();
  const N=5000, pos=new Float32Array(N*3), col=new Float32Array(N*3);
  for(let i=0;i<N;i++){
    const r=10000+Math.random()*15000;
    const th=Math.random()*Math.PI*2, ph=Math.acos(2*Math.random()-1);
    pos[i*3]=r*Math.sin(ph)*Math.cos(th); pos[i*3+1]=r*Math.sin(ph)*Math.sin(th); pos[i*3+2]=r*Math.cos(ph);
    const t=Math.random(); col[i*3]=0.7+t*0.3; col[i*3+1]=0.8+t*0.2; col[i*3+2]=0.9+t*0.1;
  }
  geo.setAttribute('position',new THREE.BufferAttribute(pos,3));
  geo.setAttribute('color',new THREE.BufferAttribute(col,3));
  return new THREE.Points(geo, new THREE.PointsMaterial({size:1.3,vertexColors:true,transparent:true,opacity:0.65}));
}
scene.add(makeStarfield());

// ═══════════════════════════════════════════
//  SCENE STATE
// ═══════════════════════════════════════════
let activeSystem  = 'stanton';
let sceneObjects  = [];
let bodyIndex     = {};
let meshes        = [];
let labelSprites  = [];
let orbitObjects  = [];
let orbitAnimating = false;
let filterState   = { PLANET:true, SATELLITE:true, orbits:true, labels:true, JUMPPOINT:true, ASTEROID_BELT:true };

// ═══════════════════════════════════════════
//  SCENE HELPERS
// ═══════════════════════════════════════════
function addToScene(obj) { scene.add(obj); sceneObjects.push(obj); return obj; }

function clearScene() {
  sceneObjects.forEach(obj => {
    scene.remove(obj);
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      if (Array.isArray(obj.material)) obj.material.forEach(m => { if(m.map) m.map.dispose(); m.dispose(); });
      else { if(obj.material.map) obj.material.map.dispose(); obj.material.dispose(); }
    }
  });
  sceneObjects=[]; bodyIndex={}; meshes=[]; labelSprites=[]; orbitObjects=[];
  closeInfo();
}

function makeLabel(text, color='#00ff88', fontSize=22) {
  const canvas=document.createElement('canvas'); canvas.width=256; canvas.height=48;
  const ctx=canvas.getContext('2d');
  ctx.font=`${fontSize}px "Share Tech Mono",monospace`;
  ctx.fillStyle=color; ctx.textAlign='left';
  ctx.fillText(text.toUpperCase(), 4, fontSize+4);
  const mat=new THREE.SpriteMaterial({map:new THREE.CanvasTexture(canvas),transparent:true,depthTest:false});
  const s=new THREE.Sprite(mat); s.scale.set(80,15,1);
  return s;
}

function makeOrbitRing(radius, color=0x004422, opacity=0.3, segs=128) {
  const pts=[];
  for(let i=0;i<=segs;i++){ const a=(i/segs)*Math.PI*2; pts.push(Math.cos(a)*radius,0,Math.sin(a)*radius); }
  const geo=new THREE.BufferGeometry(); geo.setAttribute('position',new THREE.BufferAttribute(new Float32Array(pts),3));
  return new THREE.Line(geo, new THREE.LineBasicMaterial({color,transparent:true,opacity,depthWrite:false}));
}

function makeAsteroidBelt(radius, color=0x667755, count=500, spread=0.06) {
  const geo=new THREE.BufferGeometry(); const pos=new Float32Array(count*3);
  for(let i=0;i<count;i++){
    const a=Math.random()*Math.PI*2, r=radius+(Math.random()-0.5)*radius*spread, y=(Math.random()-0.5)*10;
    pos[i*3]=Math.cos(a)*r; pos[i*3+1]=y; pos[i*3+2]=Math.sin(a)*r;
  }
  geo.setAttribute('position',new THREE.BufferAttribute(pos,3));
  return new THREE.Points(geo,new THREE.PointsMaterial({color,size:1.8,transparent:true,opacity:0.45}));
}

function makeGlow(radius, colorHex, opacity=0.06) {
  const geo=new THREE.SphereGeometry(radius*1.5,16,16);
  const mat=new THREE.MeshBasicMaterial({color:parseInt(colorHex.replace('#','0x'),16),transparent:true,opacity,side:THREE.BackSide});
  return new THREE.Mesh(geo,mat);
}

function makeJumpMarker(colorHex) {
  const geo=new THREE.OctahedronGeometry(6,0);
  const mat=new THREE.MeshBasicMaterial({color:parseInt(colorHex.replace('#','0x'),16),wireframe:true,transparent:true,opacity:0.85});
  return new THREE.Mesh(geo,mat);
}

// ═══════════════════════════════════════════
//  BUILD SYSTEM
// ═══════════════════════════════════════════
function buildSystem(systemKey) {
  clearScene();
  const sys = SYSTEMS[systemKey];

  // Scale reference — outermost direct child of star
  const starId    = sys.bodies.find(b=>b.type==='STAR').id;
  const directKids = sys.bodies.filter(b=>b.parent===starId && b.dist>0);
  const refAU     = Math.max(...directKids.map(b=>b.dist));
  let moonRefAU   = 0;
  sys.bodies.filter(b=>b.type==='PLANET').forEach(p => {
    sys.bodies.filter(b=>b.parent===p.id).forEach(m => { if(m.dist>moonRefAU) moonRefAU=m.dist; });
  });
  if (moonRefAU < 0.001) moonRefAU = 0.001;

  // Build index + compute initial positions
  sys.bodies.forEach(b => { bodyIndex[b.id] = {...b, position:new THREE.Vector3(), orbitAngle:b.lon*Math.PI/180}; });

  function getPos(id) {
    const b=bodyIndex[id]; if(!b) return new THREE.Vector3();
    if(b.type==='STAR'){ b.position.set(0,0,0); return b.position; }
    const parentBd=b.parent?bodyIndex[b.parent]:null;
    const pp=parentBd?getPos(b.parent):new THREE.Vector3();
    const useMoon=parentBd&&parentBd.type==='PLANET';
    const r=useMoon?moonAuToScene(b.dist,moonRefAU):auToScene(b.dist,refAU);
    const lat=b.lat*Math.PI/180;
    b.position.set(pp.x+r*Math.cos(lat)*Math.cos(b.orbitAngle), pp.y+r*Math.sin(lat), pp.z+r*Math.cos(lat)*Math.sin(b.orbitAngle));
    return b.position;
  }
  sys.bodies.forEach(b=>getPos(b.id));
  bodyIndex._refAU=refAU; bodyIndex._moonRefAU=moonRefAU;

  const PLANET_R={ 'Super-Earth':13, 'Gas Giant':26, 'Terrestrial Rocky':11, 'Smog Planet':11, 'Ice Giant':20, 'Protoplanet':7, default:12 };

  // ── STAR ──
  const starData=sys.bodies.find(b=>b.type==='STAR');
  const starR=36, starCol=parseInt(sys.starColor.replace('#',''),16);
  const starMesh=new THREE.Mesh(new THREE.SphereGeometry(starR,32,32), new THREE.MeshBasicMaterial({color:starCol}));
  addToScene(starMesh); bodyIndex[starData.id].mesh=starMesh; meshes.push(starMesh);
  starMesh.userData={bodyId:starData.id};
  for(let i=0;i<3;i++){ const g=makeGlow(starR*(1.4+i*0.7),sys.starColor,0.07-i*0.015); addToScene(g); }
  addToScene(new THREE.PointLight(sys.starGlow,1.5,6000));
  addToScene(new THREE.AmbientLight(0x111111,0.9));
  const starLbl=makeLabel(starData.name,sys.starColor,22);
  starLbl.position.set(starR+12,starR+10,0); starLbl.userData={type:'label',bodyType:'STAR'};
  addToScene(starLbl); labelSprites.push(starLbl);

  // ── PLANETS (direct children of star) ──
  sys.bodies.filter(b=>b.type==='PLANET'&&b.parent===starId).forEach(b=>{
    const bd=bodyIndex[b.id];
    const pr=PLANET_R[b.subtype]||PLANET_R.default;
    const col=parseInt(b.color.replace('#',''),16);

    const ring=makeOrbitRing(auToScene(b.dist,refAU),sys.orbitColor,0.22);
    ring.userData={type:'orbit',bodyType:'orbits'}; addToScene(ring); orbitObjects.push(ring);

    const geo=new THREE.SphereGeometry(pr,32,32);
    const mat=new THREE.MeshStandardMaterial({color:col,roughness:0.82,metalness:0.08});
    const mesh=new THREE.Mesh(geo,mat);
    mesh.position.copy(bd.position); mesh.userData={bodyId:b.id};
    addToScene(mesh); meshes.push(mesh); bd.mesh=mesh;

    const glow=makeGlow(pr,b.color,0.07); glow.position.copy(bd.position); addToScene(glow); bd.glowMesh=glow;

    const lbl=makeLabel(b.name,sys.labelColor,19);
    lbl.position.set(bd.position.x+pr+10,bd.position.y+pr+8,bd.position.z);
    lbl.userData={type:'label',bodyType:'PLANET',followId:b.id,offX:pr+10,offY:pr+8};
    addToScene(lbl); labelSprites.push(lbl);

    // async texture load — updates material in place when ready
    loadBodyTextures(b.id).then(textures => { if(textures.clim) applyTexturesToMesh(mesh,textures,col); });
  });

  // ── CAPTURED PLANETS (planet orbiting a planet, e.g. Pyro IV) ──
  sys.bodies.filter(b=>b.type==='PLANET'&&b.parent!==starId).forEach(b=>{
    const bd=bodyIndex[b.id]; const parentBd=bodyIndex[b.parent];
    const pr=9, col=parseInt(b.color.replace('#',''),16);

    const ring=makeOrbitRing(moonAuToScene(b.dist,moonRefAU),sys.moonOrbitColor,0.3,64);
    ring.position.copy(parentBd.position); ring.userData={type:'orbit',bodyType:'orbits',parentId:b.parent};
    addToScene(ring); orbitObjects.push(ring); bd.orbitRing=ring;

    const geo=new THREE.SphereGeometry(pr,24,24);
    const mat=new THREE.MeshStandardMaterial({color:col,roughness:0.85});
    const mesh=new THREE.Mesh(geo,mat);
    mesh.position.copy(bd.position); mesh.userData={bodyId:b.id};
    addToScene(mesh); meshes.push(mesh); bd.mesh=mesh;

    const glow=makeGlow(pr,b.color,0.06); glow.position.copy(bd.position); addToScene(glow); bd.glowMesh=glow;

    const lbl=makeLabel(b.name,sys.labelColor,17);
    lbl.position.set(bd.position.x+pr+8,bd.position.y+pr+6,bd.position.z);
    lbl.scale.set(65,12,1); lbl.userData={type:'label',bodyType:'PLANET',followId:b.id,offX:pr+8,offY:pr+6};
    addToScene(lbl); labelSprites.push(lbl);

    loadBodyTextures(b.id).then(textures => { if(textures.clim) applyTexturesToMesh(mesh,textures,col); });
  });

  // ── MOONS ──
  sys.bodies.filter(b=>b.type==='SATELLITE').forEach(b=>{
    const bd=bodyIndex[b.id]; const parentBd=bodyIndex[b.parent];
    const col=parseInt(b.color.replace('#',''),16);
    const moonOrR=moonAuToScene(b.dist,moonRefAU);

    const ring=makeOrbitRing(moonOrR,sys.moonOrbitColor,0.18,64);
    ring.position.copy(parentBd.position); ring.userData={type:'orbit',bodyType:'orbits',parentId:b.parent};
    addToScene(ring); orbitObjects.push(ring); bd.orbitRing=ring;

    const geo=new THREE.SphereGeometry(4,24,24);
    const mat=new THREE.MeshStandardMaterial({color:col,roughness:0.9});
    const mesh=new THREE.Mesh(geo,mat);
    mesh.position.copy(bd.position); mesh.userData={bodyId:b.id};
    addToScene(mesh); meshes.push(mesh); bd.mesh=mesh;

    const lbl=makeLabel(b.name,sys.moonLabelColor,15);
    lbl.position.set(bd.position.x+7,bd.position.y+7,bd.position.z);
    lbl.scale.set(55,10,1); lbl.userData={type:'label',bodyType:'SATELLITE',followId:b.id,offX:7,offY:7};
    addToScene(lbl); labelSprites.push(lbl);

    loadBodyTextures(b.id).then(textures => { if(textures.clim) applyTexturesToMesh(mesh,textures,col); });
  });

  // ── JUMP POINTS ──
  sys.bodies.filter(b=>b.type==='JUMPPOINT').forEach(b=>{
    const bd=bodyIndex[b.id];
    const marker=makeJumpMarker(sys.jumpColor);
    marker.position.copy(bd.position); marker.userData={bodyId:b.id};
    addToScene(marker); meshes.push(marker); bd.mesh=marker;

    const lbl=makeLabel(b.name,sys.jumpColor,15);
    lbl.position.set(bd.position.x+10,bd.position.y+10,bd.position.z);
    lbl.scale.set(70,11,1); lbl.userData={type:'label',bodyType:'JUMPPOINT',followId:b.id,offX:10,offY:10};
    addToScene(lbl); labelSprites.push(lbl);

    const geo=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0,0,0),bd.position.clone()]);
    const line=new THREE.Line(geo,new THREE.LineDashedMaterial({color:sys.jumpLine,dashSize:14,gapSize:10,transparent:true,opacity:0.18}));
    line.computeLineDistances(); line.userData={bodyType:'JUMPPOINT'}; addToScene(line);
  });

  // ── ASTEROID BELTS ──
  sys.bodies.filter(b=>b.type==='ASTEROID_BELT'||b.type==='ASTEROID_FIELD').forEach(b=>{
    const bd=bodyIndex[b.id];
    const belt=makeAsteroidBelt(auToScene(b.dist,refAU),parseInt(b.color.replace('#',''),16));
    belt.userData={bodyId:b.id,bodyType:'ASTEROID_BELT'}; addToScene(belt); meshes.push(belt); bd.mesh=belt;
  });

  applyFilters();
}

// ═══════════════════════════════════════════
//  SYSTEM SWITCH
// ═══════════════════════════════════════════
function switchSystem(key) {
  if (key===activeSystem) return;
  activeSystem=key;

  const flash=document.getElementById('flash');
  flash.style.opacity='0.15';
  setTimeout(()=>{ flash.style.opacity='0'; },200);

  document.body.classList.remove('pyro-theme','nyx-theme');
  if (key==='pyro') document.body.classList.add('pyro-theme');
  if (key==='nyx')  document.body.classList.add('nyx-theme');

  const sys=SYSTEMS[key];
  document.getElementById('system-id').textContent    =`SYS: ${sys.name} // ${sys.subtitle}`;
  document.getElementById('body-count').textContent   = sys.planetCount;
  document.querySelectorAll('.sys-btn').forEach(btn=>{
    btn.classList.toggle('active', btn.textContent.toLowerCase()===key);
  });

  targetSpherical={theta:0.3,phi:1.1,r:1050}; targetPan=new THREE.Vector3();
  buildSystem(key);
  orbitAnimating=false; document.getElementById('orbit-toggle').classList.remove('active');
}

// ═══════════════════════════════════════════
//  RAYCASTING
// ═══════════════════════════════════════════
const raycaster=new THREE.Raycaster(); raycaster.params.Points={threshold:10};
const mouse=new THREE.Vector2();

container.addEventListener('mousemove', e=>{
  mouse.x=(e.clientX/window.innerWidth)*2-1;
  mouse.y=-(e.clientY/window.innerHeight)*2+1;
  raycaster.setFromCamera(mouse,camera);
  const hits=raycaster.intersectObjects(meshes,false);
  const tip=document.getElementById('tooltip');
  if(hits.length>0&&hits[0].object.userData.bodyId){
    const b=bodyIndex[hits[0].object.userData.bodyId];
    if(b&&b.name){ tip.textContent=b.name.toUpperCase(); tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY-8)+'px'; tip.classList.add('show'); document.body.style.cursor='pointer'; return; }
  }
  tip.classList.remove('show'); document.body.style.cursor='crosshair';
});

container.addEventListener('click', e=>{
  mouse.x=(e.clientX/window.innerWidth)*2-1; mouse.y=-(e.clientY/window.innerHeight)*2+1;
  raycaster.setFromCamera(mouse,camera);
  const hits=raycaster.intersectObjects(meshes,false);
  if(hits.length>0&&hits[0].object.userData.bodyId) selectBody(hits[0].object.userData.bodyId);
});

function selectBody(id) {
  const b=bodyIndex[id]; if(!b) return;
  const parentName=b.parent&&bodyIndex[b.parent]?(bodyIndex[b.parent].name||'—'):'None';
  document.getElementById('ipt').textContent     = b.name||b.designation||'—';
  document.getElementById('iptype').textContent  = (b.subtype||b.type||'—').toUpperCase();
  document.getElementById('i-desig').textContent = b.designation||'—';
  document.getElementById('i-type').textContent  = b.subtype||b.type||'—';
  document.getElementById('i-parent').textContent= parentName;
  document.getElementById('i-dist').textContent  = b.dist>0?b.dist.toFixed(4)+' AU':'—';
  document.getElementById('i-period').textContent= b.period?b.period.toFixed(2)+' days':'—';
  document.getElementById('i-size').textContent  = b.size>1?Math.round(b.size)+' km':(b.size>0?(b.size*100).toFixed(0)+' (rel)':'—');
  document.getElementById('i-faction').textContent=b.faction||'—';
  document.getElementById('idesc').textContent   = b.description||'—';
  document.getElementById('info-panel').classList.add('visible');
}

function closeInfo() { document.getElementById('info-panel').classList.remove('visible'); }

// ═══════════════════════════════════════════
//  FILTERS
// ═══════════════════════════════════════════
function toggleFilter(btn) {
  const type=btn.dataset.type; filterState[type]=!filterState[type];
  btn.classList.toggle('active',filterState[type]); applyFilters();
}

function applyFilters() {
  meshes.forEach(m=>{ const id=m.userData.bodyId; if(!id) return; const b=bodyIndex[id]; if(!b) return; const t=b.type==='ASTEROID_FIELD'?'ASTEROID_BELT':b.type; if(filterState[t]!==undefined) m.visible=filterState[t]; });
  orbitObjects.forEach(o=>{ if(o.userData.type==='orbit') o.visible=filterState['orbits']; });
  labelSprites.forEach(s=>{ if(!filterState['labels']){s.visible=false;return;} s.visible=filterState[s.userData.bodyType]!==false; });
  sceneObjects.forEach(o=>{ if(o.userData.bodyType==='JUMPPOINT') o.visible=filterState['JUMPPOINT']; });
}

// ═══════════════════════════════════════════
//  ORBITAL ANIMATION
// ═══════════════════════════════════════════
function toggleOrbits() {
  orbitAnimating=!orbitAnimating;
  document.getElementById('orbit-toggle').classList.toggle('active',orbitAnimating);
}

function getOrbitSpeed(b) {
  if(b.period&&b.period>0) return 0.12/b.period;
  return b.type==='SATELLITE'?0.0018:0.0002;
}

function updateOrbits(dt) {
  if(!orbitAnimating) return;
  const sys=SYSTEMS[activeSystem];
  const refAU=bodyIndex._refAU, moonRefAU=bodyIndex._moonRefAU;

  sys.bodies.filter(b=>b.type==='PLANET'||b.type==='SATELLITE').forEach(b=>{
    const bd=bodyIndex[b.id]; if(!bd||!bd.mesh) return;
    bd.orbitAngle+=getOrbitSpeed(b)*dt;
    const parentBd=b.parent?bodyIndex[b.parent]:null;
    const pp=parentBd?parentBd.position:new THREE.Vector3();
    const useMoon=parentBd&&parentBd.type==='PLANET';
    const r=useMoon?moonAuToScene(b.dist,moonRefAU):auToScene(b.dist,refAU);
    const lat=b.lat*Math.PI/180;
    bd.position.set(pp.x+r*Math.cos(lat)*Math.cos(bd.orbitAngle), pp.y+r*Math.sin(lat), pp.z+r*Math.cos(lat)*Math.sin(bd.orbitAngle));
    bd.mesh.position.copy(bd.position);
    if(bd.glowMesh) bd.glowMesh.position.copy(bd.position);
    if(bd.orbitRing) bd.orbitRing.position.copy(pp);
  });
  labelSprites.forEach(s=>{ const fid=s.userData.followId; if(fid&&bodyIndex[fid]){ const bd=bodyIndex[fid]; s.position.set(bd.position.x+(s.userData.offX||0),bd.position.y+(s.userData.offY||0),bd.position.z); } });
}

// ═══════════════════════════════════════════
//  ANIMATE LOOP
// ═══════════════════════════════════════════
let lastTime=performance.now();
function animate() {
  requestAnimationFrame(animate);
  const now=performance.now(), dt=now-lastTime; lastTime=now;
  updateCamera(); updateOrbits(dt);
  labelSprites.forEach(s=>s.quaternion.copy(camera.quaternion));
  renderer.render(scene,camera);
}

window.addEventListener('resize',()=>{ const w=window.innerWidth,h=window.innerHeight; camera.aspect=w/h; camera.updateProjectionMatrix(); renderer.setSize(w,h); });

// ── INIT ──
buildSystem('stanton');
animate();
