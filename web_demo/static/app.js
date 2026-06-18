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
    close: "关闭",
    download: "下载",
    emptyAssets: "暂无素材",
    emptyJobs: "暂无任务",
    file: "文件",
    image: "图片",
    jobsTitle: "任务",
    messagePlaceholder: "发送素材后输入剪辑指令，或使用 /edit、/refine、/music、/cut",
    messagesAria: "消息",
    pause: "暂停",
    refresh: "刷新",
    refreshTitle: "刷新状态",
    save: "保存",
    send: "发送",
    sendFailed: "发送失败",
    sendTitle: "发送消息",
    sessionPrefix: "会话",
    settings: "设置",
    settingsHeading: "设置",
    settingsTitle: "DeepSeek API Key",
    statusAria: "状态",
    unknownJob: "任务",
    upload: "上传",
    uploadFailed: "上传失败",
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
    close: "Close",
    download: "Download",
    emptyAssets: "No assets yet",
    emptyJobs: "No jobs yet",
    file: "File",
    image: "Image",
    jobsTitle: "Jobs",
    messagePlaceholder: "Upload assets, then enter an edit instruction or use /edit, /refine, /music, /cut",
    messagesAria: "Messages",
    pause: "Pause",
    refresh: "Refresh",
    refreshTitle: "Refresh status",
    save: "Save",
    send: "Send",
    sendFailed: "Send failed",
    sendTitle: "Send message",
    sessionPrefix: "Session",
    settings: "Settings",
    settingsHeading: "Settings",
    settingsTitle: "DeepSeek API Key",
    statusAria: "Status",
    unknownJob: "job",
    upload: "Upload",
    uploadFailed: "Upload failed",
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

const state = {
  sessionId: "",
  cleanupSessionId: localStorage.getItem("omc_web_session") || "",
  language: initialLanguage(),
  lastEventId: 0,
  polling: null,
  cleanupSent: false,
};

const messagesEl = document.querySelector("#messages");
const assetsEl = document.querySelector("#assets");
const jobsEl = document.querySelector("#jobs");
const sessionLabel = document.querySelector("#sessionLabel");
const composer = document.querySelector("#composer");
const messageInput = document.querySelector("#messageInput");
const fileInput = document.querySelector("#fileInput");
const uploadBtn = document.querySelector("#uploadBtn");
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

function updateSessionLabel() {
  sessionLabel.textContent = state.sessionId ? `${t("sessionPrefix")}: ${state.sessionId}` : t("webDemo");
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
  await refreshAll();
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
  if (document.querySelector(`[data-event-id="${event.id}"]`)) return;
  const node = document.createElement("article");
  node.className = `message ${event.role || "assistant"} ${event.kind === "error" ? "error" : ""}`;
  node.dataset.eventId = event.id;
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
  messagesEl.scrollTop = messagesEl.scrollHeight;
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

function assetLabel(asset) {
  const type = mediaTypeLabel(asset.media_type || "file");
  const name = asset.original_name || asset.asset_id || "asset";
  const size = asset.size_bytes ? `${Math.round(asset.size_bytes / 1024)} KB` : "";
  return `${type} · ${name}${size ? ` · ${size}` : ""}`;
}

async function refreshAssets() {
  if (!state.sessionId) return;
  const response = await fetch(`/api/assets?session_id=${encodeURIComponent(state.sessionId)}`);
  if (!response.ok) return;
  const payload = await response.json();
  const assets = (((payload.data || {}).manifest || {}).assets || []);
  assetsEl.innerHTML = "";
  if (!assets.length) {
    const empty = document.createElement("div");
    empty.className = "status-card";
    const label = document.createElement("span");
    label.textContent = t("emptyAssets");
    empty.appendChild(label);
    assetsEl.appendChild(empty);
    return;
  }
  for (const asset of assets) {
    const card = document.createElement("div");
    card.className = "status-card";
    const status = asset.exists === false ? t("assetMissing") : t("assetSaved");
    const title = document.createElement("strong");
    title.textContent = assetLabel(asset);
    const statusNode = document.createElement("span");
    statusNode.textContent = status;
    card.appendChild(title);
    card.appendChild(statusNode);
    assetsEl.appendChild(card);
  }
}

function renderJob(job) {
  const card = document.createElement("div");
  card.className = "status-card";
  const report = job.report || {};
  const downloads = job.downloads || [];
  const summary = report.user_summary || report.latest_progress || "";
  const title = document.createElement("strong");
  title.textContent = `${job.job_id || t("unknownJob")} · ${job.status || "unknown"}`;
  const body = document.createElement("p");
  body.textContent = summary;
  card.appendChild(title);
  card.appendChild(body);
  for (const item of downloads) {
    const link = document.createElement("a");
    link.href = item.url;
    link.textContent = `${t("download")} ${item.filename}`;
    link.target = "_blank";
    card.appendChild(link);
  }
  return card;
}

async function refreshJobs() {
  if (!state.sessionId) return;
  const response = await fetch(`/api/jobs?session_id=${encodeURIComponent(state.sessionId)}&limit=8`);
  if (!response.ok) return;
  const payload = await response.json();
  const jobs = ((payload.data || {}).jobs || []);
  jobsEl.innerHTML = "";
  if (!jobs.length) {
    const empty = document.createElement("div");
    empty.className = "status-card";
    const label = document.createElement("span");
    label.textContent = t("emptyJobs");
    empty.appendChild(label);
    jobsEl.appendChild(empty);
    return;
  }
  for (const job of jobs) jobsEl.appendChild(renderJob(job));
}

async function refreshAll() {
  await Promise.all([pollEvents(), refreshAssets(), refreshJobs()]);
}

async function uploadFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  form.append("session_id", state.sessionId);
  for (const file of files) form.append("files", file);
  uploadBtn.disabled = true;
  try {
    const response = await fetch("/api/uploads", { method: "POST", body: form });
    if (!response.ok) throw new Error(await response.text());
    await refreshAll();
  } catch (error) {
    renderEvent({ id: `local-${Date.now()}`, role: "assistant", kind: "error", text: `${t("uploadFailed")}：${error.message}` });
  } finally {
    uploadBtn.disabled = false;
    fileInput.value = "";
  }
}

async function sendMessage(text) {
  const response = await fetch("/api/messages", {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ session_id: state.sessionId, text, language: state.language }),
  });
  await pollEvents();
  if (!response.ok) {
    const detail = await response.text();
    renderEvent({ id: `local-${Date.now()}`, role: "assistant", kind: "error", text: `${t("sendFailed")}：${detail}` });
  }
  await refreshAll();
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = messageInput.value.trim();
  if (!text) return;
  messageInput.value = "";
  await sendMessage(text);
});

uploadBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => uploadFiles(Array.from(fileInput.files || [])));
refreshBtn.addEventListener("click", refreshAll);
checkAssetsBtn.addEventListener("click", () => sendMessage("/check_assets"));
cutBtn.addEventListener("click", () => sendMessage("/cut"));
langZhBtn.addEventListener("click", () => setLanguage("zh"));
langEnBtn.addEventListener("click", () => setLanguage("en"));

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
  apiKeyInput.value = "";
  sessionStorage.removeItem("omc_deepseek_key");
});

applyI18n();
ensureSession().then(() => {
  refreshAll();
  state.polling = setInterval(refreshAll, 3000);
});
window.addEventListener("pagehide", cleanupCurrentSession);
