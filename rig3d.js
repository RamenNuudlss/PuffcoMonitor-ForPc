/*
 * rig3d.js - the "3D model" style for the Puffco Monitor OBS overlay.
 *
 * Renders models/peak_pro_hd.glb with three.js on a transparent canvas and animates it from the
 * same state the photo style uses. overlay.html owns the timing (session rhythm, pop-up spring,
 * light levels) and calls frame() every animation frame; this file turns that into 3D motion:
 *
 *   idle      turntable / gentle sway while it's in standby or on the charger
 *   heating   spins around once, the light ring fills up around the base, the heater threads
 *             glow hotter, and the camera eases in a little
 *   session   carb cap twists back and forth, the rig tips toward the camera on each inhale,
 *             bubbles rise in the glass, and the smoke follows the real mouthpiece
 *   charging  a 3D charging pad appears and the rig settles onto it; its ring shows the battery
 *   motion    per mode: still, slow or fast spin, sway, float, rock, bounce or wobble
 *   reactions spin, double spin, hop, hop-and-spin, flip, shake or nod when it starts heating,
 *             when it's ready, when it's fully charged and when it connects
 *   pop-up    3D entrances: spinning in, twirling in, dropping in and landing with a squash
 *
 * Parts are found by name (LightRing, Glass, GlassPercolator, CarbCap, AtomizerThreads), so
 * another model with the same part names works too; anything missing is simply skipped.
 */
import {
  WebGLRenderer, Scene, PerspectiveCamera, Group, Mesh, InstancedMesh, Object3D,
  SphereGeometry, PlaneGeometry, RingGeometry, LatheGeometry, CylinderGeometry,
  MeshBasicMaterial, MeshPhysicalMaterial, MeshStandardMaterial, ShaderMaterial,
  Color, Vector2, Vector3, Box3, PMREMGenerator, CanvasTexture, SRGBColorSpace, NeutralToneMapping,
  AdditiveBlending, DirectionalLight, PointLight, HemisphereLight,
  GLTFLoader, RoomEnvironment, MeshoptDecoder,
} from './vendor/three-lite.js';

const DEG = Math.PI / 180, TAU = Math.PI * 2;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const ease = t => (t = clamp(t, 0, 1)) < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const approach = (cur, target, rate, dt) => cur + (target - cur) * (1 - Math.exp(-rate * dt));
const FOV = 18;                       // long lens = product-shot look, little distortion
const CHARGE = new Color().setRGB(0.31, 0.94, 0.63, SRGBColorSpace);   // same green as the photo style's pad
const HEAT = new Color(1.0, 0.42, 0.12);

// view angles: how far the rig is turned. "Side" matches the side photo (glass left, cap right).
export const VIEWS = { front: 0, three_quarter: 35, side: 90, three_quarter_other: -35, side_other: -90, back: 180 };
const QUALITY = { high: { res: 1, transmission: true, tScale: 1 },
                  balanced: { res: 0.72, transmission: true, tScale: 0.5 },
                  performance: { res: 0.5, transmission: false, tScale: 0.5 } };

// critically-damped / bouncy spring, stepped in small slices so it's stable at any frame rate
class Spring {
  constructor(x = 0, w = 8, z = 1) { this.x = x; this.v = 0; this.w = w; this.z = z; }
  step(target, dt) {
    for (let k = 0, n = Math.max(1, Math.ceil(dt * 240)); k < n; k++) {
      const h = dt / n;
      this.v += (this.w * this.w * (target - this.x) - 2 * this.z * this.w * this.v) * h;
      this.x += this.v * h;
    }
    return this.x;
  }
}

function radialTexture(stops, size = 128) {
  const c = document.createElement('canvas'); c.width = c.height = size;
  const g = c.getContext('2d'), gr = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  for (const [o, col] of stops) gr.addColorStop(o, col);
  g.fillStyle = gr; g.fillRect(0, 0, size, size);
  const t = new CanvasTexture(c); t.colorSpace = SRGBColorSpace; return t;
}

export async function createRig3D(canvas, url, opts = {}) {
  const renderer = new WebGLRenderer({ canvas, alpha: true, antialias: true, premultipliedAlpha: true,
                                       powerPreference: 'high-performance' });
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.toneMapping = NeutralToneMapping;
  renderer.toneMappingExposure = 0.92;
  renderer.autoClear = false;

  const scene = new Scene();
  const pmrem = new PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.5;
  const camera = new PerspectiveCamera(FOV, 1, 0.05, 60);
  camera.layers.enable(0);

  const lights = [];
  const addLight = (l, x, y, z) => { l.position.set(x, y, z); l.layers.enableAll(); scene.add(l); lights.push(l); return l; };
  addLight(new DirectionalLight(0xffffff, 2.1), -2.2, 4.2, 3.2);             // key, upper left
  addLight(new DirectionalLight(0xfff1ea, 1.5), 2.4, 2.4, -4.0);             // rim, so the edges read on dark games
  addLight(new HemisphereLight(0xffffff, 0x221a1e, 0.25), 0, 1, 0);

  // ---- the model --------------------------------------------------------------------------
  const gltf = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).loadAsync(url);
  const model = gltf.scene;
  model.updateMatrixWorld(true);
  const part = n => model.getObjectByName(n) || null;
  const box = new Box3().setFromObject(model);
  const MH = box.max.y - box.min.y;                         // model height (glass tip to floor)
  const holder = new Group();                               // everything below is measured in holder space
  holder.add(model);
  model.position.y -= box.min.y;                            // stand it on y = 0
  holder.updateMatrixWorld(true);
  const baseR = Math.max(0.05, Math.max(-box.min.x, box.max.x, Math.abs(box.min.z), Math.abs(box.max.z)) * 0.92);

  // world-space vertices of a part (handles the quantized, compressed geometry)
  function eachVertex(mesh, fn) {
    if (!mesh || !mesh.geometry) return;
    const pos = mesh.geometry.attributes.position, v = new Vector3();
    for (let i = 0; i < pos.count; i++) fn(v.fromBufferAttribute(pos, i).applyMatrix4(mesh.matrixWorld));
  }
  const glass = part('Glass'), perc = part('GlassPercolator'), ring = part('LightRing');
  const cap = part('CarbCap'), threads = part('AtomizerThreads');

  // mouthpiece tip (where the smoke comes out) and the glass's axis
  let tip = new Vector3(0, MH, 0), gAxis = new Vector2(0, 0), gBox = new Box3();
  if (glass) {
    gBox.setFromObject(glass);
    const top = gBox.max.y, pts = [];
    eachVertex(glass, p => { if (p.y > top - 0.012 * MH) pts.push(p.clone()); });
    if (pts.length) tip = pts.reduce((a, p) => a.add(p), new Vector3()).multiplyScalar(1 / pts.length);
    gAxis.set((gBox.min.x + gBox.max.x) / 2, (gBox.min.z + gBox.max.z) / 2);
  }
  // inner radius of the glass by height, for keeping bubbles inside it
  const PROF_N = 48, prof = new Float32Array(PROF_N).fill(0);
  if (glass) {
    const bins = Array.from({ length: PROF_N }, () => []);
    eachVertex(glass, p => {
      const k = Math.floor((p.y - gBox.min.y) / (gBox.max.y - gBox.min.y) * PROF_N);
      if (k >= 0 && k < PROF_N) bins[k].push(Math.hypot(p.x - gAxis.x, p.z - gAxis.y));
    });
    bins.forEach((b, k) => { if (b.length) { b.sort((x, y) => x - y); prof[k] = b[Math.floor(b.length * 0.2)]; } });
  }
  const innerR = y => {
    const k = clamp(Math.floor((y - gBox.min.y) / Math.max(1e-6, gBox.max.y - gBox.min.y) * PROF_N), 0, PROF_N - 1);
    return prof[k];
  };
  const percR = perc ? (() => { const b = new Box3().setFromObject(perc); return Math.max(0.01, (b.max.x - b.min.x) * 0.22); })() : 0.03;

  // carb cap: the whole cap (knob, sleeve and its strap) on one pivot where it sits on the threads,
  // so it can rock, twist, spin or lift as a unit
  let capPivot = null, capRest = null;
  const capParts = ['CarbCap', 'AtomizerSleeve', 'AtomizerStrap'].map(part).filter(Boolean);
  if (capParts.length) {
    const cb = new Box3(); capParts.forEach(o => cb.expandByObject(o));
    const sleeve = part('AtomizerSleeve'), base = sleeve ? new Box3().setFromObject(sleeve) : cb;
    const c = cb.getCenter(new Vector3()); c.y = base.min.y;
    capPivot = new Group(); capParts[0].parent.add(capPivot);
    capPivot.position.copy(capParts[0].parent.worldToLocal(c.clone()));
    capPivot.updateMatrixWorld(true); capParts.forEach(o => capPivot.attach(o));
    capRest = capPivot.position.clone();
  }

  // ---- materials ----------------------------------------------------------------------------
  // light ring: emissive, with a mask that fills up around the base from the front (heat progress)
  const ringU = { uFill: { value: 1 }, uEdge: { value: 0 }, uChase: { value: -1 } };
  let ringMat = null;
  if (ring) {
    ringMat = new MeshPhysicalMaterial({ color: 0x1a1a20, roughness: 0.35, metalness: 0, vertexColors: false,
                                         emissive: 0xffffff, emissiveIntensity: 0, clearcoat: 0.6 });
    ringMat.onBeforeCompile = sh => {
      Object.assign(sh.uniforms, ringU);
      sh.vertexShader = 'varying vec3 vRingP;\n' + sh.vertexShader.replace('#include <begin_vertex>', '#include <begin_vertex>\nvRingP = position;');
      sh.fragmentShader = 'uniform float uFill, uEdge, uChase;\nvarying vec3 vRingP;\n' + sh.fragmentShader.replace('#include <emissivemap_fragment>',
        `#include <emissivemap_fragment>
         float ra = abs(atan(vRingP.x, vRingP.z)) / 3.14159265;          // 0 at the front, 1 at the back
         float lit = smoothstep(uFill + 0.03, uFill - 0.03, ra);
         float edge = exp(-pow((ra - uFill) * 14.0, 2.0)) * uEdge;      // bright leading edge while it fills
         float m = lit * 0.9 + 0.1 * step(0.999, uFill) + edge * 1.6;
         if (uChase >= 0.0) {                                           // a bright arc racing around the ring
           float a01 = atan(vRingP.x, vRingP.z) / 6.2831853 + 0.5;
           float d = fract(a01 - uChase + 0.5) - 0.5;
           m = 0.22 + 1.9 * exp(-d * d * 90.0);
         }
         totalEmissiveRadiance *= m;`);
    };
    ring.material = ringMat;
  }
  // glass: keep the model's own sunrise gradient for "match edition", or recolor it with the light color
  const glassMat = glass && glass.material, percMat = perc && perc.material;
  const gradMap = glassMat && glassMat.emissiveMap;
  let greyMap = null;
  if (gradMap && gradMap.image) {
    try {
      const im = gradMap.image, c = document.createElement('canvas');
      c.width = im.width; c.height = im.height;
      const g = c.getContext('2d'); g.drawImage(im, 0, 0);
      const d = g.getImageData(0, 0, c.width, c.height);
      for (let i = 0; i < d.data.length; i += 4) {
        const l = Math.max(d.data[i], d.data[i + 1], d.data[i + 2]) * 0.5 + (0.2126 * d.data[i] + 0.7152 * d.data[i + 1] + 0.0722 * d.data[i + 2]) * 0.5;
        d.data[i] = d.data[i + 1] = d.data[i + 2] = l;
      }
      g.putImageData(d, 0, 0);
      greyMap = new CanvasTexture(c); greyMap.colorSpace = SRGBColorSpace; greyMap.flipY = gradMap.flipY;
      greyMap.wrapS = gradMap.wrapS; greyMap.wrapT = gradMap.wrapT;
    } catch (e) { greyMap = null; }
  }
  const glassBase = glassMat ? { transmission: glassMat.transmission, opacity: glassMat.opacity } : null;
  if (glassMat) { glassMat.depthWrite = false; glassMat.envMapIntensity = 0.45; }
  for (const [m, k] of [[glassMat, 1], [percMat, 1]]) if (m) { m.emissive = new Color(1, 1, 1); m.userData.k = k; }
  const threadsMat = threads && threads.material;
  if (threadsMat) threadsMat.emissive = new Color(0, 0, 0);

  // lights that live inside the rig
  const glassLight = new PointLight(0xffffff, 0, 1.6 * MH, 2), heatLight = new PointLight(HEAT, 0, 1.2 * MH, 2);
  const ringLight = new PointLight(0xffffff, 0, 1.4 * MH, 2);
  glassLight.position.set(gAxis.x, gBox.min.y + 0.25 * (gBox.max.y - gBox.min.y), gAxis.y);
  if (threads) { const tb = new Box3().setFromObject(threads); heatLight.position.copy(tb.getCenter(new Vector3())).add(new Vector3(0, 0.06 * MH, 0)); }
  ringLight.position.set(0, 0.015 * MH, baseR * 1.45);
  for (const l of [glassLight, heatLight, ringLight]) { l.layers.enableAll(); holder.add(l); }

  // ---- scene graph: floor > lift (pad / hop) > tilt > spin > model ---------------------------
  const rigRoot = new Group(), tiltG = new Group(), flipG = new Group(), flipIn = new Group(), spinG = new Group();
  flipG.position.y = MH * 0.5; flipIn.position.y = -MH * 0.5;          // flips turn about the middle of the rig
  rigRoot.add(tiltG); tiltG.add(flipG); flipG.add(flipIn); flipIn.add(spinG); spinG.add(holder); scene.add(rigRoot);

  // bubbles in the glass (instanced spheres), rendered after the glass so they show through it
  const NB = 90, bubbles = [];
  const bubMat = new MeshStandardMaterial({ color: 0xffffff, roughness: 0.08, metalness: 0, transparent: true,
                                            opacity: 0.55, emissive: 0xffffff, emissiveIntensity: 0.12, depthWrite: false });
  const bubMesh = new InstancedMesh(new SphereGeometry(1, 12, 8), bubMat, NB);
  bubMesh.count = 0; bubMesh.renderOrder = 3; bubMesh.frustumCulled = false;
  holder.add(bubMesh);
  const bubDummy = new Object3D();
  const waterTop = gBox.min.y + 0.42 * (gBox.max.y - gBox.min.y), waterBot = gBox.min.y + 0.05 * (gBox.max.y - gBox.min.y);

  // contact shadow + charging pad (layer 1: drawn after the rig, kept out of the reflection)
  const floor = new Group(); scene.add(floor);
  const shadowMat = new MeshBasicMaterial({ map: radialTexture([[0, 'rgba(0,0,0,0.75)'], [0.45, 'rgba(0,0,0,0.42)'], [1, 'rgba(0,0,0,0)']]),
                                            transparent: true, depthWrite: false, toneMapped: false });
  const shadow = new Mesh(new PlaneGeometry(1, 1), shadowMat);
  shadow.rotation.x = -Math.PI / 2; shadow.scale.set(baseR * 3.1, baseR * 2.7, 1); shadow.renderOrder = 1;
  floor.add(shadow);

  const PR = baseR * 1.5, PH = 0.035 * MH, BV = PH * 0.35;
  const pad = new Group();
  const padBody = new Mesh(new LatheGeometry([new Vector2(0, 0), new Vector2(PR - BV, 0), new Vector2(PR, BV),
                                              new Vector2(PR, PH - BV), new Vector2(PR - BV, PH), new Vector2(0, PH)], 96),
                           new MeshPhysicalMaterial({ color: 0x1b1c22, roughness: 0.4, metalness: 0.15, clearcoat: 0.8,
                                                      clearcoatRoughness: 0.25, transparent: true }));
  const padRingU = { uColor: { value: CHARGE.clone() }, uFill: { value: 0.5 }, uTime: { value: 0 }, uFull: { value: 0 }, uOp: { value: 0 } };
  const padRing = new Mesh(new RingGeometry(PR * 0.74, PR * 0.8, 160, 1), new ShaderMaterial({
    uniforms: padRingU, transparent: true, depthWrite: false, blending: AdditiveBlending,
    vertexShader: 'varying vec2 vP; void main(){ vP = position.xy; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: `uniform vec3 uColor; uniform float uFill, uTime, uFull, uOp; varying vec2 vP;
      void main() {
        float t = fract(atan(vP.x, -vP.y) / 6.2831853 + 1.0);            // 0 at the front, clockwise
        float on = smoothstep(uFill + 0.006, uFill - 0.006, t);
        float s = fract(t - uTime * 0.28); float sweep = exp(-s * s * 260.0) * (1.0 - uFull) * on;
        vec3 c = uColor * (0.12 + 0.8 * on) + vec3(1.0) * sweep * 0.7;
        float a = (0.25 + 0.75 * on + 0.6 * sweep) * uOp;
        gl_FragColor = vec4(c, a);
        #include <colorspace_fragment>
      }` }));
  padRing.rotation.x = -Math.PI / 2; padRing.position.y = PH + 0.0015 * MH;
  const ledMat = new MeshBasicMaterial({ color: CHARGE.clone(), transparent: true, toneMapped: false });
  const padLed = new Mesh(new CylinderGeometry(PH * 0.16, PH * 0.16, PR * 0.12, 16), ledMat);
  padLed.rotation.z = Math.PI / 2; padLed.position.set(0, PH * 0.5, PR * 0.995);
  const spillMat = new MeshBasicMaterial({ map: radialTexture([[0, 'rgba(255,255,255,0.9)'], [0.45, 'rgba(255,255,255,0.3)'], [1, 'rgba(255,255,255,0)']]),
                                           color: CHARGE.clone(), transparent: true, depthWrite: false, blending: AdditiveBlending, toneMapped: false });
  const spill = new Mesh(new PlaneGeometry(1, 1), spillMat);          // green light spilling out under the pad
  spill.rotation.x = -Math.PI / 2; spill.scale.set(PR * 3.0, PR * 2.4, 1); spill.position.y = -0.001 * MH; spill.renderOrder = -1;
  pad.add(spill, padBody, padRing, padLed); floor.add(pad);
  floor.traverse(o => o.layers.set(1));

  // ---- framing ------------------------------------------------------------------------------
  let cw = 1, ch = 1, view = 0, tilt = 8, fitKey = '', D = 6, yT = MH / 2, restFloorPx = 0;
  let want = { tip: 0.1, floor: 0.9 };
  const tmp = new Vector3();
  function project(p) { tmp.copy(p).project(camera); return [(tmp.x + 1) / 2 * cw, (1 - tmp.y) / 2 * ch]; }
  function placeCamera(d, y) {
    const e = tilt * DEG;
    camera.position.set(0, y + d * Math.sin(e), d * Math.cos(e));
    camera.lookAt(0, y, 0); camera.updateMatrixWorld(true);
  }
  function restTip() { return tip.clone().applyAxisAngle(new Vector3(0, 1, 0), view * DEG); }
  // pick camera distance and height so the tip and the front of the base land exactly where the
  // overlay's layout expects them (same spot as the photo style)
  function fit() {
    camera.aspect = cw / ch; camera.updateProjectionMatrix();
    const t0 = restTip(), f0 = new Vector3(0, 0, baseR);
    const P = (d, y) => { placeCamera(d, y); return [project(t0)[1], project(f0)[1]]; };
    const frac = Math.max(0.05, (want.floor - want.tip) / ch);
    D = MH / (frac * 2 * Math.tan(FOV / 2 * DEG)); yT = MH / 2;
    for (let i = 0; i < 24; i++) {
      const [a, b] = P(D, yT), ea = a - want.tip, eb = b - want.floor;
      if (Math.abs(ea) + Math.abs(eb) < 0.02) break;
      const hD = D * 1e-3, hy = 1e-3;
      const [a1, b1] = P(D + hD, yT), [a2, b2] = P(D, yT + hy);
      const j11 = (a1 - a) / hD, j12 = (a2 - a) / hy, j21 = (b1 - b) / hD, j22 = (b2 - b) / hy;
      const det = j11 * j22 - j12 * j21;
      if (Math.abs(det) < 1e-9) break;
      D = Math.max(0.5, D - (ea * j22 - eb * j12) / det);
      yT = yT - (j11 * eb - j21 * ea) / det;
    }
    placeCamera(D, yT);
    restFloorPx = project(new Vector3(0, 0, baseR))[1];
  }

  // ---- animation state ----------------------------------------------------------------------
  let q = QUALITY.high, qName = 'high', lastPhase = null, wasFull = false, wasOff = true;
  let spinYaw = 0, spinRate = 0, moments = [], capPh = 0, capSpin = 0, pushK = 1, ringFlash = 0;
  const env = { sway: 0, float: 0, rock: 0, bounce: 0, wobble: 0 };   // fades between motions
  const capS = { rx: 0, ry: 0, rz: 0, ly: 0 };
  // one-off reactions ("moments"): each returns how it moves the rig at u = 0..1 of its run
  const MOMENTS = {
    spin:    { dur: 1.6, f: (u, d) => ({ yaw: d * TAU * ease(u) }) },
    spin2:   { dur: 2.2, f: (u, d) => ({ yaw: d * 2 * TAU * ease(u) }) },
    hop:     { dur: 0.7, f: u => ({ lift: 0.08 * MH * Math.sin(Math.PI * u) }) },
    hopspin: { dur: 1.2, f: (u, d) => ({ lift: 0.11 * MH * Math.sin(Math.PI * u), yaw: d * TAU * ease(u) }) },
    flip:    { dur: 1.3, f: u => ({ lift: 0.2 * MH * Math.sin(Math.PI * u), flip: TAU * ease(u) }) },
    shake:   { dur: 0.9, f: u => ({ roll: 7 * DEG * Math.sin(u * Math.PI * 11) * (1 - u) }) },
    nod:     { dur: 1.1, f: u => ({ pitch: 15 * DEG * Math.sin(Math.PI * u) * Math.sin(Math.PI * u) }) },
  };
  const play = (kind, flash = true) => { if (MOMENTS[kind]) { moments.push({ kind, t: 0 }); if (flash) ringFlash = 1; } };
  const tiltS = new Spring(0, 7, 0.75), padS = new Spring(0, 7, 0.8), leanS = new Spring(0, 9, 0.9);
  const accent = new Color(), white = new Color(1, 1, 1);

  function setQuality(name) {
    q = QUALITY[name] || QUALITY.high; qName = name;
    if ('transmissionResolutionScale' in renderer) renderer.transmissionResolutionScale = q.tScale;
    if (glassMat) {
      glassMat.transmission = q.transmission ? glassBase.transmission : 0;
      glassMat.transparent = !q.transmission; glassMat.opacity = q.transmission ? glassBase.opacity : 0.3;
      glassMat.needsUpdate = true;
    }
  }
  setQuality(opts.quality || 'high');

  return {
    MH, get quality() { return qName; }, setQuality,
    /* play a reaction now: spin, spin2, hop, hopspin, flip, shake or nod */
    react(kind) { play(kind); },
    /* size the canvas (device pixels) and say where the tip and base must land, in canvas pixels */
    layout(pxW, pxH, tipPx, floorPx, viewName, tiltDeg) {
      const w = Math.max(2, Math.round(pxW * q.res)), h = Math.max(2, Math.round(pxH * q.res));
      if (canvas.width !== w || canvas.height !== h) renderer.setSize(w, h, false);
      cw = w; ch = h;
      view = VIEWS[viewName] ?? 0; tilt = clamp(+tiltDeg || 0, -5, 40);
      want = { tip: tipPx * q.res, floor: floorPx * q.res };
      const key = [w, h, view, tilt, want.tip.toFixed(1), want.floor.toFixed(1)].join('/');
      if (key !== fitKey) { fitKey = key; fit(); }
    },
    /* draw one frame. s = state from the overlay. Returns where the mouthpiece is (canvas px, 0-1). */
    frame(dt, T, s, refl) {
      const ph = s.phase, idle = ph === 'off' || ph === 'standby', busy = !idle;
      // -- moments: one-off reactions when something happens
      if (lastPhase !== null && ph !== lastPhase) {
        if (ph === 'heating') play(s.evHeat, false);
        if (ph === 'active') play(s.evReady);
        if (lastPhase === 'off' && ph !== 'off') play(s.evConnect);
      }
      lastPhase = ph;
      const full = s.onPad && s.full;
      if (full && !wasFull) play(s.evFull);
      wasFull = full;
      const dir = s.spinDir < 0 ? -1 : 1, amt = Math.max(0, s.motionAmt ?? 1);
      let mYaw = 0, mLift = 0, mRoll = 0, mPitch = 0, mFlip = 0, mSq = 0;
      for (let i = moments.length - 1; i >= 0; i--) {
        const m = moments[i], M = MOMENTS[m.kind];
        m.t += dt; const u = clamp(m.t / M.dur, 0, 1), r = M.f(u, dir);
        mYaw += r.yaw || 0; mLift += r.lift || 0; mRoll += r.roll || 0; mPitch += r.pitch || 0; mFlip += r.flip || 0;
        if (u >= 1) moments.splice(i, 1);
      }
      // -- the motion picked for this mode: still, slow/fast spin, sway, float, rock, bounce, wobble
      const mo = s.motion || 'still';
      const rate = mo === 'spin_slow' ? 0.4 * Math.max(0.05, s.slowSpd ?? 1) : mo === 'spin_fast' ? 2.6 * Math.max(0.05, s.fastSpd ?? 1) : 0;
      spinRate = approach(spinRate, rate * dir, 2.2, dt);           // spins speed up and slow down smoothly
      spinYaw += spinRate * dt;
      if (!rate && Math.abs(spinRate) < 0.2) spinYaw = approach(spinYaw, Math.round(spinYaw / TAU) * TAU, 2.4, dt);   // settle facing front
      for (const k in env) env[k] = approach(env[k], mo === k ? 1 : 0, 2.4, dt);
      mYaw += 0.45 * amt * env.sway * Math.sin(T * 0.6);
      mLift += 0.035 * MH * amt * env.float * (0.5 + 0.5 * Math.sin(T * 1.3));
      mRoll += 7 * DEG * amt * env.rock * Math.sin(T * 1.05);
      const bp = Math.abs(Math.sin(T * 2.6));
      mLift += 0.06 * MH * amt * env.bounce * bp;
      mSq += 0.07 * amt * env.bounce * Math.max(0, 1 - bp * 5);
      mPitch += 4.5 * DEG * amt * env.wobble * Math.sin(T * 1.8); mRoll += 4.5 * DEG * amt * env.wobble * Math.cos(T * 1.8);
      if (s.drift) { mYaw += 2.2 * DEG * Math.sin(T * 0.23) + 1.2 * DEG * Math.sin(T * 0.57); mPitch += 1.2 * DEG * Math.sin(T * 0.31 + 1); }
      if (s.hitShake && ph === 'active' && s.inhaling) mRoll += 0.9 * DEG * s.ramp * Math.sin(T * 38);
      const yaw = view * DEG + spinYaw + mYaw + (s.entYaw || 0);
      // -- tip toward the camera on each inhale; lean into swoops; entrance tilt
      const tiltT = s.tiltHit && ph === 'active' && s.inhaling ? 6.5 * DEG * Math.max(0, s.tiltAmt ?? 1) * s.ramp : 0;
      tiltG.rotation.x = tiltS.step(tiltT, dt) + (s.entTilt || 0) + mPitch;
      tiltG.rotation.z = leanS.step(-(s.lean || 0) * DEG, dt) + mRoll;
      flipG.rotation.x = mFlip;
      spinG.rotation.y = yaw;
      // -- the camera eases in while it heats and stays in for the session
      const pushT = s.pushIn ? (ph === 'heating' ? 1 + 0.07 * s.prog : ph === 'active' ? 1.07 : 1) : 1;
      pushK = approach(pushK, pushT, 1.6, dt);
      // -- charging pad: fades/grows in, the rig settles onto it
      const pa = clamp(padS.step(s.onPad ? 1 : 0, dt), 0, 1.2);
      pad.visible = pa > 0.003;
      pad.scale.setScalar(0.82 + 0.18 * clamp(pa, 0, 1));
      padBody.material.opacity = clamp(pa, 0, 1); ledMat.opacity = clamp(pa, 0, 1);
      padRingU.uOp.value = clamp(pa, 0, 1); padRingU.uFill.value = clamp((s.battery ?? 0) / 100, 0.02, 1);
      padRingU.uTime.value = T; padRingU.uFull.value = full ? 1 : 0;
      ledMat.color.copy(CHARGE).multiplyScalar(full ? 1 : 0.6 + 0.4 * (0.5 + 0.5 * Math.sin(T * 3)));
      spillMat.opacity = clamp(pa, 0, 1) * (full ? 0.45 : 0.32 + 0.14 * Math.sin(T * 2.2));
      let lift = PH * clamp(pa, 0, 1.2);
      lift += mLift;
      const sq = (s.squash || 0) + mSq;
      rigRoot.position.y = lift;
      rigRoot.scale.set(pushK * (1 + 0.5 * sq), pushK * (1 - sq), pushK * (1 + 0.5 * sq));
      shadow.position.y = (s.onPad ? PH * clamp(pa, 0, 1) : 0) + 0.002 * MH;
      shadowMat.opacity = s.shadow ? 0.6 * clamp(1 - (lift - PH * clamp(pa, 0, 1)) / (0.25 * MH), 0.2, 1) : 0;
      // -- carb cap: rocks / twists / spins / lifts during a session (bigger on the inhale)
      if (capPivot) {
        const on = ph === 'active' && s.cap, k = on ? (s.inhaling ? 1 : 0.65) * Math.max(0, s.capAmt ?? 1) : 0, st = s.capStyle || 'rock_twist';
        capPh += dt * (s.inhaling ? 4.2 : 3.0);
        const w = Math.sin(capPh) + 0.18 * Math.sin(capPh * 2.3 + 1.1);
        const rock = st === 'rock' || st === 'rock_twist', twist = st === 'twist' || st === 'rock_twist' || st === 'lift';
        capS.rz = approach(capS.rz, rock ? 13 * DEG * k * w : 0, 8, dt);
        capS.rx = approach(capS.rx, rock ? 5 * DEG * k * Math.cos(capPh * 1.13) : 0, 8, dt);
        if (st === 'spin' && on) { capSpin += dt * 3.2 * (0.4 + k); capS.ry = capSpin; }
        else { capSpin = Math.round(capSpin / TAU) * TAU; capS.ry = approach(capS.ry, twist ? 38 * DEG * k * w : 0, 7, dt); }
        capS.ly = approach(capS.ly, st === 'lift' && on ? 0.028 * MH * k * (0.55 + 0.45 * Math.sin(capPh * 0.5)) : 0, 6, dt);
        capPivot.rotation.set(capS.rx, capS.ry, capS.rz);
        capPivot.position.set(capRest.x, capRest.y + capS.ly, capRest.z);
      }
      // -- lights
      accent.setRGB(s.color[0], s.color[1], s.color[2], SRGBColorSpace);
      ringFlash = Math.max(0, ringFlash - dt * 1.4);
      if (ringMat) {
        ringMat.emissive.copy(accent);
        const heating = ph === 'heating', rh = s.ringHeat || 'fill';
        const pulse = heating && rh === 'pulse' ? 0.45 + 0.55 * (0.5 + 0.5 * Math.sin(T * 5.5)) : 1;
        ringMat.emissiveIntensity = 3.2 * (s.ring * pulse + ringFlash * 0.8);
        const fill = heating && rh === 'fill' ? s.fill : 1;
        ringU.uFill.value = fill; ringU.uEdge.value = fill < 0.999 ? 1 : 0;
        ringU.uChase.value = heating && rh === 'chase' ? (T * (0.45 + 0.9 * s.prog)) % 1 : -1;
      }
      ringLight.color.copy(accent); ringLight.intensity = 0.35 * (s.ring + ringFlash);
      const useGrad = s.editionLight && gradMap;
      for (const m of [glassMat, percMat]) {
        if (!m) continue;
        const map = useGrad ? gradMap : (greyMap || gradMap);
        if (m.emissiveMap !== map) { m.emissiveMap = map; m.needsUpdate = true; }
        m.emissive.copy(useGrad ? white : accent);
        m.emissiveIntensity = m === glassMat ? 0.22 + 1.5 * s.glass : 0.45 + 2.6 * s.glass;
      }
      glassLight.color.copy(useGrad ? new Color(1, 0.62, 0.42) : accent); glassLight.intensity = 1.6 * s.glass;
      if (threadsMat) threadsMat.emissive.copy(HEAT).multiplyScalar(1.4 * s.heat);
      heatLight.intensity = 1.2 * s.heat;
      // -- bubbles
      const want_b = s.bubbles && ph === 'active' && s.inhaling ? 34 * Math.min(1, s.sT / 0.25 + 0.2) : 0;
      if (glass) {
        let n = want_b * dt + (bubbles.carry || 0);
        while (n >= 1 && bubbles.length < NB) {
          n--;
          const y = waterBot + Math.random() * 0.08 * MH, a = Math.random() * TAU;
          const r = percR + Math.random() * Math.max(0.005, innerR(y) * 0.82 - percR);
          bubbles.push({ x: gAxis.x + Math.cos(a) * r, z: gAxis.y + Math.sin(a) * r, y, a, r,
                         s: (0.004 + Math.random() * 0.011) * MH, v: (0.18 + Math.random() * 0.16) * MH, seed: Math.random() * 9 });
        }
        bubbles.carry = n;
        let k = 0;
        for (let i = bubbles.length - 1; i >= 0; i--) {
          const b = bubbles[i];
          b.y += b.v * dt;
          if (b.y > waterTop) { bubbles.splice(i, 1); continue; }
          const rr = Math.min(b.r, innerR(b.y) * 0.85), wob = Math.sin(T * 9 + b.seed) * 0.004 * MH;
          bubDummy.position.set(gAxis.x + Math.cos(b.a) * rr + wob, b.y, gAxis.y + Math.sin(b.a) * rr);
          bubDummy.scale.setScalar(b.s * (1 - 0.4 * clamp((b.y - waterTop + 0.05 * MH) / (0.05 * MH), 0, 1)));
          bubDummy.updateMatrix(); bubMesh.setMatrixAt(k++, bubDummy.matrix);
        }
        bubMesh.count = k; bubMesh.instanceMatrix.needsUpdate = true;
      }
      // -- render: rig (copied flipped into the reflection), then floor stuff on top
      scene.updateMatrixWorld(true);
      renderer.clear();
      camera.layers.set(0); renderer.render(scene, camera);
      if (refl) {
        const ctx = refl.getContext('2d'), k = refl.width / Math.max(1, cw);
        ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, refl.width, refl.height);
        if (s.reflection) { ctx.setTransform(k, 0, 0, -k, 0, restFloorPx * k); ctx.drawImage(canvas, 0, 0); }
      }
      camera.layers.set(1); renderer.render(scene, camera);
      // -- where the smoke should come out, and which way the glass points
      const tipW = tip.clone(), lowW = tip.clone().add(new Vector3(0, -0.2 * MH, 0));
      holder.localToWorld(tipW); holder.localToWorld(lowW);
      const [tx, ty] = project(tipW), [lx, ly] = project(lowW);
      const len = Math.hypot(tx - lx, ty - ly) || 1;
      return { mouth: [tx / cw, ty / ch], axis: [(tx - lx) / len, (ty - ly) / len], floor: restFloorPx / ch };
    },
    dispose() { renderer.dispose(); pmrem.dispose(); },
  };
}
