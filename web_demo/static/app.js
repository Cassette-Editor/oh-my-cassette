const state = {
  sessionId: localStorage.getItem("omc_web_session") || "",
  lastEventId: 0,
  polling: null,
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

async function ensureSession() {
  if (state.sessionId) {
    sessionLabel.textContent = state.sessionId;
    return;
  }
  const response = await fetch("/api/sessions", { method: "POST" });
  const payload = await response.json();
  state.sessionId = payload.session_id;
  localStorage.setItem("omc_web_session", state.sessionId);
  sessionLabel.textContent = state.sessionId;
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
      img.alt = event.attachment_name || "attachment";
      wrap.appendChild(img);
    } else if (event.attachment_type === "video") {
      const video = document.createElement("video");
      video.controls = true;
      video.src = event.attachment_url;
      wrap.appendChild(video);
    } else {
      const link = document.createElement("a");
      link.href = event.attachment_url;
      link.textContent = event.attachment_name || "下载附件";
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

function assetLabel(asset) {
  const type = asset.media_type || "file";
  const name = asset.original_name || asset.asset_id || "asset";
  const size = asset.size_bytes ? `${Math.round(asset.size_bytes / 1024)} KB` : "";
  return `${type} · ${name}${size ? ` · ${size}` : ""}`;
}

async function refreshAssets() {
  const response = await fetch(`/api/assets?session_id=${encodeURIComponent(state.sessionId)}`);
  if (!response.ok) return;
  const payload = await response.json();
  const assets = (((payload.data || {}).manifest || {}).assets || []);
  assetsEl.innerHTML = "";
  if (!assets.length) {
    assetsEl.innerHTML = `<div class="status-card"><span>暂无素材</span></div>`;
    return;
  }
  for (const asset of assets) {
    const card = document.createElement("div");
    card.className = "status-card";
    card.innerHTML = `<strong>${assetLabel(asset)}</strong><span>${asset.exists === false ? "文件缺失" : "已保存"}</span>`;
    assetsEl.appendChild(card);
  }
}

function renderJob(job) {
  const card = document.createElement("div");
  card.className = "status-card";
  const report = job.report || {};
  const downloads = job.downloads || [];
  const summary = report.user_summary || report.latest_progress || "";
  card.innerHTML = `<strong>${job.job_id || "job"} · ${job.status || "unknown"}</strong><p>${summary}</p>`;
  for (const item of downloads) {
    const link = document.createElement("a");
    link.href = item.url;
    link.textContent = `下载 ${item.filename}`;
    link.target = "_blank";
    card.appendChild(link);
  }
  return card;
}

async function refreshJobs() {
  const response = await fetch(`/api/jobs?session_id=${encodeURIComponent(state.sessionId)}&limit=8`);
  if (!response.ok) return;
  const payload = await response.json();
  const jobs = ((payload.data || {}).jobs || []);
  jobsEl.innerHTML = "";
  if (!jobs.length) {
    jobsEl.innerHTML = `<div class="status-card"><span>暂无任务</span></div>`;
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
    renderEvent({ id: `local-${Date.now()}`, role: "assistant", kind: "error", text: `上传失败：${error.message}` });
  } finally {
    uploadBtn.disabled = false;
    fileInput.value = "";
  }
}

async function sendMessage(text) {
  const response = await fetch("/api/messages", {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ session_id: state.sessionId, text }),
  });
  await pollEvents();
  if (!response.ok) {
    const detail = await response.text();
    renderEvent({ id: `local-${Date.now()}`, role: "assistant", kind: "error", text: `发送失败：${detail}` });
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

ensureSession().then(() => {
  refreshAll();
  state.polling = setInterval(refreshAll, 3000);
});

