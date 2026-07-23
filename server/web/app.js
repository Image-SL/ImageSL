"use strict";

/* ============================== helpers ============================== */
const $ = (id) => document.getElementById(id);
const RING_C = 2 * Math.PI * 52; // circumference of the progress ring

function headers(json) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  return h;
}

// vivid full-saturation color from a hue angle (the rainbow "all colors" slider)
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
  if (!m) return 345;
  const r = parseInt(m[1], 16) / 255, g = parseInt(m[2], 16) / 255, b = parseInt(m[3], 16) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d === 0) h = 0;
  else if (mx === r) h = 60 * ((((g - b) / d) % 6 + 6) % 6);
  else if (mx === g) h = 60 * ((b - r) / d + 2);
  else h = 60 * ((r - g) / d + 4);
  return Math.round(h);
}
function safeStem(name) {
  return (name || "image").replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._]+|[._]+$/g, "") || "image";
}
function showView(id) {
  ["uploadView", "loadingView", "resultsView"].forEach((v) => $(v).classList.toggle("hidden", v !== id));
}
async function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
async function fetchBlob(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.blob();
}

/* ============================== state ============================== */
let analyses = []; // [{ id, filename }]

/* ============================== upload ============================== */
const dropzone = $("dropzone");
const fileInput = $("file");

dropzone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", (e) => { const f = e.target.files; e.target.value = ""; if (f.length) runBatch(f); });
["dragover", "dragenter"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
["dragleave", "dragend"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault(); dropzone.classList.remove("dragover");
  const f = e.dataTransfer.files;
  if (f && f.length) runBatch(f);
});

/* ============================== batch analyze ============================== */
function setRing(fraction) {
  $("ringFill").style.strokeDashoffset = String(RING_C * (1 - fraction));
  $("ringPct").innerHTML = Math.round(fraction * 100) + "<small>%</small>";
}

async function analyzeOne(file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("filename", file.name);
  const res = await fetch("/api/analyze", { method: "POST", headers: headers(false), body: fd });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const data = await res.json();
  data.filename = data.filename || file.name;
  return data;
}

async function runBatch(fileList) {
  const files = Array.from(fileList);
  $("uploadError").classList.add("hidden");
  showView("loadingView");
  setRing(0);
  $("loaderSub").textContent = "Preparing…";

  const done = [];
  const errors = [];
  for (let i = 0; i < files.length; i++) {
    $("loaderSub").textContent = `Analyzing ${i + 1} of ${files.length} — ${files[i].name}`;
    try {
      const data = await analyzeOne(files[i]);
      done.push(data);
    } catch (e) {
      errors.push(`${files[i].name}: ${e.message}`);
    }
    setRing((i + 1) / files.length);
  }

  if (!done.length) {
    showView("uploadView");
    const box = $("uploadError");
    box.textContent = "Could not analyze: " + errors.join(" · ");
    box.classList.remove("hidden");
    return;
  }
  // brief beat so the ring reads 100% before the reveal
  await new Promise((r) => setTimeout(r, 350));
  renderResults(done, errors);
}

/* ============================== results ============================== */
function renderResults(list, errors) {
  analyses = list.map((d) => ({ id: d.analysis_id, filename: d.filename }));
  const container = $("resultsContainer");
  container.innerHTML = "";

  const n = list.length;
  const errNote = errors && errors.length ? ` <span class="sub">· ${errors.length} skipped</span>` : "";
  $("resultsCount").innerHTML = `${n} slide${n === 1 ? "" : "s"} analyzed${errNote}`;

  list.forEach((data, i) => {
    const card = createCard(data);
    card.style.animationDelay = (i * 90) + "ms";
    container.appendChild(card);
  });
  showView("resultsView");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function createCard(data) {
  const node = $("resultTemplate").content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);
  const analysisId = data.analysis_id;
  const filename = data.filename;
  const stem = safeStem(filename);
  let current = data;

  q(".fname").textContent = filename;

  // ---- render current state into the DOM ----
  function paint(d) {
    current = d;
    const img = d.images || {};
    if (img.original) { q(".imgOriginal").src = img.original; q(".imgOriginal2").src = img.original; }
    if (img.overlay) { q(".imgOverlayFront").src = img.overlay; q(".imgOverlay").src = img.overlay; }
    if (img.stainA) q(".imgStainA").src = img.stainA;
    if (img.stainB) q(".imgStainB").src = img.stainB;

    const r = d.result;
    if (r) {
      q(".mPercent").textContent = r.positive_percent.toFixed(2) + "%";
      q(".mPositive").textContent = r.positive_pixels.toLocaleString();
      q(".mTissue").textContent = r.tissue_pixels.toLocaleString();
      q(".mThreshold").textContent = r.threshold.toFixed(3);
      q(".badge").textContent = r.method;
    }
  }
  function paintControls(d) {
    const p = d.params, r = d.result;
    q(".cBg").value = p.background_threshold; q(".vBg").textContent = (+p.background_threshold).toFixed(2);
    q(".cScale").value = p.threshold_scale; q(".vScale").textContent = (+p.threshold_scale).toFixed(2);
    q(".cGain").value = p.target_gain; q(".vGain").textContent = (+p.target_gain).toFixed(1);
    q(".cTarget").value = String(r ? r.target_index : p.target_index);
    if (p.overlay_hex) { q(".cOverlay").value = p.overlay_hex; q(".cHue").value = hexToHue(p.overlay_hex); }
    q(".cBgColor").value = p.background_hex || "#ffffff";
  }
  paint(data); paintControls(data);

  // ---- comparison slider ----
  const vp = q(".cmp-viewport");
  let dragging = false;
  function setSplit(clientX) {
    const rect = vp.getBoundingClientRect();
    let pct = ((clientX - rect.left) / rect.width) * 100;
    pct = Math.max(0, Math.min(100, pct));
    vp.style.setProperty("--split", pct + "%");
  }
  vp.addEventListener("pointerdown", (e) => { dragging = true; vp.setPointerCapture(e.pointerId); setSplit(e.clientX); });
  vp.addEventListener("pointermove", (e) => { if (dragging) setSplit(e.clientX); });
  vp.addEventListener("pointerup", (e) => { dragging = false; try { vp.releasePointerCapture(e.pointerId); } catch (x) {} });
  vp.addEventListener("pointercancel", () => { dragging = false; });

  // ---- lightbox on variant thumbnails ----
  node.querySelectorAll(".vImg").forEach((im) => im.addEventListener("click", () => showLightbox(im.src)));

  // ---- recalculation / appearance ----
  let deb;
  function debounce(fn, ms) { clearTimeout(deb); deb = setTimeout(fn, ms); }
  async function post(url, body) {
    const res = await fetch(url, { method: "POST", headers: headers(true), body: JSON.stringify(Object.assign({ analysis_id: analysisId }, body)) });
    if (res.ok) paint(await res.json());
  }
  function recalc() {
    debounce(() => post("/api/recalculate", {
      background_threshold: parseFloat(q(".cBg").value),
      threshold_scale: parseFloat(q(".cScale").value),
      target_index: parseInt(q(".cTarget").value, 10),
    }), 240);
  }
  function appearance(extra) {
    debounce(() => post("/api/appearance", Object.assign({
      target_gain: parseFloat(q(".cGain").value),
      overlay_hex: q(".cOverlay").value,
    }, extra)), 150);
  }

  q(".cBg").addEventListener("input", () => { q(".vBg").textContent = (+q(".cBg").value).toFixed(2); recalc(); });
  q(".cScale").addEventListener("input", () => { q(".vScale").textContent = (+q(".cScale").value).toFixed(2); recalc(); });
  q(".cTarget").addEventListener("change", recalc);
  q(".cGain").addEventListener("input", () => { q(".vGain").textContent = (+q(".cGain").value).toFixed(1); appearance(); });
  q(".cOverlay").addEventListener("input", () => { q(".cHue").value = hexToHue(q(".cOverlay").value); appearance(); });
  q(".cHue").addEventListener("input", () => { q(".cOverlay").value = hueToHex(+q(".cHue").value); appearance(); });
  q(".cBgColor").addEventListener("input", () => appearance({ background_hex: q(".cBgColor").value }));
  q(".clearBg").addEventListener("click", (e) => { e.preventDefault(); q(".cBgColor").value = "#ffffff"; appearance({ background_hex: "" }); });

  // ---- downloads ----
  async function dlTif(type) {
    try {
      const blob = await fetchBlob(`/api/download_tif?analysis_id=${analysisId}&image_type=${type}`, { headers: headers(false) });
      downloadBlob(blob, `${stem}_${type}.tif`);
    } catch (e) { alert("Download failed: " + e.message); }
  }
  node.querySelectorAll(".dl-btn").forEach((b) => b.addEventListener("click", () => dlTif(b.dataset.type)));
  node.querySelectorAll(".vdl").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); dlTif(b.dataset.type); }));

  q(".export-one").addEventListener("click", async () => {
    try {
      const blob = await fetchBlob("/api/export_csv", { method: "POST", headers: headers(true), body: JSON.stringify({ analysis_ids: [analysisId] }) });
      downloadBlob(blob, `${stem}_data.csv`);
    } catch (e) { alert("Export failed: " + e.message); }
  });

  return node;
}

/* ============================== mass export ============================== */
$("btnExportCsv").addEventListener("click", async function () {
  const ids = analyses.map((a) => a.id);
  if (!ids.length) return;
  this.disabled = true;
  try {
    const blob = await fetchBlob("/api/export_csv", { method: "POST", headers: headers(true), body: JSON.stringify({ analysis_ids: ids }) });
    downloadBlob(blob, "ImageSL_results.csv");
  } catch (e) { alert("Export failed: " + e.message); }
  this.disabled = false;
});

$("btnDownloadZip").addEventListener("click", async function () {
  const ids = analyses.map((a) => a.id);
  if (!ids.length) return;
  const label = this.innerHTML; this.disabled = true; this.textContent = "Packaging…";
  try {
    const blob = await fetchBlob("/api/export_zip", { method: "POST", headers: headers(true), body: JSON.stringify({ analysis_ids: ids, images: ["comparison"], include_csv: true }) });
    downloadBlob(blob, "ImageSL_export.zip");
  } catch (e) { alert("Download failed: " + e.message); }
  this.disabled = false; this.innerHTML = label;
});

$("btnNew").addEventListener("click", () => { analyses = []; $("resultsContainer").innerHTML = ""; showView("uploadView"); window.scrollTo({ top: 0, behavior: "smooth" }); });

/* ============================== global overlay color ============================== */
function applyGlobalOverlay(hex) {
  document.querySelectorAll(".card").forEach((card) => {
    const c = card.querySelector(".cOverlay");
    const hue = card.querySelector(".cHue");
    if (!c) return;
    c.value = hex;
    if (hue) hue.value = hexToHue(hex);
    c.dispatchEvent(new Event("input", { bubbles: true })); // triggers that card's re-render + persist
  });
}
$("gHue").addEventListener("input", () => { const hex = hueToHex(+$("gHue").value); $("gOverlay").value = hex; applyGlobalOverlay(hex); });
$("gOverlay").addEventListener("input", () => { $("gHue").value = hexToHue($("gOverlay").value); applyGlobalOverlay($("gOverlay").value); });

/* ============================== lightbox ============================== */
function showLightbox(src) { if (!src) return; $("lightboxImg").src = src; $("lightbox").classList.remove("hidden"); }
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));
