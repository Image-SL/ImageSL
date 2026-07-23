"use strict";

/* ============================== helpers ============================== */
const $ = (id) => document.getElementById(id);
const RING_C = 2 * Math.PI * 52;
const OVERLAY_ALPHA = 0.5;

function headers(json) { const h = {}; if (json) h["Content-Type"] = "application/json"; return h; }

// vivid full-saturation color from a hue angle (the rainbow slider)
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

// Composite the detection overlay in-browser: original + positive mask tinted.
let _maskScratch = null;
function _tintMask(maskImg, hex, w, h) {
  if (!_maskScratch) _maskScratch = document.createElement("canvas");
  _maskScratch.width = w; _maskScratch.height = h;
  const mx = _maskScratch.getContext("2d");
  mx.clearRect(0, 0, w, h);
  mx.globalCompositeOperation = "source-over";
  mx.drawImage(maskImg, 0, 0, w, h);
  mx.globalCompositeOperation = "source-in";
  mx.fillStyle = hex; mx.fillRect(0, 0, w, h);
  return _maskScratch;
}
function drawOverlay(canvas, origImg, maskImg, hex, alpha) {
  const w = origImg.naturalWidth || origImg.width, h = origImg.naturalHeight || origImg.height;
  if (!w || !h) return;
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const cx = canvas.getContext("2d");
  cx.clearRect(0, 0, w, h);
  cx.drawImage(origImg, 0, 0, w, h);
  cx.globalAlpha = alpha == null ? OVERLAY_ALPHA : alpha;
  cx.drawImage(_tintMask(maskImg, hex, w, h), 0, 0);
  cx.globalAlpha = 1;
}
function compositeOverlay(origImg, maskImg, hex, alpha, maxW) {
  let w = origImg.naturalWidth || origImg.width, h = origImg.naturalHeight || origImg.height;
  if (!w || !h) return "";
  if (maxW && w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const cx = cv.getContext("2d");
  cx.drawImage(origImg, 0, 0, w, h);
  cx.globalAlpha = alpha == null ? OVERLAY_ALPHA : alpha;
  cx.drawImage(_tintMask(maskImg, hex, w, h), 0, 0);
  cx.globalAlpha = 1;
  return cv.toDataURL("image/jpeg", 0.85);
}
function safeStem(name) {
  return (name || "image").replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._]+|[._]+$/g, "") || "image";
}
function showView(id) { ["uploadView", "loadingView", "resultsView"].forEach((v) => $(v).classList.toggle("hidden", v !== id)); }
async function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
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

/* ============================== IndexedDB persistence ============================== */
// The whole analyzed batch (metrics + self-contained data-URI images) is stored
// so a refresh / navigation restores it instantly — even if the server cache has
// since expired.
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
  } catch (e) { /* storage full / private mode — non-fatal */ }
}
let _saveDeb;
function saveStateSoon() { clearTimeout(_saveDeb); _saveDeb = setTimeout(saveState, 400); }
async function loadState() {
  try {
    const db = await idb();
    const tx = db.transaction(STORE, "readonly");
    return await new Promise((res) => { const q = tx.objectStore(STORE).get(KEY); q.onsuccess = () => res(q.result); q.onerror = () => res(null); });
  } catch (e) { return null; }
}
async function clearState() { try { const db = await idb(); const tx = db.transaction(STORE, "readwrite"); tx.objectStore(STORE).delete(KEY); } catch (e) {} }

/* ============================== state ============================== */
let analyses = []; // [{ id, filename, data }]
let skipped = [];  // [{ filename, reason }]

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
  showView("loadingView");
  setRing(0);
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
        container.appendChild(card);
        added++;
      }
    } catch (e) {
      errors.push(`${files[i].name}: ${e.message}`);
    }
    setRing((i + 1) / files.length);
  }

  if (!analyses.length && !skipped.length) {
    showView("uploadView");
    const box = $("uploadError");
    box.textContent = "Could not analyze: " + errors.join(" · ");
    box.classList.remove("hidden");
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
  } else { panel.classList.add("hidden"); }
}

function currentOverlayHex(data) {
  const p = data.params || {}, r = data.result || {};
  return p.overlay_hex || r.suggested_overlay_hex || "#00e5ff";
}

function createCard(data) {
  const node = $("resultTemplate").content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);
  const analysisId = data.analysis_id;
  const filename = data.filename;
  const stem = safeStem(filename);
  let current = data;
  const ov = { origImg: null, maskImg: null };
  const entry = analyses.find((a) => a.id === analysisId);

  q(".fname").textContent = filename;

  function persistData() { if (entry) { entry.data = current; } saveStateSoon(); }

  function recolorOverlay() {
    if (!ov.origImg || !ov.maskImg) return;
    const color = q(".cHue").style.getPropertyValue("--thumb") || currentOverlayHex(current);
    drawOverlay(q(".cmpFront"), ov.origImg, ov.maskImg, color, OVERLAY_ALPHA);
    q(".imgOverlay").src = compositeOverlay(ov.origImg, ov.maskImg, color, OVERLAY_ALPHA, 560);
  }
  function loadOverlaySources(origSrc, maskSrc) {
    const o = new Image(), m = new Image();
    let n = 0; const done = () => { if (++n === 2) { ov.origImg = o; ov.maskImg = m; recolorOverlay(); } };
    o.onload = done; m.onload = done; o.src = origSrc; m.src = maskSrc;
  }

  function setOverlayHex(hex) {
    q(".cHue").style.setProperty("--thumb", hex);
    q(".cHue").value = hexToHue(hex);
    recolorOverlay();
  }

  function paint(d) {
    current = d;
    const img = d.images || {};
    if (img.original) { q(".imgOriginal").src = img.original; q(".imgOriginal2").src = img.original; }
    if (img.stainA) q(".imgStainA").src = img.stainA;
    if (img.stainB) q(".imgStainB").src = img.stainB;
    if (img.original && img.mask) loadOverlaySources(img.original, img.mask);

    const r = d.result;
    if (r) {
      q(".mPercent").textContent = (r.positive_percent || 0).toFixed(2) + "%";
      q(".mPositive").textContent = (r.positive_pixels || 0).toLocaleString();
      q(".mTissue").textContent = (r.tissue_pixels || 0).toLocaleString();
      q(".mThreshold").textContent = (r.threshold || 0).toFixed(3);
      q(".badge").textContent = r.stain_label || r.method || "";
      q(".labA").textContent = r.stain_a_label || "Stain A";
      q(".labB").textContent = r.stain_b_label || "Stain B";
    }
  }
  paint(data);

  // init colors
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
  let deb;
  function post(body, cb) {
    clearTimeout(deb);
    deb = setTimeout(async () => {
      try {
        const res = await fetch("/api/appearance", { method: "POST", headers: headers(true), body: JSON.stringify(Object.assign({ analysis_id: analysisId }, body)) });
        if (res.ok) { const d = await res.json(); if (cb) cb(d); }
      } catch (e) {}
    }, 200);
  }

  // ---- overlay color: instant client recolor + persist to server for exports ----
  function onOverlayHex(hex) {
    setOverlayHex(hex);
    if (current.params) current.params.overlay_hex = hex;
    persistData();
    post({ overlay_hex: hex });
  }
  q(".cHue").addEventListener("input", () => onOverlayHex(hueToHex(+q(".cHue").value)));
  q(".oc-auto").addEventListener("click", () => {
    const auto = (current.result && current.result.suggested_overlay_hex) || "#00e5ff";
    if (current.params) current.params.overlay_hex = null;
    onOverlayHex(auto);
  });

  // ---- Stain A / B recolor swatches → hidden color pickers → server re-render ----
  q(".swA").addEventListener("click", () => q(".pickA").click());
  q(".swB").addEventListener("click", () => q(".pickB").click());
  q(".pickA").value = p0.stainA_hex || r0.stain_a_hex || "#3b5bdb";
  q(".pickB").value = p0.stainB_hex || r0.stain_b_hex || "#a1531f";
  q(".pickA").addEventListener("input", () => {
    const hex = q(".pickA").value; q(".swA").style.background = hex;
    if (current.params) current.params.stainA_hex = hex;
    post({ stainA_hex: hex }, (d) => { paint(d); persistData(); });
  });
  q(".pickB").addEventListener("input", () => {
    const hex = q(".pickB").value; q(".swB").style.background = hex;
    if (current.params) current.params.stainB_hex = hex;
    post({ stainB_hex: hex }, (d) => { paint(d); persistData(); });
  });

  // ---- downloads ----
  function dlTif(type) {
    streamedDownload(`/api/download_tif?analysis_id=${analysisId}&image_type=${type}`, null, `${stem}_${type}.tif`, `Exporting ${type}`, 2 * 1048576);
  }
  node.querySelectorAll(".dl-btn").forEach((b) => b.addEventListener("click", () => dlTif(b.dataset.type)));
  node.querySelectorAll(".vdl").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); dlTif(b.dataset.type); }));
  q(".export-one").addEventListener("click", () => streamedDownload("/api/export_csv", { analysis_ids: [analysisId] }, `${stem}_data.csv`, "Exporting CSV", 0));

  // expose a setter so the global slider can drive this card
  node._setOverlayHex = onOverlayHex;
  return node;
}

/* ============================== mass export ============================== */
$("btnExportCsv").addEventListener("click", () => {
  const ids = analyses.map((a) => a.id);
  if (!ids.length) return;
  streamedDownload("/api/export_csv", { analysis_ids: ids }, "ImageSL_results.csv", "Exporting data (CSV)", 0);
});
$("btnDownloadZip").addEventListener("click", () => {
  const ids = analyses.map((a) => a.id);
  if (!ids.length) return;
  const n = ids.length;
  streamedDownload("/api/export_zip", { analysis_ids: ids, images: ["comparison"], include_csv: true },
    "ImageSL_export.zip", `Packaging ${n} slide${n === 1 ? "" : "s"} (ZIP)`, n * 1.6 * 1048576);
});
$("btnNew").addEventListener("click", () => {
  analyses = []; skipped = [];
  $("resultsContainer").innerHTML = "";
  $("skippedPanel").classList.add("hidden");
  clearState();
  showView("uploadView");
  window.scrollTo({ top: 0, behavior: "smooth" });
});

/* ============================== global overlay color ============================== */
function applyGlobalOverlay(hex) {
  document.querySelectorAll("#resultsContainer .card").forEach((card) => { if (card._setOverlayHex) card._setOverlayHex(hex); });
}
$("gHue").addEventListener("input", () => { const hex = hueToHex(+$("gHue").value); $("gHue").style.setProperty("--thumb", hex); applyGlobalOverlay(hex); });
$("gHue").style.setProperty("--thumb", hueToHex(200));

/* ============================== lightbox ============================== */
function showLightbox(src) { if (!src) return; $("lightboxImg").src = src; $("lightbox").classList.remove("hidden"); }
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));

/* ============================== restore on load ============================== */
(async function restore() {
  const st = await loadState();
  if (!st || (!(st.analyses || []).length && !(st.skipped || []).length)) return;
  analyses = (st.analyses || []).filter((a) => a && a.data);
  skipped = st.skipped || [];
  const container = $("resultsContainer");
  container.innerHTML = "";
  analyses.forEach((a) => container.appendChild(createCard(a.data)));
  renderSummary([]);
  showView("resultsView");
})();
