"use strict";

/* ============================== helpers ============================== */
const $ = (id) => document.getElementById(id);
const RING_C = 2 * Math.PI * 52;
const OVERLAY_ALPHA = 0.5;

/* The detection overlay is ONE fixed colour — neon green (#39ff14). There is no
   picker, no auto pick and no second colour anywhere; keep in step with
   engine.OVERLAY_GREEN. */
const OVERLAY_RGB = [57, 255, 20];

/* Ends of the sensitivity ladder, in step with detect.LADDER_STRICT / _LOOSE.
   Used only to label the slider with the multiplier it applies to the detection
   bar; the detection itself is driven entirely by the level map. */
const LADDER_STRICT = 4.0, LADDER_LOOSE = 0.25;

function headers(json) { const h = {}; if (json) h["Content-Type"] = "application/json"; return h; }

function safeStem(name) {
  return (name || "image").replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._]+|[._]+$/g, "") || "image";
}
function showView(id) { ["uploadView", "loadingView", "resultsView"].forEach((v) => $(v).classList.toggle("hidden", v !== id)); }
async function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/* progress toast */
function makeToast(label) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = '<div class="toast-label"></div><div class="toast-track"><div class="toast-fill"></div></div>';
  el.querySelector(".toast-label").textContent = label;
  $("toasts").appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  const fill = el.querySelector(".toast-fill"), lab = el.querySelector(".toast-label");
  const dismiss = (ms) => setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 400); }, ms);
  return {
    set(frac, text) { fill.style.width = Math.max(0, Math.min(1, frac)) * 100 + "%"; if (text) lab.textContent = text; },
    done(text) { el.classList.add("ok"); fill.style.width = "100%"; if (text) lab.textContent = text; dismiss(1100); },
    fail(text) { el.classList.add("err"); if (text) lab.textContent = text; dismiss(3600); },
  };
}
async function streamedDownload(url, body, filename, label, estBytes) {
  const t = makeToast(label);
  try {
    const opts = body == null ? { method: "GET", headers: headers(false) }
      : { method: "POST", headers: headers(true), body: JSON.stringify(body) };
    const res = await fetch(url, opts);
    if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch (e) {} throw new Error(m); }
    const missing = parseInt(res.headers.get("X-ImageSL-Missing") || "0", 10) || 0;
    if (!res.body || !res.body.getReader) { await downloadBlob(await res.blob(), filename); t.done("Saved " + filename); return; }
    const reader = res.body.getReader();
    const chunks = []; let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value); received += value.length;
      t.set(estBytes ? (received / estBytes) * 0.92 : 0.4, `${label} · ${(received / 1048576).toFixed(1)} MB`);
    }
    // An archive with nothing in it is a failure, not a download — say so rather
    // than handing over an empty folder that looks like it worked.
    if (!received) throw new Error("the server returned an empty file");
    await downloadBlob(new Blob(chunks), filename);
    if (missing) t.fail(`Saved ${filename} — but ${missing} slide${missing === 1 ? "" : "s"} had expired and ${missing === 1 ? "is" : "are"} not included`);
    else t.done("Saved " + filename);
  } catch (e) { t.fail("Export failed: " + e.message); }
}

/* ============================== level-map overlay ==============================
   The server sends ONE image carrying two channels:

     R — the first sensitivity level at which the detector calls this pixel
         positive (255 = never);
     G — 255 on tissue, 0 on slide background.

   Together they carry the whole family of results AND the denominator, so the
   browser reproduces the server's area-based, object-by-object decision — and
   its percentage — at any sensitivity and under any region, by comparison
   alone:

       positive =  level[i] <= sensitivity  &&  tissue[i]  &&  allowed[i]
       measured =  tissue[i] && allowed[i]
       percent  =  positive / measured

   Carrying the tissue channel is what makes the on-screen percentage move
   correctly when a region is drawn. A focus or ignore region changes the area
   being measured, so it moves the denominator as well as the numerator;
   without the mask the browser divided by the whole slide's tissue count and
   showed a percentage that did not match the one the server reported — and
   therefore did not match the CSV — for the very same regions. */
const NEVER = 255;

function buildLevelData(levelImg) {
  const W = levelImg.naturalWidth, H = levelImg.naturalHeight;
  const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  cx.drawImage(levelImg, 0, 0);
  const raw = cx.getImageData(0, 0, W, H).data;
  const n = W * H;
  const data = new Uint8Array(n);
  const tissue = new Uint8Array(n);

  // Older builds sent a GRAYSCALE level map with no tissue channel, and a
  // returning user's saved batch still holds one. There R === G, so reading the
  // green channel as a tissue mask would mark every pixel the detector called
  // positive (level 0-24) as *background* and report 0.00% for the whole slide.
  // The new encoding puts only 0 or 255 in green, so anything in between
  // identifies the old format, which is then treated as all-tissue — exactly
  // what that build assumed.
  let hasTissueChannel = true;
  for (let j = 1; j < raw.length; j += 4) {
    const g = raw[j];
    if (g !== 0 && g !== 255) { hasTissueChannel = false; break; }
  }

  let tissueCount = 0;
  for (let i = 0, j = 0; i < n; i++, j += 4) {
    data[i] = raw[j];
    const t = hasTissueChannel ? (raw[j + 1] === 255 ? 1 : 0) : 1;
    tissue[i] = t; tissueCount += t;
  }

  const ov = document.createElement("canvas"); ov.width = W; ov.height = H;
  const ovCtx = ov.getContext("2d");
  const iso = document.createElement("canvas"); iso.width = W; iso.height = H;
  const isoCtx = iso.getContext("2d");
  const tmp = document.createElement("canvas"); tmp.width = W; tmp.height = H;
  const tmpCtx = tmp.getContext("2d");

  return {
    W, H, data, tissue, tissueCount,
    allow: null,                        // null = everywhere; else 0/1 per pixel
    mask: new Uint8Array(n),
    count: 0,                           // positive pixels at the current setting
    measured: tissueCount,              // tissue pixels the percentage divides by
    ov, ovCtx, ovData: ovCtx.createImageData(W, H),
    iso, isoCtx, isoData: isoCtx.createImageData(W, H),
    tmp, tmpCtx,
  };
}

/* Rasterise one drawn region into a 0/1 mask.

   A pixel is inside when its CENTRE is inside the shape. That is the same rule
   ihc/regions.py states and implements, written out here rather than delegated
   to a canvas fill — because a canvas fill is not that rule. It excludes the far
   edge of a rect where PIL includes it, and it antialiases the boundary, so the
   two sides disagreed by a row and a column: 368 pixels of denominator on a
   512x384 frame, i.e. an on-screen percentage that did not match the exported
   one. Both sides now compute the mask arithmetically and agree exactly. */
function rasterRegion(ld, region) {
  const { W, H } = ld;
  // 1.0 is the OUTER EDGE of the last pixel, matching ihc/regions.py `_pts`.
  const pts = (region.points || []).map((p) => [p[0] * W, p[1] * H]);
  if (!pts.length) return null;
  const mask = new Uint8Array(W * H);

  if (region.kind === "rect" && pts.length >= 2) {
    const xa = Math.min(pts[0][0], pts[1][0]), xb = Math.max(pts[0][0], pts[1][0]);
    const ya = Math.min(pts[0][1], pts[1][1]), yb = Math.max(pts[0][1], pts[1][1]);
    const c0 = Math.max(0, Math.ceil(xa - 0.5)), c1 = Math.min(W - 1, Math.floor(xb - 0.5));
    const r0 = Math.max(0, Math.ceil(ya - 0.5)), r1 = Math.min(H - 1, Math.floor(yb - 0.5));
    for (let r = r0; r <= r1; r++) mask.fill(1, r * W + c0, r * W + c1 + 1);
    return mask;
  }
  if (pts.length < 3) return null;

  // Even-odd scanline fill at pixel centres — the mirror of `_polygon_mask`.
  const n = pts.length;
  const xi = new Float64Array(n);
  for (let r = 0; r < H; r++) {
    const yy = r + 0.5;
    let m = 0;
    for (let i = 0, j = n - 1; i < n; j = i++) {
      const [xI, yI] = pts[i], [xJ, yJ] = pts[j];
      if (yI === yJ) continue;
      if ((yI > yy) !== (yJ > yy)) xi[m++] = xI + ((yy - yI) * (xJ - xI)) / (yJ - yI);
    }
    if (m < 2) continue;
    const cross = Array.prototype.slice.call(xi, 0, m).sort((a, b) => a - b);
    for (let k = 0; k + 1 < m; k += 2) {
      const c0 = Math.max(0, Math.ceil(cross[k] - 0.5));
      const c1 = Math.min(W - 1, Math.floor(cross[k + 1] - 0.5));
      if (c1 >= c0) mask.fill(1, r * W + c0, r * W + c1 + 1);
    }
  }
  return mask;
}

function applyRegions(ld, regions) {
  const n = ld.W * ld.H;
  ld.allow = null;
  if (!regions || !regions.length) return;
  let focus = null, ignore = null;
  for (const r of regions) {
    const px = rasterRegion(ld, r);
    if (!px) continue;
    if (r.mode === "focus") {
      if (!focus) focus = new Uint8Array(n);
      for (let i = 0; i < n; i++) if (px[i]) focus[i] = 1;
    } else if (r.mode === "ignore") {
      if (!ignore) ignore = new Uint8Array(n);
      for (let i = 0; i < n; i++) if (px[i]) ignore[i] = 1;
    }
  }
  if (focus || ignore) {
    const allow = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      allow[i] = (focus ? focus[i] : 1) && !(ignore && ignore[i]) ? 1 : 0;
    }
    ld.allow = allow;
  }
}

/* Reproduces ihc/regions.py `apply()` exactly, and counts BOTH sides of the
   percentage: the positive pixels and the tissue they are measured against. */
function computeMask(ld, level, maxLevel) {
  const { data, tissue, allow, mask } = ld;
  const lim = level < 0 ? 0 : (level > maxLevel ? maxLevel : level);
  let count = 0, measured = 0;
  for (let i = 0; i < data.length; i++) {
    const inArea = tissue[i] && (!allow || allow[i]);
    if (inArea) measured++;
    const on = inArea && data[i] <= lim && data[i] !== NEVER;
    mask[i] = on ? 1 : 0;
    if (on) count++;
  }
  ld.count = count;
  ld.measured = measured;
  return count;
}

function tintOverlay(ld) {
  const [r, g, b] = OVERLAY_RGB;
  const a = Math.round(OVERLAY_ALPHA * 255);
  const px = ld.ovData.data, mask = ld.mask;
  for (let i = 0, j = 0; i < mask.length; i++, j += 4) {
    if (mask[i]) { px[j] = r; px[j + 1] = g; px[j + 2] = b; px[j + 3] = a; }
    else px[j + 3] = 0;
  }
  ld.ovCtx.putImageData(ld.ovData, 0, 0);
}

function drawLevelOverlay(canvas, origImg, ld) {
  const W = ld.W, H = ld.H;
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;
  tintOverlay(ld);
  const cx = canvas.getContext("2d");
  cx.clearRect(0, 0, W, H);
  cx.drawImage(origImg, 0, 0, W, H);
  cx.drawImage(ld.ov, 0, 0, W, H);
}

function overlayThumb(origImg, ld, maxW) {
  let w = ld.W, h = ld.H;
  if (maxW && w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const cx = cv.getContext("2d");
  tintOverlay(ld);
  cx.drawImage(origImg, 0, 0, w, h);
  cx.drawImage(ld.ov, 0, 0, w, h);
  return cv.toDataURL("image/jpeg", 0.85);
}

/* STAIN ONLY — keep the detected structures in their true colours, on white.
   Same live rule as the overlay, so the panel tracks the controls exactly. */
function isolateThumb(origImg, ld, maxW) {
  const W = ld.W, H = ld.H;
  const px = ld.isoData.data, mask = ld.mask;
  for (let i = 0, j = 0; i < mask.length; i++, j += 4) {
    if (mask[i]) { px[j] = px[j + 1] = px[j + 2] = 255; px[j + 3] = 255; }
    else px[j + 3] = 0;
  }
  ld.isoCtx.putImageData(ld.isoData, 0, 0);

  const tc = ld.tmpCtx;                       // original ∩ mask
  tc.globalCompositeOperation = "source-over";
  tc.clearRect(0, 0, W, H);
  tc.drawImage(origImg, 0, 0, W, H);
  tc.globalCompositeOperation = "destination-in";
  tc.drawImage(ld.iso, 0, 0);
  tc.globalCompositeOperation = "source-over";

  let w = W, h = H;                           // flatten onto white
  if (maxW && w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const cx = cv.getContext("2d");
  cx.fillStyle = "#ffffff"; cx.fillRect(0, 0, w, h);
  cx.drawImage(ld.tmp, 0, 0, w, h);
  return cv.toDataURL("image/jpeg", 0.85);
}

/* ============================== IndexedDB persistence ============================== */
const DB_NAME = "imagesl", STORE = "state", KEY = "batch";
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open(DB_NAME, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE);
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
}
async function saveState() {
  try {
    const db = await idb();
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put({ analyses, skipped, failures, ts: Date.now() }, KEY);
    await new Promise((r) => (tx.oncomplete = r));
  } catch (e) {}
}
let _saveDeb;
function saveStateSoon() { clearTimeout(_saveDeb); _saveDeb = setTimeout(saveState, 500); }
async function loadState() {
  try {
    const db = await idb();
    const tx = db.transaction(STORE, "readonly");
    return await new Promise((res) => { const q = tx.objectStore(STORE).get(KEY); q.onsuccess = () => res(q.result); q.onerror = () => res(null); });
  } catch (e) { return null; }
}
async function clearState() { try { const db = await idb(); const tx = db.transaction(STORE, "readwrite"); tx.objectStore(STORE).delete(KEY); } catch (e) {} }

/* ============================== state ============================== */
let analyses = [];   // [{ id, filename, data }]
let skipped = [];    // [{ filename, reason }] — read, but not a stained slide
let failures = [];   // [{ filename, reason }] — could not be analyzed at all
let mode = "auto";   // "auto" | "select"
let selectedStain = null;   // { key, name, compartment_name, ... }
let stainList = null;       // cached /api/stains

/* ============================== mode selector + stain picker ============================== */
document.querySelectorAll(".mode-card").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".mode-card").forEach((b) => { b.classList.toggle("active", b === btn); b.setAttribute("aria-selected", b === btn ? "true" : "false"); });
    mode = btn.dataset.mode;
    $("stainPicker").classList.toggle("hidden", mode !== "select");
    if (mode === "select") { ensureStains(); $("stainSearch").focus(); }
  });
});
async function ensureStains() {
  if (stainList) return stainList;
  try { stainList = (await (await fetch("/api/stains")).json()).stains || []; }
  catch (e) { stainList = []; }
  return stainList;
}
function renderStainResults(query) {
  const box = $("stainResults");
  const q = (query || "").trim().toLowerCase();
  const items = (stainList || []).filter((s) => !q || s.name.toLowerCase().includes(q) || s.category.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q));
  box.innerHTML = "";
  if (!items.length) { box.innerHTML = '<div class="sp-empty">No stain matches that search.</div>'; box.classList.add("show"); return; }
  let cat = null;
  items.slice(0, 80).forEach((s) => {
    if (s.category !== cat) { cat = s.category; const h = document.createElement("div"); h.className = "sp-cat"; h.textContent = cat; box.appendChild(h); }
    const it = document.createElement("div");
    it.className = "sp-item"; it.setAttribute("role", "option");
    it.innerHTML = `<span class="sp-sw"></span><span class="sp-nm"><b></b><small></small></span><span class="sp-comp"></span>`;
    it.querySelector(".sp-sw").style.background = s.swatch || "#999";
    it.querySelector("b").textContent = s.name;
    it.querySelector("small").textContent = s.description || "";
    it.querySelector(".sp-comp").textContent = s.enzyme || "";
    it.addEventListener("mousedown", (e) => { e.preventDefault(); chooseStain(s); });
    box.appendChild(it);
  });
  box.classList.add("show");
}
function chooseStain(s) {
  selectedStain = s;
  const chip = $("stainChosen");
  chip.innerHTML = `<span class="sp-sw"></span><b></b><span class="sp-comp"></span><button class="sp-change" type="button">Change</button>`;
  chip.querySelector(".sp-sw").style.background = s.swatch || "#999";
  chip.querySelector("b").textContent = s.name;
  chip.querySelector(".sp-comp").textContent = s.category;
  chip.querySelector(".sp-change").addEventListener("click", () => { selectedStain = null; chip.classList.add("hidden"); $("stainSearch").value = ""; $("stainSearch").focus(); renderStainResults(""); $("stainClear").classList.add("hidden"); });
  chip.classList.remove("hidden");
  $("stainResults").classList.remove("show");
  $("stainSearch").value = s.name;
  $("stainClear").classList.remove("hidden");
}
$("stainSearch") && $("stainSearch").addEventListener("input", (e) => { renderStainResults(e.target.value); $("stainClear").classList.toggle("hidden", !e.target.value); });
$("stainSearch") && $("stainSearch").addEventListener("focus", () => { if (!selectedStain) renderStainResults($("stainSearch").value); });
$("stainSearch") && $("stainSearch").addEventListener("blur", () => setTimeout(() => $("stainResults").classList.remove("show"), 150));
$("stainClear") && $("stainClear").addEventListener("click", () => { selectedStain = null; $("stainChosen").classList.add("hidden"); $("stainSearch").value = ""; $("stainClear").classList.add("hidden"); renderStainResults(""); $("stainSearch").focus(); });
function currentStainKey() { return (mode === "select" && selectedStain) ? selectedStain.key : ""; }

/* ============================== upload ============================== */
const dropzone = $("dropzone");
const fileInput = $("file");
dropzone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", (e) => { const f = e.target.files; e.target.value = ""; if (f.length) runBatch(f, false); });
["dragover", "dragenter"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
["dragleave", "dragend"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
dropzone.addEventListener("drop", (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); const f = e.dataTransfer.files; if (f && f.length) runBatch(f, false); });
$("fileAdd").addEventListener("change", (e) => { const f = e.target.files; e.target.value = ""; if (f.length) runBatch(f, true); });
$("btnAddMore").addEventListener("click", () => $("fileAdd").click());

/* ============================== batch analyze ============================== */
function setRing(fraction) {
  $("ringFill").style.strokeDashoffset = String(RING_C * (1 - fraction));
  $("ringPct").innerHTML = Math.round(fraction * 100) + "<small>%</small>";
}
/* A 4xx is a verdict about this file — retrying cannot change it. Anything else
   (a dropped connection, a gateway timeout, a server briefly out of memory
   under a big batch) is transient, and a single unlucky slide in a 46-slide run
   should not come back as "failed" when simply asking again would have worked. */
const RETRY_ATTEMPTS = 3;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function analyzeOnce(file) {
  const fd = new FormData();
  fd.append("file", file); fd.append("filename", file.name);
  const key = currentStainKey();
  if (key) fd.append("stain_key", key);
  const res = await fetch("/api/analyze", { method: "POST", headers: headers(false), body: fd });
  if (res.ok) {
    const data = await res.json();
    data.filename = data.filename || file.name;
    return data;
  }
  let msg = res.statusText;
  try { msg = (await res.json()).detail || msg; } catch (e) {}
  const err = new Error(msg);
  err.permanent = res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429;
  throw err;
}

async function analyzeOne(file, onRetry) {
  let last = null;
  for (let attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++) {
    try {
      return await analyzeOnce(file);
    } catch (e) {
      last = e;
      if (e && e.permanent) throw e;          // a verdict about the file itself
    }
    if (attempt < RETRY_ATTEMPTS) {
      if (onRetry) onRetry(attempt);
      await sleep(700 * attempt);
    }
  }
  throw last || new Error("Analysis failed");
}
async function runBatch(fileList, append) {
  const files = Array.from(fileList);
  $("uploadError").classList.add("hidden");
  if (!append) { analyses = []; skipped = []; failures = []; $("resultsContainer").innerHTML = ""; }
  showView("loadingView"); setRing(0);
  $("loaderSub").textContent = "Preparing…";
  const container = $("resultsContainer");
  const errors = [];
  let added = 0;
  for (let i = 0; i < files.length; i++) {
    $("loaderSub").textContent = `Analyzing ${i + 1} of ${files.length} — ${files[i].name}`;
    try {
      const data = await analyzeOne(files[i], (n) => {
        $("loaderSub").textContent =
          `Retrying ${files[i].name} (attempt ${n + 1} of ${RETRY_ATTEMPTS})…`;
      });
      const r = data.result || {};
      if (r.valid === false) {
        skipped.push({ filename: data.filename, reason: r.skip_reason || "Not recognized as a stained slide." });
      } else {
        analyses.push({ id: data.analysis_id, filename: data.filename, data });
        const card = createCard(data);
        card.style.animationDelay = Math.min(added * 60, 400) + "ms";
        container.appendChild(card); added++;
      }
    } catch (e) { errors.push({ filename: files[i].name, reason: (e && e.message) || "Analysis failed" }); }
    setRing((i + 1) / files.length);
  }
  failures = append ? failures.concat(errors) : errors;
  if (!analyses.length && !skipped.length) {
    showView("uploadView");
    const box = $("uploadError");
    box.textContent = "Could not analyze: " + errors.map((e) => `${e.filename}: ${e.reason}`).join(" · ");
    box.classList.remove("hidden");
    return;
  }
  await new Promise((r) => setTimeout(r, 300));
  renderSummary();
  showView("resultsView");
  if (!append) window.scrollTo({ top: 0, behavior: "smooth" });
  saveState();
}

/* ============================== results ============================== */
function renderSummary() {
  const n = analyses.length;
  const bits = [];
  if (skipped.length) bits.push(`${skipped.length} skipped`);
  if (failures.length) bits.push(`${failures.length} failed`);
  const note = bits.length ? ` <span class="sub">· ${bits.join(" · ")}</span>` : "";
  $("resultsCount").innerHTML = `${n} slide${n === 1 ? "" : "s"} analyzed${note}`;

  // Name every file that did not produce a measurement, and say why. A bare
  // "2 failed" is not something anyone can act on.
  const panel = $("skippedPanel"), list = $("skippedList");
  const items = skipped.map((s) => ({ ...s, kind: "skipped" }))
    .concat(failures.map((f) => ({ ...f, kind: "failed" })));
  if (items.length) {
    const parts = [];
    if (skipped.length) parts.push(`${skipped.length} skipped — not stained slides`);
    if (failures.length) parts.push(`${failures.length} could not be analyzed`);
    $("skippedTitle").textContent = parts.join(" · ");
    list.innerHTML = "";
    items.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = `<b></b><span class="why"></span>`;
      li.querySelector("b").textContent = s.filename;
      li.querySelector(".why").textContent = s.reason;
      if (s.kind === "failed") li.classList.add("failed");
      list.appendChild(li);
    });
    panel.classList.remove("hidden");
  } else panel.classList.add("hidden");
}

function currentLevel(data) {
  const p = data.params || {}, r = data.result || {};
  const lv = (p.level != null) ? p.level : r.level;
  return (lv == null ? (r.auto_level == null ? 100 : r.auto_level) : lv);
}

function createCard(data) {
  const node = $("resultTemplate").content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);
  const analysisId = data.analysis_id;
  const filename = data.filename;
  const stem = safeStem(filename);
  let current = data;
  const entry = analyses.find((a) => a.id === analysisId);
  const st = { origImg: null, ld: null };
  const maxLevel = ((data.result && data.result.level_count) || 201) - 1;
  let level = currentLevel(data);
  let regions = ((data.params && data.params.regions) || []).slice();

  q(".fname").textContent = filename;
  q(".cThr").max = String(maxLevel);
  function persistData() { if (entry) entry.data = current; saveStateSoon(); }

  /* ---- redraw: sensitivity + regions, entirely in the browser ---- */
  let rafPending = false;
  function redraw() {
    if (!st.origImg || !st.ld) return;
    computeMask(st.ld, level, maxLevel);
    drawLevelOverlay(q(".cmpFront"), st.origImg, st.ld);
    q(".imgOverlay").src = overlayThumb(st.origImg, st.ld, 560);
    q(".imgStainOnly").src = isolateThumb(st.origImg, st.ld, 560);
    drawRegionOutlines();
  }
  function redrawSoon() {
    if (document.hidden) { redraw(); return; }   // rAF is paused when hidden
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => { rafPending = false; redraw(); });
  }

  /* The live readout uses the SAME two counts the server divides — positive
     pixels over the tissue actually being measured — so drawing or removing a
     region moves the percentage to the value the server will report, and the
     CSV, for those exact regions.

     A region moves BOTH counts, which is why the percentage can go either way
     when you ignore something: cut out an area with little staining in it and
     the denominator falls faster than the numerator, so the percentage rises;
     cut out a heavily stained area and it falls. Both are correct — the figure
     is positive area over the tissue actually measured — but it is only
     obviously correct if the denominator is on screen, so it is spelled out
     whenever regions are in play. */
  function liveMetrics() {
    if (!st.ld) return;
    const pos = st.ld.count;
    const measured = st.ld.measured;
    const whole = st.ld.tissueCount;
    const pct = measured ? (pos / measured * 100) : 0;
    q(".mPercent").textContent = pct.toFixed(2) + "%";
    q(".mPositive").textContent = pos.toLocaleString();
    q(".mTissue").textContent = measured.toLocaleString();

    const scope = q(".mScope");
    if (scope) {
      if (regions.length && whole && measured !== whole) {
        scope.textContent = `measured over ${(measured / whole * 100).toFixed(1)}% of the `
          + `slide's tissue (${measured.toLocaleString()} of ${whole.toLocaleString()} px)`;
        scope.classList.remove("hidden");
      } else {
        scope.classList.add("hidden");
      }
    }
    [".mPercent", ".mPositive", ".mTissue"].forEach((s) => { const b = q(s); b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash"); });
  }

  /* The ladder is a multiplicative scaling of the bar a structure's peak has to
     clear, so the honest label is that multiplier — not a step index, which now
     runs to 200 and means nothing to a reader. ×0.72 says "counting structures
     down to 72% of the automatic bar", and it is the same statement on every
     slide. */
  function levelLabel() {
    const r = current.result || {};
    const auto = (r.auto_level == null ? 100 : r.auto_level);
    const n = (r.level_count == null ? 201 : r.level_count);
    if (level === auto) { q(".thrVal").textContent = "Auto"; return; }
    const mult = level < auto
      ? Math.pow(LADDER_STRICT, (auto - level) / auto)
      : Math.pow(LADDER_LOOSE, (level - auto) / Math.max(n - 1 - auto, 1));
    q(".thrVal").textContent = "×" + mult.toFixed(2);
  }

  function setLevel(v, persist) {
    level = Math.max(0, Math.min(maxLevel, v | 0));
    q(".cThr").value = String(level);
    levelLabel();
    redrawSoon(); liveMetrics();
    if (current.params) current.params.level = level;
    if (persist) { persistData(); postView(); }
  }

  function loadSources(origSrc, levelSrc) {
    const o = new Image(), s = new Image();
    let n = 0;
    const done = () => {
      if (++n !== 2) return;
      st.origImg = o;
      st.ld = buildLevelData(s);
      applyRegions(st.ld, regions);
      redraw(); liveMetrics();
    };
    o.onload = done; s.onload = done; o.src = origSrc; s.src = levelSrc;
  }

  function paint(d, keepView) {
    current = d;
    const img = d.images || {};
    if (img.original) { q(".imgOriginal").src = img.original; q(".imgOriginal2").src = img.original; }
    if (img.original && img.level) loadSources(img.original, img.level);
    const r = d.result;
    if (r) {
      q(".badge").textContent = r.stain_label || r.method || "";
      q(".mTissue").textContent = (r.tissue_pixels || 0).toLocaleString();
      q(".mObjects").textContent = (r.objects || 0).toLocaleString();
      if (!keepView) { level = currentLevel(d); q(".cThr").value = String(level); }
      q(".mPercent").textContent = (r.positive_percent || 0).toFixed(2) + "%";
      q(".mPositive").textContent = (r.positive_pixels || 0).toLocaleString();
      levelLabel();
      const note = q(".card-note");
      const text = (r.notes || []).join(" ");
      note.textContent = text;
      note.classList.toggle("hidden", !text);
    }
  }
  paint(data);

  /* ---- comparison slider ---- */
  const vp = q(".cmp-viewport");
  let dragging = false;
  function setSplit(clientX) {
    const rect = vp.getBoundingClientRect();
    let pct = ((clientX - rect.left) / rect.width) * 100;
    vp.style.setProperty("--split", Math.max(0, Math.min(100, pct)) + "%");
  }
  vp.addEventListener("pointerdown", (e) => {
    if (tool !== "off") return;                // drawing takes precedence
    dragging = true; vp.setPointerCapture(e.pointerId); setSplit(e.clientX);
  });
  vp.addEventListener("pointermove", (e) => { if (dragging) setSplit(e.clientX); });
  vp.addEventListener("pointerup", (e) => { dragging = false; try { vp.releasePointerCapture(e.pointerId); } catch (x) {} });
  vp.addEventListener("pointercancel", () => { dragging = false; });
  node.querySelectorAll(".vImg").forEach((im) => im.addEventListener("click", () => showLightbox(im.src)));

  /* ---- server recompute (debounced) ------------------------------------ #
     The browser has already redrawn; this is what makes the REPORTED numbers,
     the CSV and every export agree with what is on screen. */
  let viewDeb;
  function postView() {
    clearTimeout(viewDeb);
    viewDeb = setTimeout(async () => {
      try {
        const body = { analysis_id: analysisId, level: level, regions: regions };
        const res = await fetch("/api/appearance", { method: "POST", headers: headers(true), body: JSON.stringify(body) });
        if (res.ok) {
          const d = await res.json();
          if (d.result) { current.result = d.result; paint(d, true); persistData(); }
        }
      } catch (e) {}
    }, 350);
  }

  /* ---- sensitivity ---- */
  q(".cThr").addEventListener("input", () => setLevel(+q(".cThr").value, true));
  q(".thr-auto").addEventListener("click", () => {
    const auto = (current.result && current.result.auto_level);
    if (current.params) current.params.level = null;
    setLevel(auto == null ? 100 : auto, false);
    persistData(); postView();
  });

  /* ============================ manual regions ============================
     Coordinates are stored normalised, so a region drawn on screen is the same
     region the server rasterises for the numbers and for every export, at any
     resolution. */
  let tool = "off";
  let shape = "rect";
  let drawing = null;
  const draw = q(".cmpDraw");

  function regionColour(mode) {
    return mode === "focus" ? "#38bdf8" : "#f43f5e";
  }

  function sizeDrawCanvas() {
    const w = vp.clientWidth, h = vp.clientHeight;
    if (!w || !h) return;
    if (draw.width !== w || draw.height !== h) { draw.width = w; draw.height = h; }
  }

  function drawRegionOutlines() {
    sizeDrawCanvas();
    const cx = draw.getContext("2d");
    cx.clearRect(0, 0, draw.width, draw.height);
    const all = drawing ? regions.concat([drawing]) : regions;
    for (const r of all) {
      const pts = (r.points || []).map((p) => [p[0] * draw.width, p[1] * draw.height]);
      if (!pts.length) continue;
      cx.lineWidth = 2;
      cx.setLineDash(r.mode === "ignore" ? [6, 4] : []);
      cx.strokeStyle = regionColour(r.mode);
      cx.fillStyle = regionColour(r.mode) + "22";
      cx.beginPath();
      if (r.kind === "rect" && pts.length >= 2) {
        cx.rect(pts[0][0], pts[0][1], pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]);
      } else {
        cx.moveTo(pts[0][0], pts[0][1]);
        for (let i = 1; i < pts.length; i++) cx.lineTo(pts[i][0], pts[i][1]);
        if (r !== drawing) cx.closePath();
      }
      cx.fill(); cx.stroke();
    }
    cx.setLineDash([]);
  }

  function regionCountLabel() {
    const n = regions.length;
    q(".rcount").textContent = n === 0 ? "No regions" : (n === 1 ? "1 region" : n + " regions");
  }

  function commitRegions() {
    if (!st.ld) return;
    applyRegions(st.ld, regions);
    regionCountLabel();
    redrawSoon(); liveMetrics();
    if (current.params) current.params.regions = regions;
    persistData(); postView();
  }

  function toPoint(e) {
    const rect = vp.getBoundingClientRect();
    return [
      Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)),
    ];
  }

  node.querySelectorAll(".tool").forEach((b) => b.addEventListener("click", () => {
    tool = b.dataset.tool;
    node.querySelectorAll(".tool").forEach((o) => o.classList.toggle("is-on", o === b));
    vp.classList.toggle("drawing", tool !== "off");
  }));
  q(".tool-shape").addEventListener("change", (e) => { shape = e.target.value; });
  q(".tool-undo").addEventListener("click", () => { if (regions.length) { regions = regions.slice(0, -1); commitRegions(); } });
  q(".tool-clear").addEventListener("click", () => { if (regions.length) { regions = []; commitRegions(); } });

  draw.addEventListener("pointerdown", (e) => {
    if (tool === "off") return;
    e.preventDefault(); e.stopPropagation();
    try { draw.setPointerCapture(e.pointerId); } catch (x) {}
    drawing = { mode: tool, kind: shape, points: [toPoint(e)] };
    if (shape === "rect") drawing.points.push(drawing.points[0].slice());
    drawRegionOutlines();
  });
  draw.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    const p = toPoint(e);
    if (drawing.kind === "rect") drawing.points[1] = p;
    else if (drawing.points.length < 2048) drawing.points.push(p);
    drawRegionOutlines();
  });
  function endDraw(e) {
    if (!drawing) return;
    try { draw.releasePointerCapture(e.pointerId); } catch (x) {}
    const r = drawing; drawing = null;
    const big = r.kind === "rect"
      ? Math.abs(r.points[1][0] - r.points[0][0]) > 0.01 && Math.abs(r.points[1][1] - r.points[0][1]) > 0.01
      : r.points.length >= 3;
    if (big) { regions = regions.concat([r]); commitRegions(); }
    else drawRegionOutlines();
  }
  draw.addEventListener("pointerup", endDraw);
  draw.addEventListener("pointercancel", endDraw);
  window.addEventListener("resize", drawRegionOutlines);
  regionCountLabel();

  /* ---- downloads (always carry the CURRENT on-screen view) ----
     Sent as a POST body rather than a query string so the drawn regions travel
     with the sensitivity; a lasso does not fit in a URL, and without it a
     single-slide TIFF came back ignoring every region on screen. */
  function dlTif(type) {
    streamedDownload("/api/download_tif",
      { analysis_id: analysisId, image_type: type, level, regions },
      `${stem}_${type}.tif`, `Exporting ${type}`, 2 * 1048576);
  }
  node.querySelectorAll(".dl-btn").forEach((b) => b.addEventListener("click", () => dlTif(b.dataset.type)));
  node.querySelectorAll(".vdl").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); dlTif(b.dataset.type); }));
  q(".export-one").addEventListener("click", () => streamedDownload(
    "/api/export_csv",
    { analysis_ids: [analysisId], overrides: { [analysisId]: { level, regions } } },
    `${stem}_data.csv`, "Exporting CSV", 0));

  node.dataset.analysisId = analysisId;
  node._view = () => ({ level, regions });
  return node;
}

/* ============================== mass export ==============================
   Every export carries each card's CURRENT sensitivity and regions. The server
   re-measures any slide whose view differs from the one it last cached, so an
   export taken immediately after a change can never fall back on the previous
   settings' numbers. */
function currentOverrides() {
  // Keyed by the card's own analysis id, not by its position. Pairing the Nth
  // card with the Nth entry breaks the moment the two lists differ — and they
  // do, whenever a file is skipped or a batch is appended — which would attach
  // one slide's regions to another slide's export.
  const overrides = {};
  document.querySelectorAll("#resultsContainer .card").forEach((card) => {
    const id = card.dataset.analysisId;
    if (!id) return;
    const v = card._view ? card._view() : null;
    overrides[id] = v ? { level: v.level, regions: v.regions } : {};
  });
  return overrides;
}
// Refresh the batch immediately before an export, so a long review session
// cannot lose entries in the moment between the last heartbeat and the click.
$("btnExportCsv").addEventListener("click", async () => {
  const ids = analyses.map((a) => a.id); if (!ids.length) return;
  await keepAlive();
  streamedDownload("/api/export_csv", { analysis_ids: ids, overrides: currentOverrides() },
    "ImageSL_results.csv", "Exporting data (CSV)", 0);
});
$("btnDownloadZip").addEventListener("click", async () => {
  const ids = analyses.map((a) => a.id); if (!ids.length) return;
  const n = ids.length;
  await keepAlive();
  streamedDownload("/api/export_zip",
    { analysis_ids: ids, images: ["comparison"], include_csv: true, overrides: currentOverrides() },
    "ImageSL_export.zip", `Packaging ${n} slide${n === 1 ? "" : "s"} (ZIP)`, n * 1.6 * 1048576);
});
$("btnNew").addEventListener("click", () => {
  analyses = []; skipped = []; failures = [];
  $("resultsContainer").innerHTML = ""; $("skippedPanel").classList.add("hidden");
  clearState(); showView("uploadView"); window.scrollTo({ top: 0, behavior: "smooth" });
});

/* ============================== keep the batch alive ==============================
   The server holds each analysis for a limited time and has no other way to know
   a page is still using one. Reviewing forty slides — drawing regions, comparing,
   deciding a sensitivity — takes as long as it takes, and a batch left open while
   the user does something else used to expire underneath them: the export then
   failed, or succeeded and produced an empty archive. A heartbeat costs one tiny
   request and removes the whole class of problem. */
const KEEPALIVE_MS = 4 * 60 * 1000;

async function keepAlive() {
  const ids = analyses.map((a) => a.id);
  if (!ids.length) return;
  try {
    const res = await fetch("/api/keepalive", {
      method: "POST", headers: headers(true), body: JSON.stringify({ analysis_ids: ids }),
    });
    if (!res.ok) return;
    const d = await res.json();
    const expired = new Set(d.expired || []);
    if (!expired.size) return;
    // Say so where it is visible, rather than at the moment an export fails.
    document.querySelectorAll("#resultsContainer .card").forEach((card) => {
      if (!expired.has(card.dataset.analysisId)) return;
      card.classList.add("expired");
      const note = card.querySelector(".card-note");
      if (note) {
        note.textContent = "This slide is no longer held by the server — it will not be "
          + "included in an export. Re-upload it to measure it again.";
        note.classList.remove("hidden");
      }
    });
  } catch (e) { /* offline or asleep: the next beat will do */ }
}
setInterval(keepAlive, KEEPALIVE_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) keepAlive(); });

/* ============================== lightbox ============================== */
function showLightbox(src) { if (!src) return; $("lightboxImg").src = src; $("lightbox").classList.remove("hidden"); }
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));

/* ============================== restore on load ============================== */
(async function restore() {
  const stt = await loadState();
  if (!stt || (!(stt.analyses || []).length && !(stt.skipped || []).length)) return;
  analyses = (stt.analyses || []).filter((a) => a && a.data);
  skipped = stt.skipped || [];
  failures = stt.failures || [];
  const container = $("resultsContainer"); container.innerHTML = "";
  analyses.forEach((a) => container.appendChild(createCard(a.data)));
  renderSummary();
  showView("resultsView");
})();
