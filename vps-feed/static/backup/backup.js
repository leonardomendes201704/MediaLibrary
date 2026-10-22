const picker = document.getElementById("picker");
const uploadBtn = document.getElementById("upload");
const retryBtn = document.getElementById("retry");
const clearBtn = document.getElementById("clear");
const queueEl = document.getElementById("queue");
const summaryEl = document.getElementById("summary");

const state = {
  config: null,
  files: [],
  items: [],
  uploading: false,
};

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function setSummary(text) {
  if (!text) {
    summaryEl.hidden = true;
    summaryEl.textContent = "";
    return;
  }
  summaryEl.hidden = false;
  summaryEl.textContent = text;
}

function statusLabel(status) {
  if (status === "pending") return "Na fila";
  if (status === "presign") return "Preparando…";
  if (status === "uploading") return "Enviando…";
  if (status === "ok") return "Enviado";
  if (status === "error") return "Falhou";
  return status;
}

function renderQueue() {
  queueEl.replaceChildren();
  for (const entry of state.items) {
    const li = document.createElement("li");
    li.className = `item ${entry.status}`;
    const img = document.createElement("img");
    img.className = "thumb";
    img.alt = "";
    img.src = entry.preview;
    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("p");
    name.className = "name";
    name.textContent = `${entry.file.name} (${formatBytes(entry.file.size)})`;
    const status = document.createElement("p");
    status.className = "status";
    status.textContent = statusLabel(entry.status);
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("span");
    fill.style.width = `${entry.progress}%`;
    bar.append(fill);
    meta.append(name, status, bar);
    li.append(img, meta);
    queueEl.append(li);
    entry.ui = { li, status, fill };
  }
  const hasFiles = state.items.length > 0;
  const hasErrors = state.items.some((entry) => entry.status === "error");
  uploadBtn.disabled = state.uploading || state.items.length === 0;
  retryBtn.hidden = !hasErrors || state.uploading;
  clearBtn.hidden = !hasFiles || state.uploading;
}

const VIDEO_EXT = new Set([".mp4", ".webm", ".mov", ".m4v"]);

function maxBytesFor(file) {
  const ext = `.${(file.name.split(".").pop() || "").toLowerCase()}`;
  if (VIDEO_EXT.has(ext) && state.config.max_video_bytes) return state.config.max_video_bytes;
  return state.config.max_file_bytes;
}

function addFiles(fileList) {
  if (!state.config) return;
  const maxFiles = state.config.max_files;
  const allowed = new Set((state.config.extensions || []).map((ext) => ext.toLowerCase()));
  for (const file of fileList) {
    if (state.items.length >= maxFiles) break;
    const ext = `.${(file.name.split(".").pop() || "").toLowerCase()}`;
    if (!allowed.has(ext)) continue;
    const maxBytes = maxBytesFor(file);
    if (!file.size || file.size > maxBytes) continue;
    const preview = URL.createObjectURL(file);
    state.items.push({
      file,
      preview,
      status: "pending",
      progress: 0,
      key: "",
      uploadUrl: "",
      contentType: file.type || "application/octet-stream",
    });
  }
  renderQueue();
}

async function loadConfig() {
  const response = await fetch("/api/backup/config");
  if (!response.ok) throw new Error("config");
  state.config = await response.json();
  if (!state.config.enabled) throw new Error("disabled");
}

async function presignBatch(entries) {
  const response = await fetch("/api/backup/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      files: entries.map((entry) => ({
        name: entry.file.name,
        size: entry.file.size,
        content_type: entry.file.type || "",
      })),
    }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `batch ${response.status}`);
  }
  const data = await response.json();
  const items = Array.isArray(data.items) ? data.items : [];
  if (items.length !== entries.length) throw new Error("resposta incompleta");
  items.forEach((item, index) => {
    entries[index].key = item.key;
    entries[index].uploadUrl = item.upload_url;
    entries[index].contentType = item.content_type || entries[index].contentType;
  });
}

function uploadOne(entry) {
  return new Promise((resolve) => {
    entry.status = "uploading";
    entry.progress = 0;
    if (entry.ui) {
      entry.ui.status.textContent = statusLabel(entry.status);
      entry.ui.fill.style.width = "0%";
      entry.ui.li.className = `item ${entry.status}`;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", entry.uploadUrl, true);
    xhr.setRequestHeader("Content-Type", entry.contentType);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      entry.progress = Math.round((event.loaded / event.total) * 100);
      if (entry.ui) entry.ui.fill.style.width = `${entry.progress}%`;
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        entry.status = "ok";
        entry.progress = 100;
        resolve(true);
        return;
      }
      entry.status = "error";
      resolve(false);
    };
    xhr.onerror = () => {
      entry.status = "error";
      resolve(false);
    };
    xhr.send(entry.file);
  });
}

async function runQueue(selected) {
  state.uploading = true;
  renderQueue();
  const chunkSize = state.config.max_files;
  const concurrency = state.config.concurrency || 4;
  let ok = 0;
  let fail = 0;
  for (let offset = 0; offset < selected.length; offset += chunkSize) {
    const chunk = selected.slice(offset, offset + chunkSize);
    chunk.forEach((entry) => {
      entry.status = "presign";
      if (entry.ui) entry.ui.status.textContent = statusLabel(entry.status);
    });
    try {
      await presignBatch(chunk);
    } catch (error) {
      chunk.forEach((entry) => {
        entry.status = "error";
      });
      fail += chunk.length;
      renderQueue();
      continue;
    }
    let cursor = 0;
    const workers = Array.from({ length: Math.min(concurrency, chunk.length) }, async () => {
      while (cursor < chunk.length) {
        const index = cursor;
        cursor += 1;
        const entry = chunk[index];
        const success = await uploadOne(entry);
        if (success) ok += 1;
        else fail += 1;
        if (entry.ui) {
          entry.ui.status.textContent = statusLabel(entry.status);
          entry.ui.fill.style.width = `${entry.progress}%`;
          entry.ui.li.className = `item ${entry.status}`;
        }
      }
    });
    await Promise.all(workers);
  }
  state.uploading = false;
  setSummary(`${ok} enviado(s), ${fail} falha(s).`);
  renderQueue();
}

async function startUpload() {
  const pending = state.items.filter((entry) => entry.status === "pending" || entry.status === "error");
  if (!pending.length) return;
  setSummary("");
  await runQueue(pending);
}

function clearQueue() {
  for (const entry of state.items) URL.revokeObjectURL(entry.preview);
  state.items = [];
  setSummary("");
  renderQueue();
}

picker.addEventListener("change", () => {
  if (!picker.files?.length) return;
  addFiles(picker.files);
  picker.value = "";
});

uploadBtn.addEventListener("click", () => {
  startUpload().catch(() => setSummary("Nao foi possivel enviar."));
});

retryBtn.addEventListener("click", () => {
  const failed = state.items.filter((entry) => entry.status === "error");
  failed.forEach((entry) => {
    entry.status = "pending";
    entry.progress = 0;
    entry.uploadUrl = "";
    entry.key = "";
  });
  startUpload().catch(() => setSummary("Nao foi possivel reenviar."));
});

clearBtn.addEventListener("click", clearQueue);

loadConfig()
  .then(() => renderQueue())
  .catch(() => setSummary("Backup indisponivel."));
