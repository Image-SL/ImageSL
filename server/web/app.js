"use strict";

/* ============================== helpers ============================== */
const $ = (id) => document.getElementById(id);
const RING_C = 2 * Math.PI * 52;
const OVERLAY_ALPHA = 0.5;

/* The detection overlay is ONE fixed colour — neon green (#39ff14). There is no
   picker, no auto pick and no second colour anywhere; keep in step with
   engine.OVERLAY_GREEN. */
const OVERLAY_RGB = [57, 255, 20];

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
    if (!res.body || !res.body.getReader) { await downloadBlob(await res.blob(), filename); t.done("Saved " + filename); return; }
    const reader = res.body.getReader();
    const chunks = []; let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value); received += value.length;
      t.set(estBytes ? (received / estBytes) * 0.92 : 0.4, `${label} · ${(received / 1048576).toFixed(1)} MB`);
    }
    await downloadBlob(new Blob(chunks), filename);
    t.done("Saved " + filename);
  } catch (e) { t.fail("Export failed: " + e.message); }
}

/* ============================== level-map overlay ==============================
   The server sends ONE 8-bit image: for every pixel, the first sensitivity level
   at which the detector calls it positive (255 = never). That single image
   carries the whole family of results, so the browser reproduces the server's
   area-based, object-by-object decision at any sensitivity — and with any
   per-region offset from the manual tools — with one comparison per pixel:

       positive  =  level_map[i] <= sensitivity + offset[i]     (and not ignored)

   No round-trip, and no way for the picture on screen to drift away from the
   numbers the server reports for the same settings. */
const NEVER = 255;

function buildLevelData(levelImg) {
  const W = levelImg.naturalWidth, H = levelImg.naturalHeight;
  const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  cx.drawImage(levelImg, 0, 0);
  const raw = cx.getImageData(0, 0, W, H).data;
  const data = new Uint8Array(W * H);
  for (let i = 0, j = 0; i < data.length; i++, j += 4) data[i] = raw[j];

  const ov = document.createElement("canvas"); ov.width = W; ov.height = H;
  const ovCtx = ov.getContext("2d");
  const iso = document.createElement("canvas"); iso.width = W; iso.height = H;
  const isoCtx = iso.getContext("2d");
  const tmp = document.createElement("canvas"); tmp.width = W; tmp.height = H;
  const tmpCtx = tmp.getContext("2d");
  const reg = document.createElement("canvas"); reg.width = W; reg.height = H;
  const regCtx = reg.getContext("2d", { willReadFrequently: true });

  return {
    W, H, data,
    offset: new Int16Array(W * H),      // per-pixel sensitivity shift (boost/damp)
    allow: null,                        // null = everywhere; else 0/1 per pixel
    mask: new Uint8Array(W * H),
    count: 0,
    ov, ovCtx, ovData: ovCtx.createImageData(W, H),
    iso, isoCtx, isoData: isoCtx.createImageData(W, H),
    tmp, tmpCtx, reg, regCtx,
  };
}

/* Rasterise the drawn regions into a per-pixel sensitivity offset plus an
   allow-mask. Mirrors ihc/regions.py: focus keeps only what it covers, ignore
   removes what it covers, boost/damp shift the level locally. */
function rasterRegion(ld, region) {
  const { W, H, regCtx: cx } = ld;
  cx.setTransform(1, 0, 0, 1, 0, 0);
  cx.clearRect(0, 0, W, H);
  cx.fillStyle = "#fff";
  cx.strokeStyle = "#fff";
  cx.lineJoin = "round";
  cx.lineCap = "round";
  const pts = (region.points || []).map((p) => [p[0] * (W - 1), p[1] * (H - 1)]);
  if (!pts.length) return null;
  if (region.kind === "rect" && pts.length >= 2) {
    const [x0, y0] = pts[0], [x1, y1] = pts[1];
    cx.fillRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
  } else if (pts.length >= 3) {
    cx.beginPath();
    cx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) cx.lineTo(pts[i][0], pts[i][1]);
    cx.closePath();
    cx.fill();
  } else {
    return null;
  }
  return cx.getImageData(0, 0, W, H).data;
}

function applyRegions(ld, regions) {
  const n = ld.W * ld.H;
  ld.offset.fill(0);
  ld.allow = null;
  if (!regions || !regions.length) return;
  let focus = null, ignore = null;
  for (const r of regions) {
    const px = rasterRegion(ld, r);
    if (!px) continue;
    if (r.mode === "focus") {
      if (!focus) focus = new Uint8Array(n);
      for (let i = 0, j = 3; i < n; i++, j += 4) if (px[j]) focus[i] = 1;
    } else if (r.mode === "ignore") {
      if (!ignore) ignore = new Uint8Array(n);
      for (let i = 0, j = 3; i < n; i++, j += 4) if (px[j]) ignore[i] = 1;
    } else {
      const d = r.delta | 0;
      for (let i = 0, j = 3; i < n; i++, j += 4) if (px[j]) ld.offset[i] += d;
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

function computeMask(ld, level, maxLevel) {
  const { data, offset, allow, mask } = ld;
  let count = 0;
  for (let i = 0; i < data.length; i++) {
    const v = data[i];
    if (v === NEVER) { mask[i] = 0; continue; }
    let lim = level + offset[i];
    if (lim < 0) lim = 0; else if (lim > maxLevel) lim = maxLevel;
    const on = v <= lim && (!allow || allow[i]);
    mask[i] = on ? 1 : 0;
    if (on) count++;
  }
  ld.count = count;
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
    tx.objectStore(STORE).put({ analyses, skipped, ts: Date.now() }, KEY);
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
let skipped = [];    // [{ filename, reason }]
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
async function analyzeOne(file) {
  const fd = new FormData();
  fd.append("file", file); fd.append("filename", file.name);
  const key = currentStainKey();
  if (key) fd.append("stain_key", key);
  const res = await fetch("/api/analyze", { method: "POST", headers: headers(false), body: fd });
  if (!res.ok) { let msg = res.statusText; try { msg = (await res.json()).detail || msg; } catch (e) {} throw new Error(msg); }
  const data = await res.json();
  data.filename = data.filename || file.name;
  return data;
}
async function runBatch(fileList, append) {
  const files = Array.from(fileList);
  $("uploadError").classList.add("hidden");
  if (!append) { analyses = []; skipped = []; $("resultsContainer").innerHTML = ""; }
  showView("loadingView"); setRing(0);
  $("loaderSub").textContent = "Preparing…";
  const container = $("resultsContainer");
  const errors = [];
  let added = 0;
  for (let i = 0; i < files.length; i++) {
    $("loaderSub").textContent = `Analyzing ${i + 1} of ${files.length} — ${files[i].name}`;
    try {
      const data = await analyzeOne(files[i]);
      const r = data.result || {};
      if (r.valid === false) {
        skipped.push({ filename: data.filename, reason: r.skip_reason || "Not recognized as a stained slide." });
      } else {
        analyses.push({ id: data.analysis_id, filename: data.filename, data });
        const card = createCard(data);
        card.style.animationDelay = Math.min(added * 60, 400) + "ms";
        container.appendChild(card); added++;
      }
    } catch (e) { errors.push(`${files[i].name}: ${e.message}`); }
    setRing((i + 1) / files.length);
  }
  if (!analyses.length && !skipped.length) {
    showView("uploadView");
    const box = $("uploadError"); box.textContent = "Could not analyze: " + errors.join(" · "); box.classList.remove("hidden");
    return;
  }
  await new Promise((r) => setTimeout(r, 300));
  renderSummary(errors);
  showView("resultsView");
  if (!append) window.scrollTo({ top: 0, behavior: "smooth" });
  saveState();
}

/* ============================== results ============================== */
function renderSummary(errors) {
  const n = analyses.length;
  const bits = [];
  if (skipped.length) bits.push(`${skipped.length} skipped`);
  if (errors && errors.length) bits.push(`${errors.length} failed`);
  const note = bits.length ? ` <span class="sub">· ${bits.join(" · ")}</span>` : "";
  $("resultsCount").innerHTML = `${n} slide${n === 1 ? "" : "s"} analyzed${note}`;
  const panel = $("skippedPanel"), list = $("skippedList");
  if (skipped.length) {
    $("skippedTitle").textContent = `${skipped.length} file${skipped.length === 1 ? "" : "s"} skipped — not stained slides`;
    list.innerHTML = "";
    skipped.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = `<b></b><span class="why"></span>`;
      li.querySelector("b").textContent = s.filename;
      li.querySelector(".why").textContent = s.reason;
      list.appendChild(li);
    });
    panel.classList.remove("hidden");
  } else panel.classList.add("hidden");
}

/* How far a "More/Less here" region shifts the sensitivity inside itself. */
const REGION_DELTA = 5;

function currentLevel(data) {
  const p = data.params || {}, r = data.result || {};
  const lv = (p.level != null) ? p.level : r.level;
  return (lv == null ? (r.auto_level == null ? 12 : r.auto_level) : lv);
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
  const maxLevel = ((data.result && data.result.level_count) || 25) - 1;
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

  function liveMetrics() {
    if (!st.ld) return;
    const pos = st.ld.count;
    const tissue = (current.result && current.result.tissue_pixels) || 0;
    const pct = tissue ? (pos / tissue * 100) : 0;
    q(".mPercent").textContent = pct.toFixed(2) + "%";
    q(".mPositive").textContent = pos.toLocaleString();
    [".mPercent", ".mPositive"].forEach((s) => { const b = q(s); b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash"); });
  }

  function levelLabel() {
    const r = current.result || {};
    const auto = (r.auto_level == null ? 12 : r.auto_level);
    const d = level - auto;
    q(".thrVal").textContent = d === 0 ? "Auto" : (d > 0 ? `Auto +${d}` : `Auto ${d}`);
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
    setLevel(auto == null ? 12 : auto, false);
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
    return mode === "focus" ? "#38bdf8"
      : mode === "ignore" ? "#f43f5e"
      : mode === "boost" ? "#22c55e" : "#f59e0b";
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
    // "Less here" is the same tool as "More here" with the shift reversed.
    const mode = tool === "damp" ? "boost" : tool;
    const delta = tool === "boost" ? REGION_DELTA : (tool === "damp" ? -REGION_DELTA : 0);
    drawing = { mode, kind: shape, delta, points: [toPoint(e)] };
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

  /* ---- downloads (always carry the CURRENT on-screen view) ---- */
  function dlTif(type) {
    const qs = `analysis_id=${analysisId}&image_type=${type}&level=${level}`;
    streamedDownload(`/api/download_tif?${qs}`, null, `${stem}_${type}.tif`, `Exporting ${type}`, 2 * 1048576);
  }
  node.querySelectorAll(".dl-btn").forEach((b) => b.addEventListener("click", () => dlTif(b.dataset.type)));
  node.querySelectorAll(".vdl").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); dlTif(b.dataset.type); }));
  q(".export-one").addEventListener("click", () => streamedDownload("/api/export_csv", { analysis_ids: [analysisId] }, `${stem}_data.csv`, "Exporting CSV", 0));

  node._view = () => ({ level, regions });
  return node;
}

/* ============================== mass export ============================== */
$("btnExportCsv").addEventListener("click", () => {
  const ids = analyses.map((a) => a.id); if (!ids.length) return;
  streamedDownload("/api/export_csv", { analysis_ids: ids }, "ImageSL_results.csv", "Exporting data (CSV)", 0);
});
$("btnDownloadZip").addEventListener("click", () => {
  const ids = analyses.map((a) => a.id); if (!ids.length) return;
  const n = ids.length;
  // carry each slide's current sensitivity AND its manual regions, so every
  // image in the ZIP is exactly what that card shows on screen
  const overrides = {};
  document.querySelectorAll("#resultsContainer .card").forEach((card, i) => {
    const a = analyses[i]; if (!a) return;
    const v = card._view ? card._view() : null;
    overrides[a.id] = v ? { level: v.level, regions: v.regions } : {};
  });
  streamedDownload("/api/export_zip", { analysis_ids: ids, images: ["comparison"], include_csv: true, overrides },
    "ImageSL_export.zip", `Packaging ${n} slide${n === 1 ? "" : "s"} (ZIP)`, n * 1.6 * 1048576);
});
$("btnNew").addEventListener("click", () => {
  analyses = []; skipped = [];
  $("resultsContainer").innerHTML = ""; $("skippedPanel").classList.add("hidden");
  clearState(); showView("uploadView"); window.scrollTo({ top: 0, behavior: "smooth" });
});

/* ============================== lightbox ============================== */
function showLightbox(src) { if (!src) return; $("lightboxImg").src = src; $("lightbox").classList.remove("hidden"); }
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));

/* ============================== restore on load ============================== */
(async function restore() {
  const stt = await loadState();
  if (!stt || (!(stt.analyses || []).length && !(stt.skipped || []).length)) return;
  analyses = (stt.analyses || []).filter((a) => a && a.data);
  skipped = stt.skipped || [];
  const container = $("resultsContainer"); container.innerHTML = "";
  analyses.forEach((a) => container.appendChild(createCard(a.data)));
  renderSummary([]);
  showView("resultsView");
})();
