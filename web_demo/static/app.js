const I18N = {
  zh: {
    apiKeyLabel: "DeepSeek API Key",
    apiKeyPlaceholder: "留空使用服务器默认 key",
    assetMissing: "文件缺失",
    assetSaved: "已保存",
    assetsTitle: "素材",
    attachment: "附件",
    check: "检查",
    clear: "清空",
    clearConfirm: "确定清空已保存的 API Key 吗？",
    close: "关闭",
    commandsAria: "快捷指令",
    connecting: "正在连接服务器…",
    connectionError: "无法连接服务器，请重试",
    download: "下载",
    dropHint: "松开以上传素材",
    emptyAssets: "暂无素材 — 上传片段开始",
    emptyJobs: "暂无任务 — 发送剪辑指令开始",
    file: "文件",
    image: "图片",
    jobsTitle: "任务",
    log: "日志",
    messagePlaceholder: "输入剪辑指令，或点上方快捷指令",
    messagesAria: "消息",
    pause: "暂停",
    pauseTitle: "暂停当前操作",
    processing: "处理中",
    refresh: "刷新",
    refreshTitle: "刷新状态",
    retry: "重试",
    save: "保存",
    send: "发送",
    sendFailed: "发送失败，请重试",
    sendTitle: "发送消息",
    sessionPrefix: "会话",
    settings: "设置",
    settingsHeading: "设置",
    settingsTitle: "DeepSeek API Key",
    statusAria: "状态",
    thinking: "正在处理…",
    unknownJob: "任务",
    upload: "上传",
    uploadFailed: "上传失败，请重试",
    uploadTitle: "上传素材",
    video: "视频",
    audio: "音频",
    webDemo: "Web demo",
  },
  en: {
    apiKeyLabel: "DeepSeek API Key",
    apiKeyPlaceholder: "Leave empty to use the server default key",
    assetMissing: "Missing file",
    assetSaved: "Saved",
    assetsTitle: "Assets",
    attachment: "attachment",
    check: "Check",
    clear: "Clear",
    clearConfirm: "Clear the saved API key?",
    close: "Close",
    commandsAria: "Quick commands",
    connecting: "Connecting to server…",
    connectionError: "Can't reach the server — retry",
    download: "Download",
    dropHint: "Drop files to upload",
    emptyAssets: "No assets yet — upload a clip to begin",
    emptyJobs: "No jobs yet — send an edit instruction to start",
    file: "File",
    image: "Image",
    jobsTitle: "Jobs",
    log: "Log",
    messagePlaceholder: "Type an edit instruction, or tap a command above",
    messagesAria: "Messages",
    pause: "Pause",
    pauseTitle: "Pause the current operation",
    processing: "Processing",
    refresh: "Refresh",
    refreshTitle: "Refresh status",
    retry: "Retry",
    save: "Save",
    send: "Send",
    sendFailed: "Couldn't send — please retry",
    sendTitle: "Send message",
    sessionPrefix: "Session",
    settings: "Settings",
    settingsHeading: "Settings",
    settingsTitle: "DeepSeek API Key",
    statusAria: "Status",
    thinking: "Thinking…",
    unknownJob: "job",
    upload: "Upload",
    uploadFailed: "Couldn't upload — please retry",
    uploadTitle: "Upload assets",
    video: "Video",
    audio: "Audio",
    webDemo: "Web demo",
  },
};

function initialLanguage() {
  const stored = localStorage.getItem("omc_web_language");
  if (stored === "zh" || stored === "en") return stored;
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
}

const POLL_BASE = 3000;
const POLL_MAX = 15000;

const state = {
  sessionId: "",
  cleanupSessionId: localStorage.getItem("omc_web_session") || "",
  language: initialLanguage(),
  lastEventId: 0,
  pollTimer: null,
  pollDelay: POLL_BASE,
  connection: "connecting",
  cleanupSent: false,
};

const messagesEl = document.querySelector("#messages");
const assetsEl = document.querySelector("#assets");
const jobsEl = document.querySelector("#jobs");
const sessionLabel = document.querySelector("#sessionLabel");
const composer = document.querySelector("#composer");
const chatPane = document.querySelector(".chat-pane");
const messageInput = document.querySelector("#messageInput");
const fileInput = document.querySelector("#fileInput");
const uploadBtn = document.querySelector("#uploadBtn");
const sendBtn = document.querySelector("#sendBtn");
const sendLabel = sendBtn.querySelector("[data-i18n='send']");
const refreshBtn = document.querySelector("#refreshBtn");
const cutBtn = document.querySelector("#cutBtn");
const checkAssetsBtn = document.querySelector("#checkAssetsBtn");
const settingsBtn = document.querySelector("#settingsBtn");
const settingsDialog = document.querySelector("#settingsDialog");
const apiKeyInput = document.querySelector("#apiKeyInput");
const saveKeyBtn = document.querySelector("#saveKeyBtn");
const clearKeyBtn = document.querySelector("#clearKeyBtn");
const langZhBtn = document.querySelector("#langZhBtn");
const langEnBtn = document.querySelector("#langEnBtn");
const connectionBanner = document.querySelector("#connectionBanner");
const connectionText = document.querySelector("#connectionText");
const retryBtn = document.querySelector("#retryBtn");

function t(key) {
  return (I18N[state.language] || I18N.zh)[key] || I18N.zh[key] || key;
}

function apiKey() {
  return sessionStorage.getItem("omc_deepseek_key") || "";
}

function headers(json = true) {
  const result = {};
  if (json) result["Content-Type"] = "application/json";
  const key = apiKey();
  if (key) result["X-DeepSeek-Api-Key"] = key;
  return result;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function formatSize(bytes) {
  if (!bytes) return "";
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function setConnection(connection) {
  state.connection = connection;
  connectionBanner.classList.toggle("is-connecting", connection === "connecting");
  connectionBanner.classList.toggle("is-error", connection === "error");
  if (connection === "ok") {
    connectionBanner.hidden = true;
    retryBtn.hidden = true;
    return;
  }
  connectionBanner.hidden = false;
  connectionText.textContent = connection === "connecting" ? t("connecting") : t("connectionError");
  retryBtn.hidden = connection !== "error";
}

function updateSessionLabel() {
  sessionLabel.replaceChildren();
  if (!state.sessionId) {
    sessionLabel.textContent = t("webDemo");
    return;
  }
  sessionLabel.append(document.createTextNode(`${t("sessionPrefix")}: `));
  const id = document.createElement("span");
  id.className = "mono";
  id.textContent = state.sessionId;
  sessionLabel.appendChild(id);
}

function applyI18n() {
  document.documentElement.lang = state.language === "zh" ? "zh-CN" : "en";
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-title]")) {
    node.title = t(node.dataset.i18nTitle);
  }
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  }
  for (const node of document.querySelectorAll("[data-i18n-aria-label]")) {
    node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
  }
  langZhBtn.classList.toggle("active", state.language === "zh");
  langEnBtn.classList.toggle("active", state.language === "en");
  langZhBtn.setAttribute("aria-pressed", String(state.language === "zh"));
  langEnBtn.setAttribute("aria-pressed", String(state.language === "en"));
  updateSessionLabel();
  if (!connectionBanner.hidden) setConnection(state.connection);
}

async function setServerLanguage() {
  if (!state.sessionId) return;
  try {
    await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/language`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: state.language }),
    });
  } catch (_error) {
    // Language preference is best-effort; the local UI can still switch.
  }
}

async function setLanguage(language) {
  if (language !== "zh" && language !== "en") return;
  state.language = language;
  localStorage.setItem("omc_web_language", language);
  applyI18n();
  await setServerLanguage();
  await refreshAll().catch(() => {});
}

async function ensureSession() {
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cleanup_session_id: state.cleanupSessionId,
      language: state.language,
    }),
  });
  if (!response.ok) throw new Error(`session request failed: ${response.status}`);
  const payload = await response.json();
  state.sessionId = payload.session_id;
  state.cleanupSessionId = "";
  state.lastEventId = 0;
  messagesEl.replaceChildren();
  localStorage.setItem("omc_web_session", state.sessionId);
  updateSessionLabel();
  await setServerLanguage();
}

function cleanupCurrentSession() {
  if (!state.sessionId || state.cleanupSent) return;
  state.cleanupSent = true;
  const url = `/api/sessions/${encodeURIComponent(state.sessionId)}/cleanup`;
  localStorage.setItem("omc_web_session", state.sessionId);
  if (navigator.sendBeacon) {
    const payload = new Blob(["{}"], { type: "application/json" });
    navigator.sendBeacon(url, payload);
    return;
  }
  fetch(url, { method: "POST", keepalive: true }).catch(() => {});
}

function renderEvent(event) {
  if (event.id && document.querySelector(`[data-event-id="${event.id}"]`)) return;
  const node = document.createElement("article");
  node.className = `message ${event.role || "assistant"}`;
  if (event.id) node.dataset.eventId = event.id;
  const text = document.createElement("div");
  text.textContent = event.text || "";
  node.appendChild(text);
  if (event.has_attachment && event.attachment_url) {
    const wrap = document.createElement("div");
    wrap.className = "attachment";
    if (event.attachment_type === "image") {
      const img = document.createElement("img");
      img.src = event.attachment_url;
      img.alt = event.attachment_name || t("attachment");
      wrap.appendChild(img);
    } else if (event.attachment_type === "video") {
      const video = document.createElement("video");
      video.controls = true;
      video.src = event.attachment_url;
      wrap.appendChild(video);
    } else {
      const link = document.createElement("a");
      link.href = event.attachment_url;
      link.textContent = event.attachment_name || t("attachment");
      link.target = "_blank";
      wrap.appendChild(link);
    }
    node.appendChild(wrap);
  }
  messagesEl.appendChild(node);
  scrollToBottom();
}

function renderError(message, onRetry) {
  const node = document.createElement("article");
  node.className = "message error";
  node.setAttribute("role", "alert");
  const row = document.createElement("div");
  row.className = "error-row";
  const icon = document.createElement("span");
  icon.className = "error-icon";
  icon.textContent = "⚠";
  icon.setAttribute("aria-hidden", "true");
  const text = document.createElement("div");
  text.textContent = message;
  row.append(icon, text);
  node.appendChild(row);
  if (onRetry) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "retry-inline";
    button.textContent = t("retry");
    button.addEventListener("click", () => {
      node.remove();
      onRetry();
    });
    node.appendChild(button);
  }
  messagesEl.appendChild(node);
  scrollToBottom();
}

function showThinking() {
  if (messagesEl.querySelector(".message.thinking")) return;
  const node = document.createElement("article");
  node.className = "message assistant thinking thinking-ring";
  node.setAttribute("aria-label", t("thinking"));
  const dots = document.createElement("span");
  dots.className = "typing";
  dots.innerHTML = "<i></i><i></i><i></i>";
  node.appendChild(dots);
  messagesEl.appendChild(node);
  scrollToBottom();
}

function hideThinking() {
  messagesEl.querySelector(".message.thinking")?.remove();
}

async function pollEvents() {
  if (!state.sessionId) return;
  const response = await fetch(`/api/events?session_id=${encodeURIComponent(state.sessionId)}&after=${state.lastEventId}`);
  if (!response.ok) return;
  const payload = await response.json();
  for (const event of payload.events || []) {
    state.lastEventId = Math.max(state.lastEventId, Number(event.id || 0));
    renderEvent(event);
  }
}

function mediaTypeLabel(type) {
  if (type === "video") return t("video");
  if (type === "image") return t("image");
  if (type === "audio") return t("audio");
  return t("file");
}

function makeBadge(text, variant) {
  const badge = document.createElement("span");
  badge.className = `badge ${variant}`;
  badge.textContent = text;
  return badge;
}

function emptyCard(text) {
  const card = document.createElement("div");
  card.className = "status-card empty";
  card.textContent = text;
  return card;
}

function statusVariant(status) {
  const value = String(status || "").toLowerCase();
  if (/(done|complete|success|finish|ready)/.test(value)) return "success";
  if (/(fail|error|cancel)/.test(value)) return "danger";
  if (/(run|process|pend|queue|active|start)/.test(value)) return "info";
  return "neutral";
}

function assetCard(asset) {
  const card = document.createElement("div");
  card.className = "status-card";

  const title = document.createElement("span");
  title.className = "card-title";
  title.textContent = asset.original_name || asset.asset_id || "asset";
  card.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "card-meta";
  const type = document.createElement("span");
  type.textContent = mediaTypeLabel(asset.media_type || "file");
  meta.appendChild(type);

  const size = formatSize(asset.size_bytes);
  if (size) {
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.textContent = "·";
    const sizeNode = document.createElement("span");
    sizeNode.className = "mono";
    sizeNode.textContent = size;
    meta.append(dot, sizeNode);
  }

  const sep = document.createElement("span");
  sep.className = "dot";
  sep.textContent = "·";
  const missing = asset.exists === false;
  meta.append(sep, makeBadge(missing ? t("assetMissing") : t("assetSaved"), missing ? "danger" : "success"));
  card.appendChild(meta);
  return card;
}

function jobCard(job) {
  const card = document.createElement("div");
  card.className = "status-card";
  const report = job.report || {};
  const downloads = job.downloads || [];

  const title = document.createElement("span");
  title.className = "card-title";
  const id = document.createElement("span");
  id.className = "mono";
  id.textContent = job.job_id || t("unknownJob");
  title.appendChild(id);
  card.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "card-meta";
  meta.appendChild(makeBadge(job.status || "unknown", statusVariant(job.status)));
  card.appendChild(meta);

  const summary = report.user_summary || report.latest_progress || "";
  if (summary) {
    const body = document.createElement("p");
    body.textContent = summary;
    card.appendChild(body);
  }

  for (const item of downloads) {
    const link = document.createElement("a");
    link.href = item.url;
    link.textContent = `${t("download")} ${item.filename}`;
    link.target = "_blank";
    card.appendChild(link);
  }
  if (job.log_url) {
    const logLink = document.createElement("a");
    logLink.href = job.log_url;
    logLink.textContent = `${t("log")} ${job.job_id || ""}`.trim();
    logLink.target = "_blank";
    card.appendChild(logLink);
  }
  return card;
}

async function refreshAssets() {
  if (!state.sessionId) return;
  const response = await fetch(`/api/assets?session_id=${encodeURIComponent(state.sessionId)}`);
  if (!response.ok) return;
  const payload = await response.json();
  const assets = ((payload.data || {}).manifest || {}).assets || [];
  assetsEl.replaceChildren();
  if (!assets.length) {
    assetsEl.appendChild(emptyCard(t("emptyAssets")));
    return;
  }
  for (const asset of assets) assetsEl.appendChild(assetCard(asset));
}

async function refreshJobs() {
  if (!state.sessionId) return;
  const response = await fetch(`/api/jobs?session_id=${encodeURIComponent(state.sessionId)}&limit=8`);
  if (!response.ok) return;
  const payload = await response.json();
  const jobs = (payload.data || {}).jobs || [];
  jobsEl.replaceChildren();
  if (!jobs.length) {
    jobsEl.appendChild(emptyCard(t("emptyJobs")));
    return;
  }
  for (const job of jobs) jobsEl.appendChild(jobCard(job));
}

async function refreshAll() {
  await Promise.all([pollEvents(), refreshAssets(), refreshJobs()]);
}

async function uploadFiles(files) {
  if (!files.length) return;
  if (!state.sessionId) {
    renderError(t("connectionError"), boot);
    return;
  }
  const form = new FormData();
  form.append("session_id", state.sessionId);
  for (const file of files) form.append("files", file);
  uploadBtn.disabled = true;
  try {
    const response = await fetch("/api/uploads", { method: "POST", body: form });
    if (!response.ok) throw new Error(await response.text());
    await refreshAll();
  } catch (error) {
    console.error("upload failed:", error);
    renderError(t("uploadFailed"), () => uploadFiles(files));
  } finally {
    uploadBtn.disabled = false;
    fileInput.value = "";
  }
}

async function sendMessage(text) {
  if (!state.sessionId) {
    renderError(t("connectionError"), boot);
    return;
  }
  sendBtn.disabled = true;
  uploadBtn.disabled = true;
  if (sendLabel) sendLabel.textContent = t("processing");
  showThinking();
  try {
    const response = await fetch("/api/messages", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({ session_id: state.sessionId, text, language: state.language }),
    });
    hideThinking();
    await pollEvents();
    if (!response.ok) {
      console.error("send failed:", await response.text());
      renderError(t("sendFailed"), () => sendMessage(text));
    }
    await refreshAll();
  } catch (error) {
    console.error("send failed:", error);
    renderError(t("sendFailed"), () => sendMessage(text));
  } finally {
    hideThinking();
    sendBtn.disabled = false;
    uploadBtn.disabled = false;
    applyI18n();
  }
}

/* ---- Polling with backoff; recovers the connection banner automatically ---- */
async function pollTick() {
  let ok = true;
  try {
    await refreshAll();
  } catch (error) {
    ok = false;
    console.error("refresh failed:", error);
  }
  if (ok) {
    state.pollDelay = POLL_BASE;
    if (state.connection !== "ok") setConnection("ok");
  } else {
    state.pollDelay = Math.min(Math.round(state.pollDelay * 1.6), POLL_MAX);
    setConnection("error");
  }
  state.pollTimer = setTimeout(pollTick, state.pollDelay);
}

function startPolling() {
  clearTimeout(state.pollTimer);
  state.pollDelay = POLL_BASE;
  state.pollTimer = setTimeout(pollTick, state.pollDelay);
}

async function boot() {
  setConnection("connecting");
  clearTimeout(state.pollTimer);
  try {
    await ensureSession();
    await refreshAll();
    setConnection("ok");
    startPolling();
  } catch (error) {
    console.error("boot failed:", error);
    setConnection("error");
  }
}

/* ---- Events ---- */
composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = messageInput.value.trim();
  if (!text) return;
  messageInput.value = "";
  await sendMessage(text);
});

messageInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  // Desktop only: Enter sends, Shift+Enter is a newline. Mobile keeps Enter as newline.
  if (window.matchMedia("(min-width: 861px)").matches) {
    event.preventDefault();
    composer.requestSubmit();
  }
});

for (const chip of document.querySelectorAll("[data-cmd]")) {
  chip.addEventListener("click", () => {
    const cmd = chip.dataset.cmd;
    const rest = messageInput.value.replace(/^\/\S+\s*/, "");
    messageInput.value = `${cmd} ${rest}`;
    messageInput.focus();
    const end = messageInput.value.length;
    messageInput.setSelectionRange(end, end);
  });
}

let dragDepth = 0;
function dragHasFiles(event) {
  return Array.from(event.dataTransfer?.types || []).includes("Files");
}
chatPane.addEventListener("dragenter", (event) => {
  if (!dragHasFiles(event)) return;
  event.preventDefault();
  dragDepth += 1;
  chatPane.classList.add("dragover");
});
chatPane.addEventListener("dragover", (event) => {
  if (!dragHasFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
chatPane.addEventListener("dragleave", (event) => {
  if (!dragHasFiles(event)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) chatPane.classList.remove("dragover");
});
chatPane.addEventListener("drop", (event) => {
  event.preventDefault();
  dragDepth = 0;
  chatPane.classList.remove("dragover");
  const files = Array.from(event.dataTransfer?.files || []);
  if (files.length) uploadFiles(files);
});

uploadBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => uploadFiles(Array.from(fileInput.files || [])));
refreshBtn.addEventListener("click", () => {
  clearTimeout(state.pollTimer);
  pollTick();
});
checkAssetsBtn.addEventListener("click", () => sendMessage("/check_assets"));
cutBtn.addEventListener("click", () => sendMessage("/cut"));
langZhBtn.addEventListener("click", () => setLanguage("zh"));
langEnBtn.addEventListener("click", () => setLanguage("en"));
retryBtn.addEventListener("click", () => {
  clearTimeout(state.pollTimer);
  if (state.sessionId) {
    setConnection("connecting");
    pollTick();
  } else {
    boot();
  }
});

settingsBtn.addEventListener("click", () => {
  apiKeyInput.value = apiKey();
  settingsDialog.showModal();
});
saveKeyBtn.addEventListener("click", () => {
  const value = apiKeyInput.value.trim();
  if (value) sessionStorage.setItem("omc_deepseek_key", value);
  else sessionStorage.removeItem("omc_deepseek_key");
});
clearKeyBtn.addEventListener("click", () => {
  if (apiKey() && !window.confirm(t("clearConfirm"))) return;
  apiKeyInput.value = "";
  sessionStorage.removeItem("omc_deepseek_key");
});

applyI18n();
boot();
window.addEventListener("pagehide", cleanupCurrentSession);
