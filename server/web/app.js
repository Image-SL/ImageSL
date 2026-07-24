"use strict";

/* ============================== helpers ============================== */
const $ = (id) => document.getElementById(id);
const RING_C = 2 * Math.PI * 52;
const OVERLAY_ALPHA = 0.5;

function headers(json) { const h = {}; if (json) h["Content-Type"] = "application/json"; return h; }

function hueToHex(h) {
  h = ((h % 360) + 360) % 360;
  const x = 1 - Math.abs(((h / 60) % 2) - 1);
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = 1; g = x; } else if (h < 120) { r = x; g = 1; }
  else if (h < 180) { g = 1; b = x; } else if (h < 240) { g = x; b = 1; }
  else if (h < 300) { r = x; b = 1; } else { r = 1; b = x; }
  const to = (v) => ("0" + Math.round(v * 255).toString(16)).slice(-2);
  return "#" + to(r) + to(g) + to(b);
}
function hexToHue(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
  if (!m) return 200;
  const r = parseInt(m[1], 16) / 255, g = parseInt(m[2], 16) / 255, b = parseInt(m[3], 16) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d === 0) h = 0;
  else if (mx === r) h = 60 * ((((g - b) / d) % 6 + 6) % 6);
  else if (mx === g) h = 60 * ((b - r) / d + 2);
  else h = 60 * ((r - g) / d + 4);
  return Math.round(h);
}
function hexRGB(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "#00e5ff");
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [0, 229, 255];
}
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

/* ============================== score-map overlay ==============================
   The server sends a grayscale "stainness" score (0..255) per pixel. The browser
   thresholds it live: positive = score ≥ threshold. This makes the threshold
   slider (and the overlay colour) update instantly with no server round-trip. */
function buildScoreData(scoreImg) {
  const W = scoreImg.naturalWidth, H = scoreImg.naturalHeight;
  const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  cx.drawImage(scoreImg, 0, 0);
  const raw = cx.getImageData(0, 0, W, H).data;
  const data = new Uint8Array(W * H);
  for (let i = 0, j = 0; i < data.length; i++, j += 4) data[i] = raw[j];
  const hist = new Int32Array(258);
  for (let i = 0; i < data.length; i++) hist[data[i]]++;
  const suffix = new Int32Array(258);      // suffix[t] = # pixels with value ≥ t
  for (let t = 256; t >= 0; t--) suffix[t] = suffix[t + 1] + (hist[t] || 0);
  const ov = document.createElement("canvas"); ov.width = W; ov.height = H;
  const ovCtx = ov.getContext("2d");
  return { W, H, data, suffix, ov, ovCtx, ovData: ovCtx.createImageData(W, H) };
}
function scoreTint(sc, hex, thrNorm, alpha) {
  const t = Math.max(1, Math.round(thrNorm * 255));
  const [r, g, b] = hexRGB(hex);
  const a = Math.round((alpha == null ? OVERLAY_ALPHA : alpha) * 255);
  const px = sc.ovData.data, data = sc.data;
  for (let i = 0, j = 0; i < data.length; i++, j += 4) {
    if (data[i] >= t) { px[j] = r; px[j + 1] = g; px[j + 2] = b; px[j + 3] = a; }
    else px[j + 3] = 0;
  }
  sc.ovCtx.putImageData(sc.ovData, 0, 0);
}
function drawScoreOverlay(canvas, origImg, sc, hex, thrNorm) {
  const W = sc.W, H = sc.H;
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;
  scoreTint(sc, hex, thrNorm, OVERLAY_ALPHA);
  const cx = canvas.getContext("2d");
  cx.clearRect(0, 0, W, H);
  cx.drawImage(origImg, 0, 0, W, H);
  cx.drawImage(sc.ov, 0, 0, W, H);
}
function scoreThumb(origImg, sc, hex, thrNorm, maxW) {
  let w = sc.W, h = sc.H;
  if (maxW && w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const cx = cv.getContext("2d");
  scoreTint(sc, hex, thrNorm, OVERLAY_ALPHA);
  cx.drawImage(origImg, 0, 0, w, h);
  cx.drawImage(sc.ov, 0, 0, w, h);
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

function currentOverlayHex(data) {
  const p = data.params || {}, r = data.result || {};
  return p.overlay_hex || r.suggested_overlay_hex || "#00e5ff";
}
function currentThr(data) {
  const p = data.params || {}, r = data.result || {};
  const st = (p.score_threshold != null) ? p.score_threshold : r.score_auto_threshold;
  return (st == null ? 0.2 : st);
}

function createCard(data) {
  const node = $("resultTemplate").content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);
  const analysisId = data.analysis_id;
  const filename = data.filename;
  const stem = safeStem(filename);
  let current = data;
  const entry = analyses.find((a) => a.id === analysisId);
  const st = { origImg: null, sc: null };
  let thrNorm = currentThr(data);
  const scoreFull = (data.result && data.result.score_full) || 1.8;

  q(".fname").textContent = filename;
  function persistData() { if (entry) entry.data = current; saveStateSoon(); }

  // ---- overlay redraw (colour + threshold, all client-side) ----
  let rafPending = false;
  function redraw() {
    if (!st.origImg || !st.sc) return;
    const hex = q(".cHue").style.getPropertyValue("--thumb") || currentOverlayHex(current);
    drawScoreOverlay(q(".cmpFront"), st.origImg, st.sc, hex, thrNorm);
    q(".imgOverlay").src = scoreThumb(st.origImg, st.sc, hex, thrNorm, 560);
  }
  function redrawSoon() {
    if (document.hidden) { redraw(); return; }   // rAF is paused when hidden
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => { rafPending = false; redraw(); });
  }

  function liveMetrics() {
    if (!st.sc) return;
    const t = Math.max(1, Math.round(thrNorm * 255));
    const pos = st.sc.suffix[t] || 0;
    const tissue = (current.result && current.result.tissue_pixels) || 0;
    const pct = tissue ? (pos / tissue * 100) : 0;
    q(".mPercent").textContent = pct.toFixed(2) + "%";
    q(".mPositive").textContent = pos.toLocaleString();
    const concThr = thrNorm * scoreFull;
    q(".mThreshold").textContent = concThr.toFixed(3);
    q(".thrVal").textContent = concThr.toFixed(3);
    [".mPercent", ".mPositive"].forEach((s) => { const b = q(s); b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash"); });
  }

  function setThr(norm, persist) {
    thrNorm = Math.max(0.001, Math.min(0.999, norm));
    q(".cThr").value = Math.round(thrNorm * 1000);
    redrawSoon(); liveMetrics();
    if (current.params) current.params.score_threshold = thrNorm;
    if (persist) { persistData(); postThreshold(thrNorm); }
  }
  function setOverlayHex(hex) { q(".cHue").style.setProperty("--thumb", hex); q(".cHue").value = hexToHue(hex); redrawSoon(); }

  function loadSources(origSrc, scoreSrc) {
    const o = new Image(), s = new Image();
    let n = 0; const done = () => { if (++n === 2) { st.origImg = o; st.sc = buildScoreData(s); redraw(); liveMetrics(); } };
    o.onload = done; s.onload = done; o.src = origSrc; s.src = scoreSrc;
  }

  function paint(d, keepThr) {
    current = d;
    const img = d.images || {};
    if (img.original) { q(".imgOriginal").src = img.original; q(".imgOriginal2").src = img.original; }
    if (img.stainA) q(".imgStainA").src = img.stainA;
    if (img.stainB) q(".imgStainB").src = img.stainB;
    if (img.original && img.score) loadSources(img.original, img.score);
    const r = d.result;
    if (r) {
      q(".badge").textContent = r.stain_label || r.method || "";
      q(".mTissue").textContent = (r.tissue_pixels || 0).toLocaleString();
      q(".labA").textContent = r.stain_a_label || "Stain A";
      q(".labB").textContent = r.stain_b_label || "Stain B";
      if (!keepThr) { thrNorm = currentThr(d); q(".cThr").value = Math.round(thrNorm * 1000); }
      q(".mPercent").textContent = (r.positive_percent || 0).toFixed(2) + "%";
      q(".mPositive").textContent = (r.positive_pixels || 0).toLocaleString();
      q(".mThreshold").textContent = (r.threshold || 0).toFixed(3);
      q(".thrVal").textContent = (r.threshold || 0).toFixed(3);
    }
  }
  paint(data);
  setOverlayHex(currentOverlayHex(data));
  const p0 = data.params || {}, r0 = data.result || {};
  q(".swA").style.background = p0.stainA_hex || r0.stain_a_hex || "#3b5bdb";
  q(".swB").style.background = p0.stainB_hex || r0.stain_b_hex || "#a1531f";

  // ---- comparison slider ----
  const vp = q(".cmp-viewport");
  let dragging = false;
  function setSplit(clientX) {
    const rect = vp.getBoundingClientRect();
    let pct = ((clientX - rect.left) / rect.width) * 100;
    vp.style.setProperty("--split", Math.max(0, Math.min(100, pct)) + "%");
  }
  vp.addEventListener("pointerdown", (e) => { dragging = true; vp.setPointerCapture(e.pointerId); setSplit(e.clientX); });
  vp.addEventListener("pointermove", (e) => { if (dragging) setSplit(e.clientX); });
  vp.addEventListener("pointerup", (e) => { dragging = false; try { vp.releasePointerCapture(e.pointerId); } catch (x) {} });
  vp.addEventListener("pointercancel", () => { dragging = false; });
  node.querySelectorAll(".vImg").forEach((im) => im.addEventListener("click", () => showLightbox(im.src)));

  // ---- server appearance calls (debounced) ----
  let deb, thrDeb;
  function post(body, cb) {
    clearTimeout(deb);
    deb = setTimeout(async () => {
      try { const res = await fetch("/api/appearance", { method: "POST", headers: headers(true), body: JSON.stringify(Object.assign({ analysis_id: analysisId }, body)) }); if (res.ok && cb) cb(await res.json()); } catch (e) {}
    }, 200);
  }
  function postThreshold(norm) {
    clearTimeout(thrDeb);
    thrDeb = setTimeout(async () => {
      try {
        const res = await fetch("/api/appearance", { method: "POST", headers: headers(true), body: JSON.stringify({ analysis_id: analysisId, score_threshold: norm }) });
        if (res.ok) { const d = await res.json(); if (d.result) { current.result = d.result; paint(d, true); persistData(); } }
      } catch (e) {}
    }, 350);
  }

  // ---- overlay colour ----
  function onOverlayHex(hex) { setOverlayHex(hex); if (current.params) current.params.overlay_hex = hex; persistData(); post({ overlay_hex: hex }); }
  q(".cHue").addEventListener("input", () => onOverlayHex(hueToHex(+q(".cHue").value)));
  q(".oc-auto").addEventListener("click", () => { const auto = (current.result && current.result.suggested_overlay_hex) || "#00e5ff"; if (current.params) current.params.overlay_hex = null; onOverlayHex(auto); });

  // ---- live detection threshold ----
  q(".cThr").addEventListener("input", () => setThr((+q(".cThr").value) / 1000, true));
  q(".thr-auto").addEventListener("click", () => { const auto = (current.result && current.result.score_auto_threshold) || 0.2; if (current.params) current.params.score_threshold = null; setThr(auto, false); postThreshold(""); persistData(); });

  // ---- Stain A / B recolour ----
  q(".swA").addEventListener("click", () => q(".pickA").click());
  q(".swB").addEventListener("click", () => q(".pickB").click());
  q(".pickA").value = p0.stainA_hex || r0.stain_a_hex || "#3b5bdb";
  q(".pickB").value = p0.stainB_hex || r0.stain_b_hex || "#a1531f";
  q(".pickA").addEventListener("input", () => { const hex = q(".pickA").value; q(".swA").style.background = hex; if (current.params) current.params.stainA_hex = hex; post({ stainA_hex: hex }, (d) => { paint(d, true); persistData(); }); });
  q(".pickB").addEventListener("input", () => { const hex = q(".pickB").value; q(".swB").style.background = hex; if (current.params) current.params.stainB_hex = hex; post({ stainB_hex: hex }, (d) => { paint(d, true); persistData(); }); });

  // ---- downloads (always carry the CURRENT on-screen colour + threshold) ----
  function dlTif(type) {
    const hex = q(".cHue").style.getPropertyValue("--thumb") || "";
    const qs = `analysis_id=${analysisId}&image_type=${type}&overlay_hex=${encodeURIComponent(hex)}&score_threshold=${thrNorm.toFixed(4)}`;
    streamedDownload(`/api/download_tif?${qs}`, null, `${stem}_${type}.tif`, `Exporting ${type}`, 2 * 1048576);
  }
  node.querySelectorAll(".dl-btn").forEach((b) => b.addEventListener("click", () => dlTif(b.dataset.type)));
  node.querySelectorAll(".vdl").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); dlTif(b.dataset.type); }));
  q(".export-one").addEventListener("click", () => streamedDownload("/api/export_csv", { analysis_ids: [analysisId] }, `${stem}_data.csv`, "Exporting CSV", 0));

  node._setOverlayHex = onOverlayHex;
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
  // carry each slide's current overlay colour + threshold so the ZIP matches screen
  const overrides = {};
  document.querySelectorAll("#resultsContainer .card").forEach((card, i) => {
    const a = analyses[i]; if (!a) return;
    const hex = card.querySelector(".cHue").style.getPropertyValue("--thumb") || "";
    const thr = (parseInt(card.querySelector(".cThr").value, 10) || 200) / 1000;
    overrides[a.id] = { overlay_hex: hex, score_threshold: thr.toFixed(4) };
  });
  streamedDownload("/api/export_zip", { analysis_ids: ids, images: ["comparison"], include_csv: true, overrides },
    "ImageSL_export.zip", `Packaging ${n} slide${n === 1 ? "" : "s"} (ZIP)`, n * 1.6 * 1048576);
});
$("btnNew").addEventListener("click", () => {
  analyses = []; skipped = [];
  $("resultsContainer").innerHTML = ""; $("skippedPanel").classList.add("hidden");
  clearState(); showView("uploadView"); window.scrollTo({ top: 0, behavior: "smooth" });
});

/* ============================== global overlay color ============================== */
function applyGlobalOverlay(hex) { document.querySelectorAll("#resultsContainer .card").forEach((card) => { if (card._setOverlayHex) card._setOverlayHex(hex); }); }
$("gHue").addEventListener("input", () => { const hex = hueToHex(+$("gHue").value); $("gHue").style.setProperty("--thumb", hex); applyGlobalOverlay(hex); });
$("gHue").style.setProperty("--thumb", hueToHex(200));

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
