"use strict";

const $ = (id) => document.getElementById(id);
const RING_C = 2 * Math.PI * 52;
const OVERLAY_ALPHA = 0.5;

const OVERLAY_RGB = [57, 255, 20];

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

    if (!received) throw new Error("the server returned an empty file");
    await downloadBlob(new Blob(chunks), filename);
    if (missing) t.fail(`Saved ${filename} — but ${missing} slide${missing === 1 ? "" : "s"} had expired and ${missing === 1 ? "is" : "are"} not included`);
    else t.done("Saved " + filename);
  } catch (e) { t.fail("Export failed: " + e.message); }
}

const NEVER = 255;
const CANDIDATE = 254;

function buildLevelData(levelImg) {
  const W = levelImg.naturalWidth, H = levelImg.naturalHeight;
  const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  cx.drawImage(levelImg, 0, 0);
  const raw = cx.getImageData(0, 0, W, H).data;
  const n = W * H;
  const data = new Uint8Array(n);
  const tissue = new Uint8Array(n);

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
    include: null,
    exclude: null,
    mask: new Uint8Array(n),
    count: 0,
    measured: tissueCount,
    added: 0, removed: 0,
    ov, ovCtx, ovData: ovCtx.createImageData(W, H),
    iso, isoCtx, isoData: isoCtx.createImageData(W, H),
    tmp, tmpCtx,
  };
}

function rasterRegion(ld, region) {
  const { W, H } = ld;

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

function regionMode(m) {
  const s = String(m || "").toLowerCase();
  if (s === "include" || s === "focus" || s === "boost") return "include";
  if (s === "exclude" || s === "ignore") return "exclude";
  return null;
}

function applyRegions(ld, regions) {
  const n = ld.W * ld.H;
  ld.include = null; ld.exclude = null;
  if (!regions || !regions.length) return;
  for (const r of regions) {
    const mode = regionMode(r.mode);
    if (!mode) continue;
    const px = rasterRegion(ld, r);
    if (!px) continue;
    if (!ld[mode]) ld[mode] = new Uint8Array(n);
    const dst = ld[mode];
    for (let i = 0; i < n; i++) if (px[i]) dst[i] = 1;
  }
}

function countObjects(ld) {
  const { W, H, mask } = ld;
  const labels = new Int32Array(W * H);
  const parent = [0];
  const find = (x) => { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; };
  const union = (a, b) => { a = find(a); b = find(b); if (a !== b) parent[b > a ? b : a] = b > a ? a : b; };
  let next = 1;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const i = y * W + x;
      if (!mask[i]) continue;

      let lab = 0;
      const nb = [
        y > 0 && x > 0 ? labels[i - W - 1] : 0,
        y > 0 ? labels[i - W] : 0,
        y > 0 && x < W - 1 ? labels[i - W + 1] : 0,
        x > 0 ? labels[i - 1] : 0,
      ];
      for (const n of nb) if (n) lab = lab ? Math.min(lab, find(n)) : find(n);
      if (!lab) { lab = next++; parent[lab] = lab; }
      labels[i] = lab;
      for (const n of nb) if (n) union(lab, n);
    }
  }
  const roots = new Set();
  for (let i = 0; i < labels.length; i++) if (labels[i]) roots.add(find(labels[i]));
  return roots.size;
}

function computeMask(ld, level, maxLevel) {
  const { data, tissue, include, exclude, mask } = ld;
  const lim = level < 0 ? 0 : (level > maxLevel ? maxLevel : level);
  let count = 0, added = 0, removed = 0;
  for (let i = 0; i < data.length; i++) {
    if (!tissue[i]) { mask[i] = 0; continue; }
    const lv = data[i];
    const auto = lv <= lim;
    let on = auto;
    if (!on && include && include[i]) on = lv <= CANDIDATE;
    if (on && exclude && exclude[i]) on = false;
    mask[i] = on ? 1 : 0;
    if (on) { count++; if (!auto) added++; }
    else if (auto) removed++;
  }
  ld.count = count;
  ld.measured = ld.tissueCount;
  ld.added = added;
  ld.removed = removed;
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

function isolateThumb(origImg, ld, maxW) {
  const W = ld.W, H = ld.H;
  const px = ld.isoData.data, mask = ld.mask;
  for (let i = 0, j = 0; i < mask.length; i++, j += 4) {
    if (mask[i]) { px[j] = px[j + 1] = px[j + 2] = 255; px[j + 3] = 255; }
    else px[j + 3] = 0;
  }
  ld.isoCtx.putImageData(ld.isoData, 0, 0);

  const tc = ld.tmpCtx;
  tc.globalCompositeOperation = "source-over";
  tc.clearRect(0, 0, W, H);
  tc.drawImage(origImg, 0, 0, W, H);
  tc.globalCompositeOperation = "destination-in";
  tc.drawImage(ld.iso, 0, 0);
  tc.globalCompositeOperation = "source-over";

  let w = W, h = H;
  if (maxW && w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const cx = cv.getContext("2d");
  cx.fillStyle = "#ffffff"; cx.fillRect(0, 0, w, h);
  cx.drawImage(ld.tmp, 0, 0, w, h);
  return cv.toDataURL("image/jpeg", 0.85);
}

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

let analyses = [];
let skipped = [];
let failures = [];
let mode = "auto";
let selectedStain = null;
let stainList = null;

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

const dropzone = $("dropzone");
const fileInput = $("file");

let staged = [];
const MAX_LISTED = 12;

const fileKey = (f) => `${f.name} ${f.size} ${f.lastModified}`;

function humanSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
}

function renderSelection() {
  const panel = $("selection"), list = $("selList"), more = $("selMore");
  if (!panel) return;
  panel.classList.toggle("hidden", staged.length === 0);
  if (!staged.length) { list.innerHTML = ""; more.classList.add("hidden"); return; }

  $("selCount").textContent =
    staged.length === 1 ? "1 file selected" : `${staged.length} files selected`;

  list.innerHTML = "";
  staged.slice(0, MAX_LISTED).forEach((f, i) => {
    const li = document.createElement("li");
    li.className = "sel-item";
    const name = document.createElement("span");
    name.className = "sel-name";
    name.textContent = f.name;
    name.title = f.name;
    const size = document.createElement("span");
    size.className = "sel-size";
    size.textContent = humanSize(f.size);
    const drop = document.createElement("button");
    drop.type = "button";
    drop.className = "sel-drop";
    drop.textContent = "×";
    drop.title = "Remove";
    drop.setAttribute("aria-label", `Remove ${f.name}`);
    drop.addEventListener("click", () => { staged.splice(i, 1); renderSelection(); });
    li.append(name, size, drop);
    list.appendChild(li);
  });

  const hidden = staged.length - MAX_LISTED;
  more.textContent = hidden > 0
    ? `and ${hidden} more file${hidden === 1 ? "" : "s"}` : "";
  more.classList.toggle("hidden", hidden <= 0);
}

function stage(fileList) {
  const seen = new Set(staged.map(fileKey));
  let added = 0, dupes = 0;
  Array.from(fileList).forEach((f) => {
    const k = fileKey(f);
    if (seen.has(k)) { dupes++; return; }
    seen.add(k);
    staged.push(f);
    added++;
  });
  renderSelection();
  const err = $("uploadError");
  if (err) {
    if (dupes && !added) {
      err.textContent = dupes === 1
        ? "That file is already selected."
        : "Those files are already selected.";
      err.classList.remove("hidden");
    } else {
      err.classList.add("hidden");
    }
  }
  if (added) $("btnAnalyze").focus({ preventScroll: true });
}

function clearSelection() {
  staged = [];
  renderSelection();
  const err = $("uploadError");
  if (err) err.classList.add("hidden");
}

dropzone.addEventListener("click", () => { fileInput.value = ""; fileInput.click(); });
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.value = ""; fileInput.click(); }
});

fileInput.addEventListener("change", (e) => {
  const files = Array.from(e.target.files);
  if (files.length) stage(files);
});
["dragover", "dragenter"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
["dragleave", "dragend"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
dropzone.addEventListener("drop", (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); const f = e.dataTransfer.files; if (f && f.length) stage(f); });

$("selClear").addEventListener("click", clearSelection);
$("btnSelAdd").addEventListener("click", () => { fileInput.value = ""; fileInput.click(); });
$("btnAnalyze").addEventListener("click", () => {
  if (!staged.length) return;
  const batch = staged;
  clearSelection();
  runBatch(batch, false);
});

$("fileAdd").addEventListener("change", (e) => {
  const files = Array.from(e.target.files);
  if (files.length) runBatch(files, true);
});
$("btnAddMore").addEventListener("click", () => { $("fileAdd").value = ""; $("fileAdd").click(); });

function setRing(fraction) {
  $("ringFill").style.strokeDashoffset = String(RING_C * (1 - fraction));
  $("ringPct").innerHTML = Math.round(fraction * 100) + "<small>%</small>";
}

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

  let msg = "";
  try { msg = (await res.json()).detail || ""; } catch (e) {}
  if (!msg) msg = res.statusText;
  if (!msg) {
    msg = (res.status >= 500 || res.status === 0)
      ? `the server could not complete this slide (HTTP ${res.status}) — it may be out of memory or restarting`
      : `HTTP ${res.status}`;
  }
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
      if (e && e.permanent) throw e;
    }
    if (attempt < RETRY_ATTEMPTS) {
      if (onRetry) onRetry(attempt);
      await sleep(700 * attempt);
    }
  }
  throw last || new Error("Analysis failed");
}

const UPLOAD_CONCURRENCY = 3;

async function runBatch(fileList, append) {
  const files = Array.from(fileList);
  $("uploadError").classList.add("hidden");
  if (!append) { analyses = []; skipped = []; failures = []; $("resultsContainer").innerHTML = ""; }
  showView("loadingView"); setRing(0);
  $("loaderSub").textContent = "Preparing…";
  const container = $("resultsContainer");
  const errors = [];
  const results = new Array(files.length);
  let done = 0, next = 0, added = 0;

  let emitAt = 0;
  function emitReady() {
    while (emitAt < files.length && results[emitAt] !== undefined) {
      const slot = results[emitAt];
      if (slot && slot.data) {
        const data = slot.data, r = data.result || {};
        if (r.valid === false) {
          skipped.push({ filename: data.filename, reason: r.skip_reason || "Not recognized as a stained slide." });
        } else {
          analyses.push({ id: data.analysis_id, filename: data.filename, data });
          const card = createCard(data);
          card.style.animationDelay = Math.min(added * 60, 400) + "ms";
          container.appendChild(card); added++;
        }
      } else if (slot && slot.error) {
        errors.push(slot.error);
      }
      results[emitAt] = null;
      emitAt++;
    }
  }

  async function worker() {
    for (;;) {
      const i = next++;
      if (i >= files.length) return;
      try {
        results[i] = { data: await analyzeOne(files[i]) };
      } catch (e) {
        const why = (e && e.message) || "";
        results[i] = { error: {
          filename: files[i].name,

          reason: why || "the connection to the server was lost while analyzing this slide",
        } };
      }
      done++;
      setRing(done / files.length);
      $("loaderSub").textContent = `Analyzing ${Math.min(done + 1, files.length)} of ${files.length}`;
      emitReady();
    }
  }
  await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, files.length) }, worker));
  emitReady();
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

function renderSummary() {
  const n = analyses.length;
  const bits = [];
  if (skipped.length) bits.push(`${skipped.length} skipped`);
  if (failures.length) bits.push(`${failures.length} failed`);
  const note = bits.length ? ` <span class="sub">· ${bits.join(" · ")}</span>` : "";
  $("resultsCount").innerHTML = `${n} slide${n === 1 ? "" : "s"} analyzed${note}`;

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
  let analysisId = data.analysis_id;
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

  let rafPending = false;
  let maskLevel = -1, maskStamp = -1, regionStamp = 0;

  function recount() {
    if (!st.ld) return;
    if (maskLevel === level && maskStamp === regionStamp) return;
    computeMask(st.ld, level, maxLevel);
    maskLevel = level; maskStamp = regionStamp;
  }
  function invalidateMask() { maskLevel = -1; maskStamp = -1; }

  function redraw() {
    if (!st.origImg || !st.ld) return;
    recount();
    drawLevelOverlay(q(".cmpFront"), st.origImg, st.ld);
    q(".imgOverlay").src = overlayThumb(st.origImg, st.ld, 560);
    q(".imgStainOnly").src = isolateThumb(st.origImg, st.ld, 560);
    drawRegionOutlines();

    q(".mObjects").textContent = countObjects(st.ld).toLocaleString();
  }
  function redrawSoon() {
    if (document.hidden) { redraw(); return; }
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => { rafPending = false; redraw(); });
  }

  function liveMetrics() {
    if (!st.ld) return;
    recount();
    const pos = st.ld.count;
    const measured = st.ld.measured;
    const framePx = st.ld.W * st.ld.H;

    q(".mPercentImage").textContent = framePx ? (pos / framePx * 100).toFixed(2) + "%" : "–";
    q(".mPositive").textContent = pos.toLocaleString();
    q(".mTissue").textContent = measured.toLocaleString();
    q(".mImagePx").textContent = framePx.toLocaleString();

    const scope = q(".mScope");
    if (scope) {
      const bits = [];
      if (st.ld.added) bits.push(`+${st.ld.added.toLocaleString()} px recovered by Include`);
      if (st.ld.removed) bits.push(`−${st.ld.removed.toLocaleString()} px removed by Exclude`);
      if (bits.length) {
        scope.textContent = "Corrected by hand: " + bits.join(", ")
          + ` · still measured over the whole image (${framePx.toLocaleString()} px)`;
        scope.classList.remove("hidden");
      } else {
        scope.classList.add("hidden");
      }
    }
    [".mPercentImage", ".mPositive", ".mTissue"].forEach((s) => { const b = q(s); if (b) { b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash"); } });
  }

  function levelLabel() {
    const r = current.result || {};
    const auto = (r.auto_level == null ? 100 : r.auto_level);
    const n = (r.level_count == null ? 201 : r.level_count);
    if (level === auto) { q(".thrVal").textContent = "Auto"; return; }

    const hi = (r.ladder_hi == null ? LADDER_STRICT : r.ladder_hi);
    const lo = (r.ladder_lo == null ? LADDER_LOOSE : r.ladder_lo);
    const mult = level < auto
      ? Math.pow(hi, (auto - level) / Math.max(auto, 1))
      : Math.pow(lo, (level - auto) / Math.max(n - 1 - auto, 1));
    q(".thrVal").textContent = "×" + mult.toFixed(2);
  }

  function setLevel(v, persist) {
    level = Math.max(0, Math.min(maxLevel, v | 0));
    q(".cThr").value = String(level);
    levelLabel();
    liveMetrics(); redrawSoon();
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
      regionStamp++; invalidateMask();
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
      q(".mPositive").textContent = (r.positive_pixels || 0).toLocaleString();
      const framePx = (r.width || 0) * (r.height || 0);
      q(".mImagePx").textContent = framePx.toLocaleString();
      q(".mPercentImage").textContent = framePx
        ? ((r.positive_pixels || 0) / framePx * 100).toFixed(2) + "%" : "–";
      levelLabel();
      const note = q(".card-note");
      const text = (r.notes || []).join(" ");
      note.textContent = text;
      note.classList.toggle("hidden", !text);
    }
  }
  paint(data);

  const vp = q(".cmp-viewport");
  let dragging = false;
  function setSplit(clientX) {
    const rect = vp.getBoundingClientRect();
    let pct = ((clientX - rect.left) / rect.width) * 100;
    vp.style.setProperty("--split", Math.max(0, Math.min(100, pct)) + "%");
  }
  vp.addEventListener("pointerdown", (e) => {
    if (tool !== "off") return;
    dragging = true; vp.setPointerCapture(e.pointerId); setSplit(e.clientX);
  });
  vp.addEventListener("pointermove", (e) => { if (dragging) setSplit(e.clientX); });
  vp.addEventListener("pointerup", (e) => { dragging = false; try { vp.releasePointerCapture(e.pointerId); } catch (x) {} });
  vp.addEventListener("pointercancel", () => { dragging = false; });
  node.querySelectorAll(".vImg").forEach((im) => im.addEventListener("click", () => showLightbox(im.src)));

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
        } else if (res.status === 404 || res.status === 410) {

          keepAlive();
        }
      } catch (e) {}
    }, 350);
  }

  q(".cThr").addEventListener("input", () => setLevel(+q(".cThr").value, true));
  q(".thr-auto").addEventListener("click", () => {
    const auto = (current.result && current.result.auto_level);
    if (current.params) current.params.level = null;
    setLevel(auto == null ? 100 : auto, false);
    persistData(); postView();
  });

  let tool = "off";
  let shape = "rect";
  let drawing = null;
  const draw = q(".cmpDraw");

  function regionColour(mode) {
    return regionMode(mode) === "include" ? "#38bdf8" : "#f43f5e";
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
      cx.setLineDash(regionMode(r.mode) === "exclude" ? [6, 4] : []);
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
    q(".rcount").textContent = n === 0 ? "No corrections"
      : (n === 1 ? "1 correction" : n + " corrections");
  }

  function commitRegions() {
    if (!st.ld) return;
    applyRegions(st.ld, regions);
    regionStamp++;
    regionCountLabel();
    liveMetrics(); redrawSoon();
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

  async function dlTif(type) {
    await keepAlive();
    streamedDownload("/api/download_tif",
      { analysis_id: analysisId, image_type: type, level, regions },
      `${stem}_${type}.tif`, `Exporting ${type}`, 2 * 1048576);
  }
  node.querySelectorAll(".dl-btn").forEach((b) => b.addEventListener("click", () => dlTif(b.dataset.type)));
  node.querySelectorAll(".vdl").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); dlTif(b.dataset.type); }));
  q(".export-one").addEventListener("click", async () => {
    await keepAlive();
    streamedDownload("/api/export_csv",
      { analysis_ids: [analysisId], overrides: { [analysisId]: { level, regions } } },
      `${stem}_data.csv`, "Exporting CSV", 0);
  });

  node.dataset.analysisId = analysisId;
  node._view = () => ({ level, regions });
  node._filename = filename;

  node._rebind = (id, d) => {
    analysisId = id;
    node.dataset.analysisId = id;
    node.classList.remove("expired");
    if (entry) { entry.id = id; }
    if (d) { current = d; if (current.params) { current.params.level = level; current.params.regions = regions; } }
    const note = q(".card-note");
    const text = ((d && d.result && d.result.notes) || []).join(" ");
    note.textContent = text;
    note.classList.toggle("hidden", !text);
    persistData();
  };
  return node;
}

function currentOverrides() {

  const overrides = {};
  document.querySelectorAll("#resultsContainer .card").forEach((card) => {
    const id = card.dataset.analysisId;
    if (!id) return;
    const v = card._view ? card._view() : null;
    overrides[id] = v ? { level: v.level, regions: v.regions } : {};
  });
  return overrides;
}

$("btnExportCsv").addEventListener("click", async () => {
  if (!analyses.length) return;
  await keepAlive();
  const ids = analyses.map((a) => a.id); if (!ids.length) return;
  streamedDownload("/api/export_csv", { analysis_ids: ids, overrides: currentOverrides() },
    "ImageSL_results.csv", "Exporting data (CSV)", 0);
});
$("btnDownloadZip").addEventListener("click", async () => {
  if (!analyses.length) return;
  await keepAlive();
  const ids = analyses.map((a) => a.id); if (!ids.length) return;
  const n = ids.length;
  streamedDownload("/api/export_zip",
    { analysis_ids: ids, images: ["comparison"], include_csv: true, overrides: currentOverrides() },
    "ImageSL_export.zip", `Packaging ${n} slide${n === 1 ? "" : "s"} (ZIP)`, n * 1.6 * 1048576);
});
$("btnNew").addEventListener("click", () => {
  analyses = []; skipped = []; failures = [];
  $("resultsContainer").innerHTML = ""; $("skippedPanel").classList.add("hidden");
  clearState(); showView("uploadView"); window.scrollTo({ top: 0, behavior: "smooth" });
});

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
    if (expired.size) await healExpired(expired);
  } catch (e) {  }
}

let healing = null;
function healExpired(expired) {

  if (healing) return healing;
  healing = _heal(expired).finally(() => { healing = null; });
  return healing;
}
async function _heal(expired) {
  const cards = Array.from(document.querySelectorAll("#resultsContainer .card"))
    .filter((c) => expired.has(c.dataset.analysisId));
  let t = null, done = 0, failed = 0;
  try {
    for (const card of cards) {
      const oldId = card.dataset.analysisId;
      const entry = analyses.find((a) => a.id === oldId);
      const src = entry && entry.data && entry.data.images && entry.data.images.original;
      if (!src) { failed++; markLost(card); continue; }
      if (!t) t = makeToast(`Restoring ${cards.length} slide${cards.length === 1 ? "" : "s"}…`);
      try {
        const view = card._view ? card._view() : {};
        const res = await fetch("/api/rehydrate", {
          method: "POST", headers: headers(true),
          body: JSON.stringify({
            image: src,
            filename: card._filename || (entry.data && entry.data.filename) || "image",
            params: { level: view.level, regions: view.regions,
                      stain_key: (entry.data.params || {}).stain_key || null },
          }),
        });
        if (!res.ok) throw new Error(String(res.status));
        const d = await res.json();
        if (!d.analysis_id) throw new Error("no id");

        d.images = Object.assign({}, d.images, { original: src });
        entry.data = d;
        card._rebind(d.analysis_id, d);
        done++;
      } catch (e) { failed++; markLost(card); }
      if (t) t.set((done + failed) / cards.length, `Restoring slides · ${done + failed} of ${cards.length}`);
    }
    saveState();
    if (t) {
      if (failed) t.fail(`Restored ${done} of ${cards.length} slides — ${failed} could not be rebuilt`);
      else t.done(`Restored ${done} slide${done === 1 ? "" : "s"}`);
    }
  } finally { healing = false; }
}

function markLost(card) {
  card.classList.add("expired");
  const note = card.querySelector(".card-note");
  if (note) {
    note.textContent = "This slide is no longer held by the server and could not be "
      + "rebuilt — it will not be included in an export. Re-upload it to measure it again.";
    note.classList.remove("hidden");
  }
}

setInterval(keepAlive, KEEPALIVE_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) keepAlive(); });

function showLightbox(src) { if (!src) return; $("lightboxImg").src = src; $("lightbox").classList.remove("hidden"); }
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));

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

  keepAlive();
})();
