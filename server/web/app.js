"use strict";

const $ = (id) => document.getElementById(id);
let analyses = [];

// --- access key persistence ---
$("key").value = localStorage.getItem("imagesl_key") || "";
$("key").addEventListener("change", () => localStorage.setItem("imagesl_key", $("key").value.trim()));

function headers(json) {
  const h = {};
  const k = $("key").value.trim();
  if (k) h["X-ImageSL-Key"] = k;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

function setStatus(msg) { $("status").textContent = msg || ""; }

// --------------------------------------------------------------------------
// Analyze & Dashboard Rendering
// --------------------------------------------------------------------------
async function runAnalysis(files) {
  if (!files || !files.length) { setStatus("Choose files first."); return; }
  
  $("resultsContainer").innerHTML = ""; // clear previous
  analyses = [];

  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    setStatus(`Analyzing ${i + 1} of ${files.length} (${f.name})…`);
    const fd = new FormData();
    fd.append("file", f);
    fd.append("use_ai", $("useAi").checked ? "true" : "false");

    try {
      const res = await fetch("/api/analyze", { method: "POST", headers: headers(false), body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const data = await res.json();
      data._filename = f.name;
      analyses.push(data);
      
      createCard(data);
      
      // Force browser to repaint so the user sees the card instantly
      await new Promise(r => setTimeout(r, 100));
    } catch (e) {
      console.error(`Error on ${f.name}:`, e);
      setStatus("Error: " + e.message);
    }
  }

  if (analyses.length > 0) {
    setStatus(`Done analyzing ${analyses.length} image(s).`);
  }
}

// Drag & Drop Listeners
const dropzone = $("dropzone");
const fileInput = $("file");

dropzone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", (e) => {
  runAnalysis(e.target.files);
  e.target.value = ""; // reset
});

dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});
dropzone.addEventListener("dragleave", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
});
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    runAnalysis(e.dataTransfer.files);
  }
});

// --------------------------------------------------------------------------
// Card Creation Logic
// --------------------------------------------------------------------------
function createCard(data) {
  const template = $("resultTemplate");
  const clone = template.content.cloneNode(true);
  
  // Scoped querying function
  const qs = (sel) => clone.querySelector(sel);
  
  qs(".filename-header").textContent = data._filename;

  let currentData = data;
  let analysisId = data.analysis_id;
  const messages = [];

  // Render function
  function renderState(d, setControls) {
    if (d.images) {
      if (d.images.original) qs(".imgOriginal").src = d.images.original;
      if (d.images.overlay) qs(".imgOverlay").src = d.images.overlay;
      if (d.images.stainA) qs(".imgStainA").src = d.images.stainA;
      if (d.images.stainB) qs(".imgStainB").src = d.images.stainB;
    }
    if (d.result) {
      const r = d.result;
      qs(".mPercent").textContent = r.positive_percent.toFixed(2) + "%";
      qs(".mPositive").textContent = r.positive_pixels.toLocaleString();
      qs(".mTissue").textContent = r.tissue_pixels.toLocaleString();
      qs(".mThreshold").textContent = r.threshold.toFixed(3);
      qs(".mMethod").textContent = r.method;
    }
    if (d.params) {
      const p = d.params;
      if (setControls) {
        qs(".cBg").value = p.background_threshold;
        qs(".cScale").value = p.threshold_scale;
        qs(".cTarget").value = String(d.result ? d.result.target_index : p.target_index);
        qs(".cGain").value = p.target_gain;
        qs(".cColor").value = p.background_hex || "#ffffff";
      }
      qs(".vBg").textContent = (+p.background_threshold).toFixed(2);
      qs(".vScale").textContent = (+p.threshold_scale).toFixed(2);
      qs(".vGain").textContent = (+p.target_gain).toFixed(2);
    }
    if (d.vision) {
      qs(".visionNote").textContent = (d.vision.available && d.vision.summary) ? ("AI vision: " + d.vision.summary) : "";
    }
  }

  // Initial render
  renderState(currentData, true);

  // Lightbox bindings
  qs(".imgOriginal").addEventListener("click", (e) => showLightbox(e.target.src));
  qs(".imgOverlay").addEventListener("click", (e) => showLightbox(e.target.src));
  qs(".imgStainA").addEventListener("click", (e) => showLightbox(e.target.src));
  qs(".imgStainB").addEventListener("click", (e) => showLightbox(e.target.src));

  // Controls logic
  let debounce;
  function recalc() {
    clearTimeout(debounce);
    debounce = setTimeout(async () => {
      const body = {
        analysis_id: analysisId,
        background_threshold: parseFloat(qs(".cBg").value),
        threshold_scale: parseFloat(qs(".cScale").value),
        target_index: parseInt(qs(".cTarget").value, 10),
      };
      const res = await fetch("/api/recalculate", { method: "POST", headers: headers(true), body: JSON.stringify(body) });
      if (res.ok) {
        currentData = await res.json();
        renderState(currentData, false);
      }
    }, 220);
  }
  
  function appearance() {
    clearTimeout(debounce);
    debounce = setTimeout(async () => {
      const body = {
        analysis_id: analysisId,
        target_gain: parseFloat(qs(".cGain").value),
        background_hex: qs(".cColor").dataset.cleared ? null : qs(".cColor").value,
      };
      const res = await fetch("/api/appearance", { method: "POST", headers: headers(true), body: JSON.stringify(body) });
      if (res.ok) {
        currentData = await res.json();
        renderState(currentData, false);
      }
    }, 150);
  }

  qs(".cBg").addEventListener("input", () => { qs(".vBg").textContent = (+qs(".cBg").value).toFixed(2); recalc(); });
  qs(".cScale").addEventListener("input", () => { qs(".vScale").textContent = (+qs(".cScale").value).toFixed(2); recalc(); });
  qs(".cTarget").addEventListener("change", recalc);
  qs(".cGain").addEventListener("input", () => { qs(".vGain").textContent = (+qs(".cGain").value).toFixed(2); appearance(); });
  qs(".cColor").addEventListener("input", () => { qs(".cColor").dataset.cleared = ""; appearance(); });
  qs(".clearColor").addEventListener("click", (e) => { e.preventDefault(); qs(".cColor").dataset.cleared = "1"; appearance(); });

  // Download bindings
  const download = (type) => { window.location.href = `/api/download_tif?analysis_id=${analysisId}&image_type=${type}`; };
  qs(".dl-original").addEventListener("click", () => download("original"));
  qs(".dl-stainA").addEventListener("click", () => download("stainA"));
  qs(".dl-stainB").addEventListener("click", () => download("stainB"));
  qs(".dl-overlay").addEventListener("click", () => download("overlay"));
  qs(".dl-comparison").addEventListener("click", () => download("comparison"));

  // Chat logic
  const chatBox = qs(".chat");
  function addMsg(who, text, cls) {
    const d = document.createElement("div");
    d.className = "msg " + (cls || "");
    d.innerHTML = '<div class="who"></div><div class="text"></div>';
    d.querySelector(".who").textContent = who;
    d.querySelector(".text").textContent = text;
    chatBox.appendChild(d);
    chatBox.scrollTop = chatBox.scrollHeight;
    return d;
  }

  qs(".chatForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const chatInput = qs(".chatInput");
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    messages.push({ role: "user", content: text });
    addMsg("You", text, "user");
    const pending = addMsg("Assistant", "…", "pending");

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({ messages, analysis_id: analysisId }),
      });
      const resData = await res.json();
      pending.remove();
      const reply = resData.reply || "(no reply)";
      messages.push({ role: "assistant", content: reply });
      addMsg("Assistant", reply);
      if (resData.updated) {
        currentData = resData;
        renderState(currentData, true);
        addMsg("System", "↳ analysis recalculated", "recalc");
      }
    } catch (e2) {
      pending.remove();
      addMsg("Assistant", "Error: " + e2.message);
    }
  });

  // Append card
  $("resultsContainer").appendChild(clone);
}

// --------------------------------------------------------------------------
// Lightbox Global
// --------------------------------------------------------------------------
function showLightbox(src) {
  if (!src) return;
  $("lightboxImg").src = src;
  $("lightbox").classList.remove("hidden");
}
$("lightbox").addEventListener("click", () => {
  $("lightbox").classList.add("hidden");
});
