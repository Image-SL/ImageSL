"use strict";

const $ = (id) => document.getElementById(id);
let analysisId = null;
const messages = [];

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
// Analyze
// --------------------------------------------------------------------------
$("analyze").addEventListener("click", async () => {
  const f = $("file").files[0];
  if (!f) { setStatus("Choose a file first."); return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("use_ai", $("useAi").checked ? "true" : "false");

  $("analyze").disabled = true;
  setStatus("Analyzing…");
  try {
    const res = await fetch("/api/analyze", { method: "POST", headers: headers(false), body: fd });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    analysisId = data.analysis_id;
    $("results").classList.remove("hidden");
    render(data, true);
    renderVision(data.vision);
    setStatus("Done.");
  } catch (e) {
    setStatus("Error: " + e.message);
  } finally {
    $("analyze").disabled = false;
  }
});

// --------------------------------------------------------------------------
// Render helpers
// --------------------------------------------------------------------------
function render(data, setControls) {
  if (data.images) {
    if (data.images.original) $("imgOriginal").src = data.images.original;
    if (data.images.overlay) $("imgOverlay").src = data.images.overlay;
    if (data.images.variant) $("imgVariant").src = data.images.variant;
  }
  if (data.result) {
    const r = data.result;
    $("mPercent").textContent = r.positive_percent.toFixed(2) + "%";
    $("mPositive").textContent = r.positive_pixels.toLocaleString();
    $("mTissue").textContent = r.tissue_pixels.toLocaleString();
    $("mThreshold").textContent = r.threshold.toFixed(3);
    $("mMethod").textContent = r.method;
  }
  if (data.params) {
    const p = data.params;
    if (setControls) {
      $("cBg").value = p.background_threshold;
      $("cScale").value = p.threshold_scale;
      $("cTarget").value = String(data.result ? data.result.target_index : p.target_index);
      $("cGain").value = p.target_gain;
      $("cColor").value = p.background_hex || "#ffffff";
    }
    $("vBg").textContent = (+p.background_threshold).toFixed(2);
    $("vScale").textContent = (+p.threshold_scale).toFixed(2);
    $("vGain").textContent = (+p.target_gain).toFixed(2);
  }
}

function renderVision(v) {
  $("visionNote").textContent = (v && v.available && v.summary) ? ("AI vision: " + v.summary) : "";
}

// --------------------------------------------------------------------------
// Manual controls
// --------------------------------------------------------------------------
let debounce;
function recalc() {
  if (!analysisId) return;
  clearTimeout(debounce);
  debounce = setTimeout(async () => {
    const body = {
      analysis_id: analysisId,
      background_threshold: parseFloat($("cBg").value),
      threshold_scale: parseFloat($("cScale").value),
      target_index: parseInt($("cTarget").value, 10),
    };
    const res = await fetch("/api/recalculate", { method: "POST", headers: headers(true), body: JSON.stringify(body) });
    if (res.ok) render(await res.json(), false);
  }, 220);
}
function appearance() {
  if (!analysisId) return;
  clearTimeout(debounce);
  debounce = setTimeout(async () => {
    const body = {
      analysis_id: analysisId,
      target_gain: parseFloat($("cGain").value),
      background_hex: $("cColor").dataset.cleared ? null : $("cColor").value,
    };
    const res = await fetch("/api/appearance", { method: "POST", headers: headers(true), body: JSON.stringify(body) });
    if (res.ok) render(await res.json(), false);
  }, 150);
}

$("cBg").addEventListener("input", () => { $("vBg").textContent = (+$("cBg").value).toFixed(2); recalc(); });
$("cScale").addEventListener("input", () => { $("vScale").textContent = (+$("cScale").value).toFixed(2); recalc(); });
$("cTarget").addEventListener("change", recalc);
$("cGain").addEventListener("input", () => { $("vGain").textContent = (+$("cGain").value).toFixed(2); appearance(); });
$("cColor").addEventListener("input", () => { $("cColor").dataset.cleared = ""; appearance(); });
$("clearColor").addEventListener("click", () => { $("cColor").dataset.cleared = "1"; appearance(); });

// --------------------------------------------------------------------------
// Chat (agentic — can recalculate)
// --------------------------------------------------------------------------
function addMsg(who, text, cls) {
  const d = document.createElement("div");
  d.className = "msg " + (cls || "");
  d.innerHTML = '<div class="who"></div><div class="text"></div>';
  d.querySelector(".who").textContent = who;
  d.querySelector(".text").textContent = text;
  $("chat").appendChild(d);
  $("chat").scrollTop = $("chat").scrollHeight;
  return d;
}

$("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("chatInput").value.trim();
  if (!text) return;
  $("chatInput").value = "";
  messages.push({ role: "user", content: text });
  addMsg("You", text, "user");
  const pending = addMsg("Assistant", "…", "pending");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({ messages, analysis_id: analysisId }),
    });
    const data = await res.json();
    pending.remove();
    const reply = data.reply || "(no reply)";
    messages.push({ role: "assistant", content: reply });
    addMsg("Assistant", reply);
    if (data.updated) {
      render(data, true);
      addMsg("System", "↳ analysis recalculated", "recalc");
    }
  } catch (e2) {
    pending.remove();
    addMsg("Assistant", "Error: " + e2.message);
  }
});
