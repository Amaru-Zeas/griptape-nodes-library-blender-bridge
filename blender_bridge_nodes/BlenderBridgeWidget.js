export default function BlenderBridgeWidget(container, props) {
  var value = props.value || {};
  var onChange = props.onChange;
  var disabled = !!props.disabled;
  var widgetHeight = (props.height && props.height > 0) ? Math.max(340, props.height - 24) : 400;

  var state = {
    port: 7890,
    serverRunning: false,
    incomingVersion: 0,
    incomingFilename: "",
    incomingKind: "image",
    incomingPreview: "",
    incomingPrompt: "",
    autoLoad: true,
    status: "Waiting for media from Blender.",
    sendNonce: 0,
    pullNonce: 0
  };

  if (value && typeof value === "object") {
    if (value.port !== undefined) state.port = Number(value.port) || 7890;
    if (value.serverRunning !== undefined) state.serverRunning = !!value.serverRunning;
    if (value.incomingVersion !== undefined) state.incomingVersion = Number(value.incomingVersion) || 0;
    if (value.incomingFilename !== undefined) state.incomingFilename = String(value.incomingFilename || "");
    if (value.incomingKind !== undefined) state.incomingKind = String(value.incomingKind || "image");
    if (value.incomingPreview !== undefined) state.incomingPreview = String(value.incomingPreview || "");
    if (value.incomingPrompt !== undefined) state.incomingPrompt = String(value.incomingPrompt || "");
    if (value.autoLoad !== undefined) state.autoLoad = !!value.autoLoad;
    if (value.status !== undefined) state.status = String(value.status || "");
    if (value.sendNonce !== undefined) state.sendNonce = Number(value.sendNonce) || 0;
    if (value.pullNonce !== undefined) state.pullNonce = Number(value.pullNonce) || 0;
  }

  var lastServerVersion = state.incomingVersion;

  container.innerHTML =
    '<div class="bl-bridge nodrag nowheel" style="' +
      "display:flex;flex-direction:column;gap:8px;" +
      "padding:8px;background:#12100e;border:1px solid #4a3b28;border-radius:8px;" +
      "font-family:monospace;font-size:11px;color:#f4ede2;width:100%;box-sizing:border-box;" +
      "height:" + widgetHeight + "px;overflow:hidden;" +
    '">' +
      '<div style="display:flex;gap:6px;align-items:center;">' +
        '<span class="dot" style="width:9px;height:9px;border-radius:50%;background:#c0392b;flex:0 0 auto;"></span>' +
        '<span class="lbl-server" style="color:#c9b795;">bridge</span>' +
        '<span style="margin-left:auto;color:#c9b795;">Port</span>' +
        '<input class="inp-port" type="number" min="1" max="65535" value="' + String(state.port) + '" style="' +
          "width:72px;padding:3px 6px;background:#0d0b09;border:1px solid #4a3b28;border-radius:4px;color:#f4ede2;outline:none;" +
        '" />' +
      "</div>" +
      '<div class="preview" style="' +
        "flex:1;min-height:80px;display:flex;align-items:center;justify-content:center;" +
        "background:#0a0806;border:1px solid #4a3b28;border-radius:6px;overflow:hidden;" +
      '">' +
        '<span class="preview-empty" style="color:#a8946e;">No media from Blender yet.</span>' +
        '<img class="preview-img" alt="incoming" style="max-width:100%;max-height:100%;object-fit:contain;display:none;" />' +
        '<video class="preview-vid" muted controls loop style="max-width:100%;max-height:100%;object-fit:contain;display:none;"></video>' +
      "</div>" +
      '<div class="lbl-prompt" style="' +
        "min-height:14px;max-height:40px;overflow:hidden;color:#d8c49a;white-space:nowrap;text-overflow:ellipsis;" +
      '"></div>' +
      '<div style="display:flex;gap:6px;align-items:center;">' +
        '<label style="display:flex;gap:4px;align-items:center;color:#c9b795;cursor:pointer;">' +
          '<input class="chk-auto" type="checkbox" ' + (state.autoLoad ? "checked" : "") + " /> auto-detect</label>" +
        '<button class="btn-pull" style="' +
          "margin-left:auto;padding:4px 10px;border-radius:4px;border:1px solid #4a3b28;background:#241d13;color:#f4ede2;cursor:pointer;" +
        '">Pull</button>' +
        '<button class="btn-send" style="' +
          "padding:4px 10px;border-radius:4px;border:1px solid #e87d0d;background:#b35f06;color:#fff;cursor:pointer;font-weight:bold;" +
        '">Send to Blender</button>' +
      "</div>" +
      '<div class="lbl-status" style="min-height:14px;color:#a8946e;"></div>' +
    "</div>";

  var dot = container.querySelector(".dot");
  var lblServer = container.querySelector(".lbl-server");
  var inpPort = container.querySelector(".inp-port");
  var previewEmpty = container.querySelector(".preview-empty");
  var previewImg = container.querySelector(".preview-img");
  var previewVid = container.querySelector(".preview-vid");
  var lblPrompt = container.querySelector(".lbl-prompt");
  var chkAuto = container.querySelector(".chk-auto");
  var btnPull = container.querySelector(".btn-pull");
  var btnSend = container.querySelector(".btn-send");
  var lblStatus = container.querySelector(".lbl-status");
  var lastSyncedPayload = null;
  var pollTimer = null;

  function stop(e) { e.stopPropagation(); }
  function stopAll(e) { e.stopPropagation(); e.preventDefault(); }

  function baseUrl() {
    return "http://127.0.0.1:" + (Number(state.port) || 7890);
  }

  function renderServer(running) {
    state.serverRunning = !!running;
    dot.style.background = running ? "#27ae60" : "#c0392b";
    lblServer.textContent = running ? "bridge online" : "bridge offline";
  }

  function renderPreview() {
    if (state.incomingKind === "video" && state.incomingVersion > 0) {
      previewImg.style.display = "none";
      previewEmpty.style.display = "none";
      previewVid.style.display = "block";
      previewVid.src = baseUrl() + "/to_gtn/latest/raw?v=" + state.incomingVersion;
    } else if (state.incomingPreview) {
      previewVid.style.display = "none";
      previewVid.removeAttribute("src");
      previewImg.src = state.incomingPreview;
      previewImg.style.display = "block";
      previewEmpty.style.display = "none";
    } else {
      previewImg.style.display = "none";
      previewVid.style.display = "none";
      previewVid.removeAttribute("src");
      previewEmpty.style.display = "block";
    }
  }

  function renderPrompt() {
    lblPrompt.textContent = state.incomingPrompt ? ("prompt: " + state.incomingPrompt) : "";
    lblPrompt.title = state.incomingPrompt || "";
  }

  function renderStatus() {
    lblStatus.textContent = state.status || "";
  }

  function syncToNode() {
    if (!onChange) return;
    var payload = {
      port: Number(state.port) || 7890,
      serverRunning: state.serverRunning,
      incomingVersion: state.incomingVersion,
      incomingFilename: state.incomingFilename,
      incomingKind: state.incomingKind,
      incomingPreview: state.incomingPreview,
      incomingPrompt: state.incomingPrompt,
      autoLoad: state.autoLoad,
      status: state.status,
      sendNonce: state.sendNonce,
      pullNonce: state.pullNonce
    };
    var serialized;
    try { serialized = JSON.stringify(payload); } catch (_) { serialized = null; }
    if (serialized && serialized === lastSyncedPayload) return;
    lastSyncedPayload = serialized;
    onChange(payload);
  }

  function poll() {
    if (disabled) return;
    var url = baseUrl() + "/to_gtn/latest";
    fetch(url, { method: "GET" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (meta) {
        renderServer(true);
        if (!meta || typeof meta !== "object") return;
        var v = Number(meta.version) || 0;
        if (v > lastServerVersion && meta.hasMedia) {
          lastServerVersion = v;
          if (state.autoLoad) {
            state.pullNonce = (Number(state.pullNonce) || 0) + 1;
            state.status = "New " + (meta.kind || "media") + " v" + v + " from Blender - run node to load.";
            renderStatus();
            syncToNode();
          } else {
            state.status = "New " + (meta.kind || "media") + " v" + v + " available. Click Pull.";
            renderStatus();
          }
        }
      })
      .catch(function () {
        renderServer(false);
      });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(poll, 1500);
    poll();
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  inpPort.addEventListener("keydown", stop);
  inpPort.addEventListener("input", function (e) {
    stop(e);
    state.port = Math.max(1, Math.min(65535, parseInt(inpPort.value || "7890", 10) || 7890));
    syncToNode();
  });

  chkAuto.addEventListener("change", function (e) {
    stop(e);
    state.autoLoad = !!chkAuto.checked;
    syncToNode();
  });

  btnPull.addEventListener("click", function (e) {
    stopAll(e);
    if (disabled) return;
    state.pullNonce = (Number(state.pullNonce) || 0) + 1;
    state.status = "Pulling latest media from Blender...";
    renderStatus();
    syncToNode();
  });

  btnSend.addEventListener("click", function (e) {
    stopAll(e);
    if (disabled) return;
    state.sendNonce = (Number(state.sendNonce) || 0) + 1;
    state.status = "Sending media to Blender...";
    renderStatus();
    syncToNode();
  });

  if (disabled) {
    btnPull.disabled = true;
    btnSend.disabled = true;
    btnPull.style.opacity = "0.6";
    btnSend.style.opacity = "0.6";
    btnPull.style.cursor = "not-allowed";
    btnSend.style.cursor = "not-allowed";
  }

  renderServer(state.serverRunning);
  renderPreview();
  renderPrompt();
  renderStatus();
  syncToNode();
  startPolling();

  return function cleanup() {
    stopPolling();
    container.innerHTML = "";
  };
}
