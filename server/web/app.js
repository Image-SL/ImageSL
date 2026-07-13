/* ImageSL analyzer — talks only to the backend API. No analysis happens here. */
(() => {
  "use strict";

  // A license key can be injected by the desktop shell via ?key=... or window.IMAGESL_KEY.
  const KEY =
    new URLSearchParams(location.search).get("key") ||
    window.IMAGESL_KEY ||
    localStorage.getItem("imagesl_key") ||
    "";
  if (new URLSearchParams(location.search).get("key")) {
    localStorage.setItem("imagesl_key", KEY);
  }

  const state = { analysisId: null, images: {}, view: "render", targetIndex: 1 };

  const $ = (id) => document.getElementById(id);
  const headers = (extra = {}) => (KEY ? { "X-ImageSL-Key": KEY, ...extra } : extra);

  // ---- upload -------------------------------------------------------------
  const drop = $("drop"), fileInput = $("file");
  drop.addEventListener("click", () => fileInput.click());
  ["dragover", "dragenter"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (ev) => { if (ev.dataTransfer.files[0]) analyze(ev.dataTransfer.files[0]); });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) analyze(fileInput.files[0]); });

  function setStatus(msg, isErr = false, busy = false) {
    const s = $("status");
    s.className = "status" + (isErr ? " err" : "");
    s.innerHTML = (busy ? '<span class="spinner"></span>' : "") + (msg || "");
  }

  async function analyze(file) {
    setStatus(`Analyzing ${file.name}…`, false, true);
    const fd = new FormData();
    fd.append("file", file);
    fd.append("use_ai", $("useAI").checked ? "true" : "false");
    try {
      const res = await fetch("/api/analyze", { method: "POST", headers: headers(), body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const data = await res.json();
      state.analysisId = data.analysis_id;
      state.images = { original: data.images.original, overlay: data.images.overlay, render: data.images.original };
      state.targetIndex = data.result.target_index;
      renderResult(data);
      // start from a fresh render matching current slider values
      updateRender();
      setStatus("Analysis complete.");
    } catch (err) {
      setStatus("Error: " + err.message, true);
    }
  }

  function renderResult(data) {
    const r = data.result;
    $("metrics").style.display = "grid";
    $("mPct").innerHTML = r.positive_percent.toFixed(2) + "<small>%</small>";
    $("mOD").textContent = r.mean_positive_intensity.toFixed(3);
    $("mPos").textContent = r.positive_pixels.toLocaleString();
    $("mTissue").textContent = r.tissue_pixels.toLocaleString();
    const tag = $("methodTag");
    tag.style.display = "inline-block";
    tag.textContent = r.method;
    $("renderControls").style.display = "block";
    $("viewTabs").style.display = "flex";

    // sync target toggle
    document.querySelectorAll("#targetToggle button").forEach((b) =>
      b.classList.toggle("active", Number(b.dataset.t) === r.target_index));

    const v = data.vision;
    const note = $("visionNote");
    if (v && v.available && v.summary) {
      note.style.display = "block";
      note.innerHTML =
        `<b>AI vision:</b> ${escapeHtml(v.summary)} ` +
        (v.stain_type ? `<br><b>Stain:</b> ${escapeHtml(v.stain_type)} · target ${escapeHtml(v.target_chromogen_color || "")}` : "");
      if (v.suggested_background_hex && /^#[0-9a-f]{6}$/i.test(v.suggested_background_hex)) {
        $("bgColor").value = v.suggested_background_hex;
      }
    } else {
      note.style.display = v && v.summary ? "block" : "none";
      if (v && v.summary) note.innerHTML = escapeHtml(v.summary);
    }
    showView(state.view);
  }

  // ---- view tabs ----------------------------------------------------------
  document.querySelectorAll("#viewTabs button").forEach((b) =>
    b.addEventListener("click", () => showView(b.dataset.v)));

  function showView(view) {
    state.view = view;
    document.querySelectorAll("#viewTabs button").forEach((b) =>
      b.classList.toggle("active", b.dataset.v === view));
    const src = state.images[view];
    const viewer = $("viewer");
    if (src) viewer.innerHTML = `<img id="viewImg" src="${src}" alt="${view}" />`;
  }

  // ---- render sliders -----------------------------------------------------
  const targetGain = $("targetGain"), csGain = $("csGain"), bgColor = $("bgColor"), bgOn = $("bgOn");
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  function syncLabels() {
    $("tgVal").textContent = Number(targetGain.value).toFixed(2) + "×";
    $("csVal").textContent = Number(csGain.value).toFixed(2) + "×";
  }
  [targetGain, csGain].forEach((el) => el.addEventListener("input", () => { syncLabels(); debouncedRender(); }));
  [bgColor, bgOn].forEach((el) => el.addEventListener("input", () => debouncedRender()));
  const debouncedRender = debounce(updateRender, 180);

  async function updateRender() {
    if (!state.analysisId) return;
    const body = {
      analysis_id: state.analysisId,
      target_gain: Number(targetGain.value),
      counterstain_gain: Number(csGain.value),
    };
    if (bgOn.checked) body.background_rgb = hexToRgb(bgColor.value);
    try {
      const res = await fetch("/api/variant", {
        method: "POST", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const data = await res.json();
      state.images.render = data.image;
      if (state.view === "render") showView("render");
    } catch (err) {
      setStatus("Render error: " + err.message, true);
    }
  }

  // ---- target toggle ------------------------------------------------------
  document.querySelectorAll("#targetToggle button").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!state.analysisId) return;
      const ti = Number(b.dataset.t);
      setStatus("Recomputing target…", false, true);
      try {
        const res = await fetch("/api/set-target", {
          method: "POST", headers: headers({ "Content-Type": "application/json" }),
          body: JSON.stringify({ analysis_id: state.analysisId, target_index: ti }),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
        const data = await res.json();
        state.targetIndex = data.result.target_index;
        state.images.overlay = data.images.overlay;
        renderResult({ result: data.result, vision: { available: false }, images: state.images });
        await updateRender();
        setStatus("Target updated.");
      } catch (err) { setStatus("Error: " + err.message, true); }
    }));

  // ---- download render ----------------------------------------------------
  $("downloadRender").addEventListener("click", () => {
    if (!state.images.render) return;
    const a = document.createElement("a");
    a.href = state.images.render; a.download = "imagesl-render.png"; a.click();
  });

  // ---- chat (SSE) ---------------------------------------------------------
  const chatLog = $("chatLog"), chatInput = $("chatInput"), chatSend = $("chatSend");
  const history = [];

  function addMsg(role, text, cls = "") {
    const el = document.createElement("div");
    el.className = "msg " + (role === "user" ? "user" : "bot") + (cls ? " " + cls : "");
    el.textContent = text;
    chatLog.appendChild(el);
    chatLog.scrollTop = chatLog.scrollHeight;
    return el;
  }

  async function sendChat() {
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    addMsg("user", text);
    history.push({ role: "user", content: text });

    const botEl = addMsg("bot", "…", "thinking");
    let acc = "";
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ messages: history, analysis_id: state.analysisId }),
      });
      if (!res.ok || !res.body) throw new Error(res.statusText);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      botEl.classList.remove("thinking");
      botEl.textContent = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
          const line = part.replace(/^data: /, "").trim();
          if (!line) continue;
          try {
            const evt = JSON.parse(line);
            if (evt.delta) { acc += evt.delta; botEl.textContent = acc; chatLog.scrollTop = chatLog.scrollHeight; }
          } catch {}
        }
      }
      history.push({ role: "assistant", content: acc || "(no response)" });
    } catch (err) {
      botEl.classList.remove("thinking");
      botEl.textContent = "Error: " + err.message;
    }
  }

  chatSend.addEventListener("click", sendChat);
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });

  // ---- utils --------------------------------------------------------------
  function hexToRgb(hex) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [245, 243, 239];
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  syncLabels();
})();
